"""The run's identity, for correlation -- and the crash report that carries it.

A fault, a log line or a stack trace that cannot be joined to the work that produced it is noise.
This module is the one place that answers "which intent is this process executing right now?", and
the one place that installs the handler which puts that answer into an unhandled-exception report.

Deliberately a **``ContextVar``**, and the difference from a module global is not academic: the
engine runs a thread per run and installs a ``threading.excepthook``, so a global set by thread B
while thread A is crashing files A's stack trace under B's intent. That is worse than no
correlation at all -- it is confidently wrong. A context variable is isolated per thread *and* per
asyncio task, and the hook reads back whichever context is actually running.

The ``intent_id`` threaded explicitly through the workflow layer is the same value, and it is what
the *telemetry* uses. This variable exists for the paths that have no arguments to thread -- an
interpreter-level exception handler being the one that matters, because it is handed nothing but
the exception. Anything that is *not* on the run's own thread (the Stop endpoint, which runs on the
API thread) must not use this: it asks the intent queue what is running instead, because a context
variable is invisible across threads by design.
"""

from __future__ import annotations

import sys
import threading
import traceback
from contextvars import ContextVar
from typing import Any, Optional

# The intent the *current execution context* is running, or "" when it is idle.
_CURRENT_INTENT: ContextVar[str] = ContextVar("aleth_current_intent", default="")


def get_current_intent() -> str:
    """The intent this execution context is running, or ``""`` when it is idle."""
    return _CURRENT_INTENT.get()


def set_current_intent(intent_id: Optional[str]) -> str:
    """Point *this execution context* at ``intent_id`` for one run, or clear it with ``None``.

    The value does not leak to another thread or to a sibling asyncio task, which is the property
    the whole module exists for.
    """
    resolved = str(intent_id or "")
    _CURRENT_INTENT.set(resolved)
    return resolved


def install_exception_logging() -> None:
    """Log an unhandled exception **with the intent it happened in**.

    Both hooks, because both are reachable here: ``sys.excepthook`` for the main thread, and
    ``threading.excepthook`` for a run thread that died outside its own guard. The naked traceback is
    what this removes -- the reader gets the correlation id on the same line, so a crash in a busy
    engine is joinable to the work that caused it instead of being an orphaned stack.

    Installed by ``main.py`` rather than at import: a library that hijacks the interpreter's
    exception reporting the moment it is imported is a library that breaks whatever embeds it.
    """
    def _report(exc_type: Any, exc: Any, tb: Any, thread: str = "") -> None:
        where = f" thread={thread}" if thread else ""
        print(
            f"[engine] unhandled {exc_type.__name__} intent={get_current_intent() or '<none>'}"
            f"{where}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        traceback.print_exception(exc_type, exc, tb, file=sys.stderr)

    sys.excepthook = lambda exc_type, exc, tb: _report(exc_type, exc, tb)

    def _thread_hook(args: Any) -> None:
        _report(
            args.exc_type, args.exc_value, args.exc_traceback,
            thread=str(getattr(args.thread, "name", "") or ""),
        )

    try:
        threading.excepthook = _thread_hook
    except Exception:  # pragma: no cover - a platform without it; the main-thread hook remains
        pass


__all__ = ["get_current_intent", "install_exception_logging", "set_current_intent"]
