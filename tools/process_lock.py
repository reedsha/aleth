"""Phase 41: the process lock. One engine per project, decided by the filesystem.

A port check is a *reactive* heuristic: it says "something is bound", not "an Aleth daemon is
running" -- so a ghost engine that orphaned its SQLite WAL and its Docker networks is
indistinguishable from an unrelated program, and the user is left hunting PIDs by hand. This module
replaces it with a deterministic answer.

``<state_dir>/aleth.pid`` is held under an **exclusive OS file lock** for the daemon's lifetime, and
carries the owning pid. On boot:

* the lock is free -> write our pid and take it;
* the lock is held -> read the pid. If that process is alive, **another daemon is running** and this
  one refuses to start. If it is dead, the lock is **stale residue from a hard crash**: reap it,
  run the Phase 36 container sweep, and take over.

The lock is an OS lock rather than "does the file exist", because a file left by a SIGKILL is
indistinguishable from a live one by existence alone -- and because a lock the kernel drops on
process death cannot itself become a zombie.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
from typing import Iterator, Optional

from filelock import FileLock, Timeout

PID_FILENAME = "aleth.pid"


@dataclasses.dataclass(frozen=True)
class LockState:
    """What the lockfile said, and what was done about it."""

    acquired: bool
    path: str
    pid: int = 0
    holder_pid: int = 0
    stale: bool = False
    detail: str = ""


def pid_path(state_dir: str) -> str:
    """Where the lock lives: beside the ledger, in the project's state directory."""
    return os.path.join(str(state_dir), PID_FILENAME)


def read_pid(path: str) -> int:
    """The pid recorded in the lockfile, or ``0``. Never raises."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return int(str(handle.read()).strip() or 0)
    except (OSError, ValueError):
        return 0


def process_alive(pid: int) -> bool:
    """Whether ``pid`` is a live process.

    POSIX asks the kernel with signal 0. Windows cannot: ``os.kill`` there calls
    ``TerminateProcess``, so probing a live engine's pid would **kill it**. It asks
    ``OpenProcess`` instead, and treats "no such process" as the only dead answer -- an access
    denial means the process exists and is not ours to inspect.

    Where liveness cannot be established at all, this answers ``True``: refusing to start is the
    safe direction, and reaping a lock another engine holds is not.
    """
    if int(pid) <= 0:
        return False
    if os.name == "posix":
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # it exists; it is just not ours to signal
        except OSError:
            return True
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # PROCESS_QUERY_LIMITED_INFORMATION: enough to learn it exists, no more.
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # 87 is ERROR_INVALID_PARAMETER, which is how "no such process" surfaces here.
        return int(kernel32.GetLastError()) != 87
    except Exception:
        return True


@contextlib.contextmanager
def process_lock(state_dir: str, *, reap=None) -> Iterator[LockState]:
    """Hold the project's process lock for the block, or refuse to start.

    ``reap`` is called when a stale lock is found -- ``cli`` passes the Phase 36 container sweep, so
    a hard crash's leftover containers are collected at the moment we discover it happened, rather
    than at the next boot's sweep.
    """
    path = pid_path(state_dir)
    os.makedirs(str(state_dir), exist_ok=True)
    lock = FileLock(path + ".lock")
    try:
        lock.acquire(timeout=0)
    except Timeout:
        holder = read_pid(path)
        yield LockState(
            acquired=False, path=path, holder_pid=holder,
            detail=(
                f"another engine is running (pid {holder})" if holder
                else "another engine holds the lock"
            ),
        )
        return

    previous = read_pid(path)
    stale = bool(previous) and previous != os.getpid() and not process_alive(previous)
    if stale and callable(reap):
        # The lock was free but the file named a dead process: a hard crash. Collect what it left
        # before taking over -- the ledger rows, the shadows and the containers (Phase 36).
        try:
            reap()
        except Exception:
            pass
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
            handle.flush()
            os.fsync(handle.fileno())
        yield LockState(
            acquired=True, path=path, pid=os.getpid(), holder_pid=previous, stale=stale,
            detail="reaped a stale lock from a hard crash" if stale else "acquired",
        )
    finally:
        try:
            if read_pid(path) == os.getpid():
                os.remove(path)
        except OSError:
            pass
        lock.release()


def check(state_dir: str) -> LockState:
    """Whether the engine may start, without taking the lock. For the pre-flight.

    A *probe*: it answers "is another engine running?" and releases immediately, so the real
    acquisition happens in :func:`process_lock` where it is held for the daemon's lifetime.
    """
    path = pid_path(state_dir)
    holder = read_pid(path)
    if not holder:
        return LockState(acquired=True, path=path, detail="no engine is running")
    if holder == os.getpid():
        return LockState(
            acquired=True, path=path, pid=holder, holder_pid=holder,
            detail="this process holds it",
        )
    if process_alive(holder):
        return LockState(
            acquired=False, path=path, holder_pid=holder,
            detail=f"another engine is running (pid {holder})",
        )
    return LockState(
        acquired=True, path=path, holder_pid=holder, stale=True,
        detail=f"a stale lock from a dead process (pid {holder}); it will be reaped on boot",
    )


__all__ = [
    "PID_FILENAME",
    "LockState",
    "check",
    "pid_path",
    "process_alive",
    "process_lock",
    "read_pid",
]
