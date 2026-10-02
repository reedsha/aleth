"""Phase 23: the execution surface's file I/O, bound to the shadow and to nothing else.

The engine's file access has two halves with **opposed** security contracts, and until Phase 23 they
were one module behind one resolver. That is how ``overwrite_source`` came to resolve against the
user's live project directory while every other execution path resolved against the shadow -- a
titanium cage with a pipeline out of it (Phase 20).

* **This module is the run's half.** ``overwrite_source`` and ``read_source`` are what the workflow
  writes and reads *as the agent*: a deliverable, and the current contents of a file it is about to
  patch. Both resolve against the **execution root** -- the active shadow
  (``tools.workspace.get_execution_dir``), or the root the orchestrator injects -- and this module
  does not import ``get_project_dir`` at all. That is the boundary: an edit here cannot reach the
  user's tree by accident, because the name is not in scope.
* **``tools.workspace_io`` is the frontend's half**: the file listing, the preview and the ``.env``
  reader. Those are reads of the *user's project* and stay on ``get_project_dir``.

``read_source`` belongs here rather than with the readers for a sharper reason than symmetry: it is
the read half of a read-modify-write patch. Reading the host while the patch lands in the shadow
patches a stale base -- the first step's edit would be invisible to the second -- so the two halves
of that sequence have to resolve the same root or the sequence is simply wrong.

**The root is injected, not guessed.** ``root=`` is a parameter: the orchestrator passes the run's
root (``WorkflowContext.execution_root``, captured from the shadow when the run starts), and a
caller with no root falls back to the process's active execution root. Either way the resolver
refuses -- with a hard :class:`PathDenied`, which *is* a ``ValueError`` -- any path resolving outside
the granted root, so an injected root cannot be escaped with a filename.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from tools import atomic_io
from tools.workspace import get_execution_dir


class PathDenied(ValueError):
    """A path resolved outside the root the caller was granted. A hard refusal.

    A ``ValueError`` on purpose: an out-of-root path is a malformed argument, and a caller that
    handles it should be handling the refusal rather than reaching for a bare ``Exception``.
    """


def execution_root(root: Optional[str] = None) -> str:
    """The absolute root execution I/O is granted: the injected one, else the active shadow.

    ``get_execution_dir()`` is the process's active execution root -- the shadow for the duration of
    a staged run, the project directory otherwise (``EngineService._staged_execution``). A caller
    that *has* a root injects it, and the process global is not consulted at all.
    """
    return os.path.abspath(root or get_execution_dir())


def _resolve(filename: str, *, root: str) -> Path:
    """``filename`` resolved inside ``root``, or :class:`PathDenied`.

    ``Path.resolve()`` on both sides is the whole guard: it normalises ``..``, expands a symlink to
    its target and makes an absolute argument absolute, so the containment test that follows cannot
    be fooled by the shape of the string.
    """
    granted = Path(root).resolve()
    candidate = (granted / str(filename or "")).resolve()
    if candidate != granted and granted not in candidate.parents:
        raise PathDenied(
            f"Path traversal denied: {filename!r} is outside the execution root {granted}."
        )
    return candidate


def overwrite_source(filename: str, content: str, *, root: Optional[str] = None) -> str:
    """Create or replace a file in the execution root. **Not an LLM tool.**

    The workflow publishes its own deliverables through this, because a deliverable may already
    exist from a previous run and re-running a task must be able to refresh it. The chokehold is on
    the model's write tool (the MCP filesystem server's ``create_file``, which refuses to
    overwrite), not on the engine publishing its own artifact.

    Atomic (``tools.atomic_io``): a run can be severed mid-write by the plan TTL or the OOM killer,
    and a half-written deliverable presented as a finished one is worse than no write at all.
    """
    filepath = _resolve(filename, root=execution_root(root))
    atomic_io.write_text_atomic(str(filepath), content)
    return f"Successfully wrote {len(content)} characters to {filename}."


def read_source(filename: str, *, root: Optional[str] = None) -> str:
    """The file's full text from the execution root, or ``""`` when it is missing or unreadable.

    No token-optimised truncation: the caller that reads through this is reading in order to write
    the content *back* (a bug patch, a rebuild), and an omitted middle would be silently deleted
    when the truncated text is written to disk.

    A refusal is **not** softened to ``""``. A missing file is an ordinary answer -- there is
    nothing to patch yet -- but a path outside the granted root is a refusal, and a caller that
    swallowed it would go on to write a patch somewhere it never intended.
    """
    filepath = _resolve(filename, root=execution_root(root))
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace", newline="") as handle:
            return handle.read()
    except OSError:
        return ""


__all__ = ["PathDenied", "execution_root", "overwrite_source", "read_source"]
