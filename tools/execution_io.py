"""Phase 23/25: the execution surface's file *reads*, bound to the shadow and to nothing else.

The engine's file access has two halves with **opposed** security contracts, and until Phase 23 they
were one module behind one resolver. That is how ``read_source`` came to resolve against the user's
live project directory while the patch it was reading *for* landed in the shadow -- a
read-modify-write sequence split across two roots, so the second step could not see the first
step's edit.

* **This module is the run's half.** ``read_source`` is what the workflow reads *as the agent*: the
  current contents of a file it is about to patch. It resolves against the **execution root** -- the
  active shadow (``tools.workspace.get_execution_dir``), or the root the orchestrator injects -- and
  this module does not import ``get_project_dir`` at all. That is the boundary: an edit here cannot
  reach the user's tree by accident, because the name is not in scope.
* **``tools.workspace_io`` is the frontend's half**: the file listing, the preview and the ``.env``
  reader. Those are reads of the *user's project* and stay on ``get_project_dir``.

**The writing half is gone, and deliberately.** ``overwrite_source`` lived here until Phase 25. It
had no production caller -- ``executor.apply_artifact`` publishes through the **MCP filesystem
server** (``client.write_file``), whose root is the shadow -- so it was a second writer with a
second set of rules and nothing left to use it. The executor is the one writer; a reader is all
this module needs to be.

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


__all__ = ["PathDenied", "execution_root", "read_source"]
