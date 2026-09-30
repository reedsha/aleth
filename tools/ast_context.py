"""AST + vector context assembly: System 2 reads exact nodes, not sliced text.

This replaces the line-anchored context slicer. Context for a task is assembled from
*structurally complete* code units -- whole classes, methods and functions, with the exact
byte span each one occupies -- retrieved from a local LanceDB index by cosine similarity
against the task's own words. Nothing here slices a file by line number or by character
count, and nothing here searches source text with a regular expression: ``ast_chunker``
parses with tree-sitter, and this module only embeds and retrieves the nodes it returns.

The index is a cache, not a source of truth. It lives in the system temporary directory,
keyed by the workspace's real path, and is rebuilt whenever a source file is newer than
the last build. A workspace that cannot be indexed degrades to no retrieval rather than
failing the run -- the plan slice alone is still a usable prompt.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from typing import Iterable, List, Optional

from tools.ast_chunker import CodeUnit, chunk_file, detect_language
from tools.semantic_index import CodeUnitIndex, Embedder, HashingEmbedder, SearchHit
from tools.workspace import walk_workspace

# Where the per-workspace index caches live. The system temp directory, so a build never
# appears in the workspace the codebase audit walks, and never in the user's repository.
_INDEX_ROOT = os.path.join(tempfile.gettempdir(), "deepagents_ast_index")

# How many units a task's context carries by default. Enough to cover a task's own file
# plus its neighbours, small enough that the prompt stays a slice rather than a dump.
DEFAULT_K = 6


def _workspace_key(workspace_dir: str) -> str:
    """A stable, filesystem-safe id for a workspace path."""
    digest = hashlib.sha1(os.path.realpath(workspace_dir).encode("utf-8")).hexdigest()
    return digest[:16]


def index_dir(workspace_dir: str) -> str:
    """The cache directory the workspace's index lives in."""
    return os.path.join(_INDEX_ROOT, _workspace_key(workspace_dir))


def resolve_embedder() -> Embedder:
    """The production embedder, or the offline stand-in when the model is unavailable.

    ``MiniLmEmbedder`` is the semantic encoder, but its weights are fetched from the Hub on
    first use and it needs ``torch``/``transformers``. When either is missing this falls
    back to the deterministic hashed bag-of-words encoder, so retrieval still works
    offline instead of the whole context path failing.
    """
    try:
        import torch  # noqa: F401
        from transformers import AutoModel  # noqa: F401

        from tools.semantic_index import MiniLmEmbedder

        return MiniLmEmbedder()
    except Exception:
        return HashingEmbedder()


def iter_source_files(workspace_dir: str) -> List[str]:
    """Every file in the workspace that a grammar can parse, in a stable order."""
    if not os.path.isdir(workspace_dir):
        return []
    found: List[str] = []
    for root, _dirs, files in walk_workspace(workspace_dir):
        for name in files:
            path = os.path.join(root, name)
            if detect_language(path) is not None:
                found.append(path)
    return sorted(found)


def _newest_mtime(paths: Iterable[str]) -> float:
    """The most recent modification time across ``paths`` (0.0 when there are none)."""
    newest = 0.0
    for path in paths:
        try:
            newest = max(newest, os.path.getmtime(path))
        except OSError:
            continue
    return newest


def _manifest_path(workspace_dir: str) -> str:
    return os.path.join(index_dir(workspace_dir), "_built.json")


def _is_stale(workspace_dir: str, sources: List[str]) -> bool:
    """Whether the cached index is missing or older than any source file."""
    manifest = _manifest_path(workspace_dir)
    if not os.path.isfile(manifest):
        return True
    try:
        with open(manifest, "r", encoding="utf-8") as handle:
            built_at = float(json.load(handle).get("built_at", 0.0))
    except (OSError, ValueError, TypeError):
        return True
    return _newest_mtime(sources) > built_at


def build_index(
    workspace_dir: str,
    *,
    embedder: Optional[Embedder] = None,
) -> CodeUnitIndex:
    """Chunk every parseable file and (re)build the workspace's vector index."""
    sources = iter_source_files(workspace_dir)
    units: List[CodeUnit] = []
    for path in sources:
        try:
            units.extend(chunk_file(path))
        except Exception:
            # A file the grammar cannot parse is skipped, not fatal: the index is context,
            # and one bad file must not cost the whole task its context.
            continue

    target = index_dir(workspace_dir)
    shutil.rmtree(target, ignore_errors=True)
    os.makedirs(target, exist_ok=True)

    chosen = embedder or resolve_embedder()
    try:
        index = CodeUnitIndex(os.path.join(target, "vectors"), chosen)
        index.index_units(units)
    except Exception:
        # The semantic encoder failed (no weights, no network): the offline stand-in keeps
        # retrieval working rather than leaving the task with no context at all.
        index = CodeUnitIndex(os.path.join(target, "vectors"), HashingEmbedder())
        index.index_units(units)

    with open(_manifest_path(workspace_dir), "w", encoding="utf-8") as handle:
        json.dump(
            {"built_at": _newest_mtime(sources), "units": len(units), "files": len(sources)},
            handle,
        )
    return index


def ensure_index(
    workspace_dir: str,
    *,
    embedder: Optional[Embedder] = None,
    rebuild: bool = False,
) -> Optional[CodeUnitIndex]:
    """The workspace's index, rebuilt when stale. ``None`` when it cannot be built."""
    if not workspace_dir or not os.path.isdir(workspace_dir):
        return None
    sources = iter_source_files(workspace_dir)
    if not sources:
        return None
    if not rebuild and not _is_stale(workspace_dir, sources):
        try:
            return CodeUnitIndex(os.path.join(index_dir(workspace_dir), "vectors"), embedder or resolve_embedder())
        except Exception:
            pass
    try:
        return build_index(workspace_dir, embedder=embedder)
    except Exception:
        return None


def retrieve(
    workspace_dir: str,
    query: str,
    k: int = DEFAULT_K,
    *,
    embedder: Optional[Embedder] = None,
) -> List[SearchHit]:
    """The ``k`` AST units closest to ``query``, best first. ``[]`` when unindexed."""
    index = ensure_index(workspace_dir, embedder=embedder)
    if index is None or not (query or "").strip():
        return []
    try:
        return index.search(query, k=k)
    except Exception:
        return []


def render_hits(hits: Iterable[SearchHit]) -> str:
    """Render retrieved units as a prompt block, each labelled with its exact span.

    The byte range is carried into the prompt deliberately: it is what lets the Coder name
    a precise node (``edit_ast_node``) instead of describing a whole-file rewrite.
    """
    blocks: List[str] = []
    for hit in hits:
        unit = hit.unit
        blocks.append(
            f"### `{unit.filepath}` :: `{unit.qualified_name}` ({unit.kind})\n"
            f"<!-- bytes {unit.byte_start}..{unit.byte_end}, lines {unit.line_start}-{unit.line_end} -->\n"
            f"```{unit.language}\n{unit.content}\n```"
        )
    return "\n\n".join(blocks)


def context_for_query(
    workspace_dir: str,
    query: str,
    k: int = DEFAULT_K,
    *,
    embedder: Optional[Embedder] = None,
) -> str:
    """The rendered AST context for ``query``, or ``""`` when nothing is indexed."""
    return render_hits(retrieve(workspace_dir, query, k=k, embedder=embedder))
