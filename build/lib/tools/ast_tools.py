"""Surgical AST tools, built per run and bound to a live MCP session.

**AST parsing belongs in the orchestrator; workspace I/O belongs in the MCP server.** These
tools are exactly that split: they read through the session, do the structural work in
Python, and commit through the session. Nothing here opens a file, and nothing here holds a
connection -- the tools close over the session they were built for, so they die with the run
like everything else in ``orchestration.mcp_session``.

Why these exist at all: the model's file tools are MCP tools now, and the filesystem server
can only offer whole-file operations. An agent with only those would rewrite whole files --
which is the architectural crime Phase 3.5 was built to prevent. So the three structural
tools live here, where the parser is, and reach the disk only through the server.

The chokehold is preserved by *shape*, not by a flag:

* ``create_file`` (server-side) refuses to overwrite an existing path, so the model cannot
  dump a whole file over code it did not reproduce;
* ``edit_ast_node`` replaces one node's exact byte span, and refuses a splice that would leave
  the file unparseable;
* ``append_to_file`` extends the current content rather than replacing it.
"""

from __future__ import annotations

from typing import Any, List

from pydantic import BaseModel, Field

_STRICT = {"extra": "forbid", "strict": False}


class ListSymbolsArgs(BaseModel):
    model_config = _STRICT

    filename: str = Field(..., description="Workspace-relative path of the file to inspect.")


class EditAstNodeArgs(BaseModel):
    model_config = _STRICT

    filename: str = Field(..., description="Workspace-relative path of the file to edit.")
    symbol: str = Field(..., description="Qualified name of the node, e.g. 'Greeter.bye'.")
    replacement: str = Field(..., description="The complete new source for exactly that node.")


class AppendToFileArgs(BaseModel):
    model_config = _STRICT

    filename: str = Field(..., description="Workspace-relative path of the file to extend.")
    content: str = Field(..., description="The text to add at the end.")


def _units(text: str, filename: str) -> List[Any]:
    from tools.ast_chunker import chunk_source

    return chunk_source(text, filename)


def _find(units: List[Any], symbol: str) -> Any:
    """The unit a name selects: exact ``qualified_name`` first, then bare name.

    The qualified name wins so ``Outer.Inner.run`` is never confused with a same-named ``run``
    elsewhere in the file.
    """
    for unit in units:
        if unit.qualified_name == symbol:
            return unit
    for unit in units:
        if unit.name == symbol:
            return unit
    return None


def build_ast_tools(session: Any) -> List[Any]:
    """The three structural tools, bound to ``session`` for I/O.

    Returns framework tools, so they sit in the same bound set as the server's own tools and
    the model cannot tell which side of the boundary each one lives on.
    """
    from langchain_core.tools import StructuredTool

    def list_symbols(filename: str) -> str:
        text = session.read_file(filename)
        units = _units(text, filename)
        if not units:
            return f"{filename} declares no classes, methods or functions."
        return "\n".join(
            f"{unit.kind} {unit.qualified_name} "
            f"(lines {unit.line_start}-{unit.line_end}, bytes {unit.byte_start}-{unit.byte_end})"
            for unit in units
        )

    def edit_ast_node(filename: str, symbol: str, replacement: str) -> str:
        from tools.ast_chunker import parse_has_errors
        from tools.ast_editor import splice_bytes

        text = session.read_file(filename)
        unit = _find(_units(text, filename), symbol)
        if unit is None:
            return f"Edit refused: no symbol {symbol!r} in {filename}."
        try:
            updated = splice_bytes(text, unit.byte_start, unit.byte_end, replacement)
        except ValueError as error:
            return f"Edit refused: {error}"
        # Re-parse before publishing: an edit that leaves the file unparseable is rejected, so
        # a broken splice cannot reach the disk.
        try:
            broken = parse_has_errors(updated, filename, unit.language)
        except Exception as error:
            return f"Edit refused: the edit would leave {filename} unparseable ({error})."
        if broken:
            return f"Edit refused: the edit would leave {filename} with a syntax error."
        session.write_file(filename, updated)
        return (
            f"Replaced {unit.qualified_name} in {filename} "
            f"(bytes {unit.byte_start}-{unit.byte_end})."
        )

    def append_to_file(filename: str, content: str) -> str:
        try:
            existing = session.read_file(filename)
        except Exception:
            existing = ""
        separator = "" if (not existing or existing.endswith("\n")) else "\n"
        session.write_file(filename, f"{existing}{separator}{content}\n")
        return f"Appended to {filename}."

    return [
        StructuredTool.from_function(
            func=list_symbols,
            name="list_symbols",
            description=(
                "List every class, method and function a file declares, with its exact byte "
                "span. Call this before editing: it is the map of the file's AST nodes, so an "
                "edit can name a precise symbol instead of replacing the whole file."
            ),
            args_schema=ListSymbolsArgs,
        ),
        StructuredTool.from_function(
            func=edit_ast_node,
            name="edit_ast_node",
            description=(
                "Replace exactly one AST node (class, method or function) with new source. "
                "Only that node's exact bytes change, so an unrelated line can never be lost. "
                "Refused if the result would not parse."
            ),
            args_schema=EditAstNodeArgs,
        ),
        StructuredTool.from_function(
            func=append_to_file,
            name="append_to_file",
            description=(
                "Add text to the end of a file without deleting what is already there."
            ),
            args_schema=AppendToFileArgs,
        ),
    ]
