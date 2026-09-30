"""Process control: kill a whole group, and leave no zombie behind.

WHY A GROUP, NOT A PROCESS
--------------------------
A child that spawns its own children (a shell running a pipeline, a launcher forking a
worker) is not killed by killing the child. The descendants are reparented to init and keep
running -- the "orphaned container" failure. Starting the child in its own **session**
(``start_new_session=True``, which is ``setsid`` on POSIX) makes it the leader of a fresh
process group, so one signal reaches the entire tree:

    os.killpg(process.pid, signal.SIGKILL)

``process.pid`` *is* the pgid, because the child is the group leader. ``preexec_fn`` is
deliberately not used for this: it runs arbitrary Python between ``fork`` and ``exec``, which is
unsafe in a threaded process (the app runs an MCP reader thread per server, a process pool and
the UI bridge), and it is unavailable on Windows.

WHY NOT ``waitpid(-1, WNOHANG)``
--------------------------------
Reaping is done with ``Popen.wait()`` on the process this module was handed, and deliberately
**not** with a ``waitpid(-1, ...)`` sweep. Two reasons, and they matter:

* A sweep cannot reap a *grandchild* -- a grandchild is not this process's child, so it never
  appears to ``waitpid``. The only way to end a grandchild is the group signal above. A sweep
  that appears to "clean up zombies" is doing nothing about the processes the phase is worried
  about.
* A sweep can reap a child this process does **not** own. The Swarm runs a
  ``ProcessPoolExecutor`` whose children are waited on by ``multiprocessing``; stealing one of
  their statuses with ``waitpid(-1)`` leaves multiprocessing raising ``ChildProcessError`` and
  a future that never completes. In a process that owns several kinds of child, a blanket sweep
  is a correctness bug, not a cleanup.

``Popen.wait()`` on the process we spawned is exactly right, and it leaves no zombie.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Optional

IS_POSIX = os.name == "posix"

# How long a child gets to exit on SIGTERM before the group is SIGKILLed.
DEFAULT_GRACE_SECONDS = 5.0


def spawn_kwargs() -> dict:
    """The ``Popen`` keyword that puts the child in its own process group.

    Returned as a dict so every spawn site uses the same one, and so the reason lives in one
    place. ``start_new_session`` is ignored on Windows (there is no ``setsid``); ``kill_group``
    then falls back to killing the process itself.
    """
    return {"start_new_session": True}


def kill_group(process: Optional[subprocess.Popen]) -> bool:
    """SIGKILL the child's whole process group. Returns whether a signal was delivered.

    Never raises: this runs on the timeout path, where an exception would replace the real
    failure (the command timed out) with a spurious one.
    """
    if process is None or process.poll() is not None:
        return False
    if not IS_POSIX:
        try:
            process.kill()
            return True
        except OSError:
            return False
    try:
        os.killpg(process.pid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        # The group is already gone -- the ordinary race with a child that exited on its own.
        return False
    except PermissionError:
        try:
            process.kill()
            return True
        except OSError:
            return False


def terminate_group(
    process: Optional[subprocess.Popen], *, grace: float = DEFAULT_GRACE_SECONDS
) -> int:
    """SIGTERM the group, wait ``grace``, SIGKILL what is left, then reap. Returns the exit code.

    The polite signal first so a child can flush and clean up, and the group signal so nothing
    survives. The final ``wait()`` is what makes this zombie-free: the child is our child, and
    its status is collected here rather than left ``defunct``.
    """
    if process is None:
        return -1
    if process.poll() is not None:
        return process.returncode if process.returncode is not None else -1

    if IS_POSIX:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    else:
        try:
            process.terminate()
        except OSError:
            pass

    deadline = time.monotonic() + max(0.0, float(grace))
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)

    if process.poll() is None:
        kill_group(process)

    try:
        process.wait(timeout=max(1.0, float(grace)))
    except subprocess.TimeoutExpired:  # pragma: no cover - a group that ignores SIGKILL
        pass
    return process.returncode if process.returncode is not None else -1


def group_is_gone(pgid: int) -> bool:
    """Whether no process remains in ``pgid``. Used by the chaos tests, POSIX only.

    Signal 0 asks "may I signal this group?" without delivering anything: ``ProcessLookupError``
    means the group is empty.
    """
    if not IS_POSIX:
        return True
    try:
        os.killpg(pgid, 0)
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        # The group exists but is not ours to signal; it is still alive.
        return False


def close_pipes(process: Optional[subprocess.Popen]) -> None:
    """Close a child's pipes so its file descriptors are released promptly.

    Called after the child is dead: an unclosed pipe is an fd leak, and the phase's
    file-descriptor test counts them.
    """
    if process is None:
        return
    for stream in (process.stdin, process.stdout, process.stderr):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass
