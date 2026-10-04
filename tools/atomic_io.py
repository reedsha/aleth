"""Phase 22: a write that either completes, or never happened at all.

The engine severs things on purpose. A run that breaches the wall-clock TTL
(``orchestration.autonomy.PLAN_TTL_SECONDS``) or the container's memory ceiling is killed
mid-flight, and a process writing a file at that instant is killed with it. Through an in-place
write -- ``open(path, "w")`` truncates first -- whatever reached the disk is then a *prefix*: half
a module, half a patch. On the host side of a merge that prefix is the user's own file, corrupted
by a run they had only asked to review (Phase 20).

So every durable write goes through a temporary file and one ``os.replace``. ``rename(2)`` is
atomic, so a reader sees the old bytes or the new bytes and never a mixture of the two.

The temporary is a **sibling of its target**, and that is a requirement rather than a preference.
``os.replace`` is only atomic *within one filesystem*; across two it raises ``OSError`` (``EXDEV``,
"Invalid cross-device link"). A shadow workspace lives under ``~/.aleth/state/<project>/staging``
and a merge writes into the user's repository -- frequently two mounts, and under WSL a ``/tmp``
temporary would be a third. A temporary beside its target cannot cross a device, so the rename
cannot fail that way.

Its name carries the ``.tmp`` prefix the engine already treats as scratch
(``tools.staging._copy_ignore`` prunes it from a shadow walk, ``tools.workspace_io`` skips it in the
file listing), so a write interrupted by a SIGKILL leaves a file no part of the system mistakes for
something a project contained.

The payload is encoded and written in binary mode, so it lands byte for byte: this is the text
layer's ``newline=""`` behaviour, with no CRLF translation inserted behind the caller's back.
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from typing import Optional

# The window a copy moves through. Bounded on purpose: a merge can carry a binary larger than
# memory, and holding it whole in order to rename it would trade a torn file for a dead process.
COPY_CHUNK_BYTES = 1024 * 1024


def _temporary_sibling(target: str) -> str:
    """A fresh, collision-proof path in ``target``'s own directory (see the module docstring)."""
    directory, name = os.path.split(target)
    return os.path.join(directory, f".tmp.{name}.{uuid.uuid4().hex}")


def _discard(temporary: str) -> None:
    """Remove a temporary that never became its target. Never raises."""
    try:
        os.remove(temporary)
    except OSError:
        pass


def _prepare(target: str) -> str:
    """The absolute target, with its directory created."""
    resolved = os.path.abspath(target)
    directory = os.path.dirname(resolved)
    if directory:
        os.makedirs(directory, exist_ok=True)
    return resolved


# A rename over a file another process holds open fails on Windows with a sharing violation
# (WinError 32) -- editors, indexers and antivirus hold files open routinely, and the merge path
# renames over the *user's own* files. The compiled core retries its atomic rename for exactly this
# reason (``crates/deepagents_core/src/state.rs``); the Python path used to give up on the first
# attempt, which surfaced as an intermittent ``PermissionError`` in the merge. Same budget as the
# Rust side: ten attempts, 15 ms apart.
RENAME_ATTEMPTS = 10
RENAME_RETRY_SECONDS = 0.015


def _replace_retry(temporary: str, target: str) -> None:
    """``os.replace``, riding out the transient sharing violations of a live filesystem."""
    last: Optional[OSError] = None
    for attempt in range(RENAME_ATTEMPTS):
        try:
            os.replace(temporary, target)
            return
        except OSError as error:
            # Only the transient classes are retried: a missing directory or a permission
            # policy will not heal in 150 ms, and retrying it just delays the honest error.
            if getattr(error, "winerror", None) not in (5, 32, 33) and not isinstance(
                error, (PermissionError, BlockingIOError)
            ):
                raise
            last = error
            time.sleep(RENAME_RETRY_SECONDS)
    assert last is not None
    raise last


def write_bytes_atomic(path: str, payload: bytes) -> str:
    """Write ``payload`` to ``path`` through a temporary and a rename. Returns the absolute path.

    Every failure path -- a refused write, a full disk, a cancelled task -- leaves the target
    exactly as it was and no half-written temporary behind. ``BaseException`` is caught rather than
    ``Exception`` because a cancellation is precisely the interruption this module exists for.
    """
    target = _prepare(path)
    temporary = _temporary_sibling(target)
    try:
        with open(temporary, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_retry(temporary, target)
    except BaseException:
        _discard(temporary)
        raise
    return target


def write_text_atomic(path: str, text: str, *, encoding: str = "utf-8") -> str:
    """``write_bytes_atomic`` for text, with no newline translation (see the module docstring)."""
    return write_bytes_atomic(path, str(text).encode(encoding))


def copy_file_atomic(source: str, destination: str) -> str:
    """Copy ``source`` onto ``destination`` atomically, carrying its mode and timestamps.

    The merge path (``tools.staging._apply_one``). ``shutil.copy2`` writes **into** the
    destination's own inode, so a kill mid-copy leaves half a file where the user's own file was --
    and that file may hold work they never committed. Here the bytes stream into a sibling and are
    renamed over the target, so the swap is one atomic step; the metadata is applied to the
    temporary *before* the rename, so the file has its final stat the moment it becomes visible.
    """
    target = _prepare(destination)
    temporary = _temporary_sibling(target)
    try:
        with open(source, "rb") as reader, open(temporary, "wb") as writer:
            shutil.copyfileobj(reader, writer, COPY_CHUNK_BYTES)
            writer.flush()
            os.fsync(writer.fileno())
        shutil.copystat(source, temporary)
        _replace_retry(temporary, target)
    except BaseException:
        _discard(temporary)
        raise
    return target
