"""Phase 42: the process-wide shutdown flag, and the bounded drain it drives.

A daemon does not die well by having its main thread pulled out from under it: a SQLite write in
flight aborts, an agent's container outlives the engine that made it, and the pid lockfile is left
for the next boot to unpick. A signal is therefore not a kill -- it is a *request*, and this module
is where that request lives.

The signal handler flips one process-wide :class:`threading.Event` and returns. Every long-lived
loop -- the HTTP server, the intent worker, the agent's step loop -- reads that event at a safe
boundary (the start of a request, the start of a step) and stops there, where its state is
consistent. The handler then **drains**: it runs whatever the running components registered as
"finish what you are holding", bounded, and only then does the engine sweep and leave.

One event, one meaning: **the engine is stopping, do not start new work.**

The flag is a ``threading.Event`` rather than a module global because it is read from every thread
-- the API's handler threads, the intent worker, the agent's own loop -- and a plain boolean would
be a torn read waiting to happen. It is *process-wide* on purpose: the shutdown is one decision, and
a component that took its own private copy would keep working after the engine asked it to stop.
"""

from __future__ import annotations

import sys
import threading
from typing import Callable, List

# The flag. Set once by the signal handler; read by every loop that must stop.
_shutdown = threading.Event()

# What "drain" means, component by component. The API server registers its own stop; the engine
# registers nothing here because its teardown is the sweep that runs after the drain. A list, run in
# reverse registration order -- the last thing started is the first thing stopped.
_drains: List[Callable[[], None]] = []
_lock = threading.Lock()

# The strict ceiling on the cooperative drain (Phase 43). A loop blocked on a long network call
# cannot observe the flag until that call returns, so an unbounded drain would wait forever -- the
# operator loses patience, hits Ctrl+C again, and the interpreter dies mid-write anyway. Past this
# the caller must escalate: reap the containers and leave with a failure status.
DRAIN_TIMEOUT_SECONDS = 10.0


def shutdown_event() -> threading.Event:
    """The event itself, for a loop that would rather wait on it than poll."""
    return _shutdown


def is_shutting_down() -> bool:
    """Whether a shutdown is in progress. The one question every loop asks."""
    return _shutdown.is_set()


def begin_shutdown() -> bool:
    """Flip the flag. ``True`` when *this* call was the one that flipped it.

    Idempotent: a second signal -- a user pressing Ctrl+C twice, a supervisor repeating a SIGTERM --
    must not restart the drain or leave the caller unsure who owns the shutdown.
    """
    with _lock:
        first = not _shutdown.is_set()
        _shutdown.set()
    return first


def register_drain(drain: Callable[[], None]) -> None:
    """Register a component's own stop, to run when the engine drains. Idempotent."""
    with _lock:
        if drain not in _drains:
            _drains.append(drain)


def unregister_drain(drain: Callable[[], None]) -> None:
    """Forget a registered stop. Safe when it was never registered."""
    with _lock:
        try:
            _drains.remove(drain)
        except ValueError:
            pass


def clear_drains() -> None:
    """Forget every registered stop. For a test that must not leak into the next one."""
    with _lock:
        _drains.clear()


def drain(timeout: float = DRAIN_TIMEOUT_SECONDS) -> bool:
    """Run every registered stop, **bounded**. Returns whether the drain finished in time.

    A blocking stop -- an HTTP server joining a worker that is itself blocked on a ninety-second
    model call -- cannot be interrupted from here, so the whole drain runs on a worker thread and
    the caller waits with a deadline. A ``False`` is the signal to escalate: the loops did not
    yield inside the budget, and the caller must stop being polite.

    Best effort within the budget: one stop that raises must not stop the others, and nothing on a
    signal path may replace the sender's exit status with a traceback. Each stop still bounds its
    own internal waits; this is the outer ceiling that guarantees the *caller* returns.
    """
    with _lock:
        drains = list(reversed(_drains))
    if not drains:
        return True
    finished = threading.Event()

    def _run() -> None:
        for stop in drains:
            try:
                stop()
            except Exception as error:
                print(
                    f"[lifecycle] a drain step failed: {type(error).__name__}: {error}",
                    file=sys.stderr,
                )
        finished.set()

    # A daemon thread: if the deadline passes the caller exits the process, and a thread that would
    # keep it alive must not.
    worker = threading.Thread(target=_run, name="aleth-drain", daemon=True)
    worker.start()
    if finished.wait(timeout=max(0.0, float(timeout))):
        return True
    print(
        f"[lifecycle] the drain did not finish within {float(timeout):.0f}s; escalating",
        file=sys.stderr,
    )
    return False


def reset() -> None:
    """Clear the flag and the registry. **Tests only** -- a process cannot un-shutdown."""
    with _lock:
        _shutdown.clear()
        _drains.clear()


__all__ = [
    "DRAIN_TIMEOUT_SECONDS",
    "begin_shutdown",
    "clear_drains",
    "drain",
    "is_shutting_down",
    "register_drain",
    "reset",
    "shutdown_event",
    "unregister_drain",
]
