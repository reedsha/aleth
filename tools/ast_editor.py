"""Edits against exact AST nodes: the Coder writes a *symbol*, not a whole file.

The old path asked the model for the complete contents of a file and replaced the file
wholesale -- a blind rewrite in which any unrelated line the model dropped was silently
lost. This module is the opposite: ``tools.ast_chunker`` already knows every node's exact
byte span, so an edit is a splice at that span. There is no string search, no regular
expression and no line-number arithmetic anywhere in it.

Two guards make the splice safe:

* the symbol is looked up in a **fresh parse** of the file, so an edit is always applied
  to the tree the file actually has (a stale span from an earlier turn cannot land); and
* the target must resolve **inside the workspace**, so a symbol name can never be used to
  reach a file the sandbox does not own.
"""

from __future__ import annotations

import os
from typing import List, Optional

from pydantic import BaseModel, ConfigDict

from tools import atomic_io
from tools.ast_chunker import CodeUnit, chunk_file, parse_has_errors

_STRICT = ConfigDict(extra="forbid", strict=False)


class EditResult(BaseModel):
    """The outcome of one symbol edit, as the strict JSON boundary the tool returns."""

    model_config = _STRICT

    success: bool
    error: str = ""
    filename: str = ""
    symbol: str = ""
    byte_start: int = 0
    byte_end: int = 0
    # The size of the change, so the caller can narrate it without re-reading the file.
    lines_added: int = 0
    lines_removed: int = 0


class SymbolList(BaseModel):
    """Every symbol a file declares, with the byte span each one occupies."""

    model_config = _STRICT

    filename: str = ""
    symbols: List[CodeUnit] = []


def _within_workspace(filepath: str, workspace_dir: str) -> bool:
    """Whether ``filepath`` resolves to a location inside ``workspace_dir``.

    ``realpath`` on both sides so a symlink or a ``..`` segment cannot be used to escape
    the workspace the sandbox granted.
    """
    if not workspace_dir:
        return False
    root = os.path.realpath(workspace_dir)
    target = os.path.realpath(filepath)
    return target == root or target.startswith(root + os.sep)


def symbols_in_file(filepath: str) -> SymbolList:
    """Every structurally complete unit in a file, in source order."""
    return SymbolList(filename=filepath, symbols=chunk_file(filepath))


def find_symbol(filepath: str, symbol: str) -> Optional[CodeUnit]:
    """The unit a name selects: its exact ``qualified_name`` first, then its bare name.

    The qualified name wins so ``Outer.Inner.run`` is never confused with a same-named
    ``run`` elsewhere in the file.
    """
    units = chunk_file(filepath)
    for unit in units:
        if unit.qualified_name == symbol:
            return unit
    for unit in units:
        if unit.name == symbol:
            return unit
    return None


def splice_bytes(source: str, byte_start: int, byte_end: int, replacement: str) -> str:
    """Replace exactly ``[byte_start, byte_end)`` of ``source`` with ``replacement``.

    Byte offsets, not character offsets: ``CodeUnit`` reports a byte span, and a document
    with multi-byte characters would otherwise be spliced at the wrong place. A range that
    does not fit the source is refused rather than clamped -- a silently shifted splice is
    how a file gets corrupted.
    """
    raw = source.encode("utf-8")
    if byte_start < 0 or byte_end < byte_start or byte_end > len(raw):
        raise ValueError(
            f"byte range {byte_start}..{byte_end} does not fit a {len(raw)}-byte file"
        )
    spliced = raw[:byte_start] + replacement.encode("utf-8") + raw[byte_end:]
    return spliced.decode("utf-8")


def edit_symbol(
    filepath: str,
    symbol: str,
    replacement: str,
    *,
    workspace_dir: str,
) -> EditResult:
    """Replace one symbol's exact byte span with ``replacement``.

    Returns a refusal (never raises) for a path outside the workspace, a file that cannot
    be read, or a symbol the fresh parse does not contain -- each with the reason, so the
    caller can report it rather than guess.
    """
    if not _within_workspace(filepath, workspace_dir):
        return EditResult(
            success=False,
            error=f"refusing to edit outside the workspace: {filepath}",
            filename=filepath,
            symbol=symbol,
        )
    # The Artifact Gate. A node splice against a file a ``planned`` task targets is refused
    # until that task is approved -- the enforcement is here, at the boundary, not in a
    # prompt the model could ignore.
    from tools.execution_gate import StateExecutionError, guard_write

    try:
        guard_write(filepath)
    except StateExecutionError as refusal:
        return EditResult(
            success=False,
            error=str(refusal),
            filename=filepath,
            symbol=symbol,
        )
    if not os.path.isfile(filepath):
        return EditResult(
            success=False, error=f"{filepath} does not exist", filename=filepath, symbol=symbol
        )
    try:
        with open(filepath, "r", encoding="utf-8", newline="") as handle:
            source = handle.read()
    except OSError as error:
        return EditResult(success=False, error=str(error), filename=filepath, symbol=symbol)

    unit = find_symbol(filepath, symbol)
    if unit is None:
        return EditResult(
            success=False,
            error=f"no symbol {symbol!r} in {os.path.basename(filepath)}",
            filename=filepath,
            symbol=symbol,
        )

    try:
        updated = splice_bytes(source, unit.byte_start, unit.byte_end, replacement)
    except ValueError as error:
        return EditResult(success=False, error=str(error), filename=filepath, symbol=symbol)

    # Re-parse the result before it is published: an edit that leaves the file unparseable
    # is rejected, so a broken splice cannot reach the disk. Tree-sitter is error-tolerant,
    # so the check is the tree's own error flag rather than an exception.
    try:
        broken = parse_has_errors(updated, filepath, unit.language)
    except Exception as error:  # a grammar failure is a refusal, not a crash
        return EditResult(
            success=False,
            error=f"the edit would leave {os.path.basename(filepath)} unparseable: {error}",
            filename=filepath,
            symbol=symbol,
        )
    if broken:
        return EditResult(
            success=False,
            error=f"the edit would leave {os.path.basename(filepath)} with a syntax error",
            filename=filepath,
            symbol=symbol,
        )

    try:
        atomic_io.write_text_atomic(filepath, updated)
    except OSError as error:
        return EditResult(success=False, error=str(error), filename=filepath, symbol=symbol)

    return EditResult(
        success=True,
        filename=filepath,
        symbol=unit.qualified_name,
        byte_start=unit.byte_start,
        byte_end=unit.byte_end,
        lines_added=max(0, replacement.count("\n") - unit.content.count("\n")),
        lines_removed=max(0, unit.content.count("\n") - replacement.count("\n")),
    )
