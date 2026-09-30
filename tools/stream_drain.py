"""Bounded stream draining: read a child's output without letting it fill memory.

A child that writes more than the OS pipe buffer (64 KB on Linux) blocks in ``write()`` until
someone reads. So a parent that waits for the child to finish *before* reading deadlocks, and a
parent that reads with ``communicate()`` survives the deadlock but keeps every byte -- a child
that logs 5 MB, or a runaway loop that logs 5 GB, is retained in full.

This module is the middle path: read continuously on a background thread (so the child never
blocks) while retaining only a bounded window.

THE WINDOW
----------
* the **head**, verbatim, up to :data:`HEAD_BYTES` (100 KB);
* the **tail**, a sliding window of at most :data:`TAIL_BYTES` (900 KB);
* everything between, dropped as it arrives.

:data:`LIMIT_BYTES` (1 MB) is therefore a hard ceiling per stream, whatever the child writes.
Both ends are kept on purpose: the head carries the command's opening context, and the tail is
where a crash, a traceback and a summary line live -- a head-only cut would discard exactly the
evidence a failure needs.

A truncated read says so, in the receipt, with the byte count that was dropped
(:data:`TRUNCATION_MARKER`). Silent truncation would make a partial log look complete.
"""

from __future__ import annotations

import threading
from typing import BinaryIO, Tuple

HEAD_BYTES = 100 * 1024
TAIL_BYTES = 900 * 1024
LIMIT_BYTES = HEAD_BYTES + TAIL_BYTES
TRUNCATION_MARKER = b"\n[... TRUNCATED BY ALETH ENGINE ...]\n"
CHUNK_BYTES = 64 * 1024


class DualBuffer:
    """The first ``HEAD_BYTES`` verbatim, plus a sliding tail, capped at ``LIMIT_BYTES``."""

    __slots__ = ("_head", "_tail", "_dropped", "_head_bytes", "_tail_bytes")

    def __init__(self, head_bytes: int = HEAD_BYTES, tail_bytes: int = TAIL_BYTES):
        self._head = bytearray()
        self._tail = bytearray()
        self._head_bytes = int(head_bytes)
        self._tail_bytes = int(tail_bytes)
        self._dropped = 0

    def feed(self, chunk: bytes) -> None:
        """Add a chunk, discarding whatever falls out of the window."""
        if not chunk:
            return
        room = self._head_bytes - len(self._head)
        if room > 0:
            taken = chunk[:room]
            self._head.extend(taken)
            chunk = chunk[room:]
        if not chunk:
            return
        self._tail.extend(chunk)
        excess = len(self._tail) - self._tail_bytes
        if excess > 0:
            del self._tail[:excess]
            self._dropped += excess

    @property
    def dropped(self) -> int:
        """How many bytes the window discarded."""
        return self._dropped

    @property
    def retained(self) -> int:
        """Bytes currently held. Never exceeds ``LIMIT_BYTES``."""
        return len(self._head) + len(self._tail)

    @property
    def truncated(self) -> bool:
        return self._dropped > 0

    def render(self) -> bytes:
        """The receipt: head, a marker naming the dropped count, then the tail."""
        if not self._dropped:
            return bytes(self._head) + bytes(self._tail)
        return (
            bytes(self._head)
            + f"\n[... TRUNCATED BY ALETH ENGINE: {self._dropped} bytes dropped ...]\n".encode("utf-8")
            + bytes(self._tail)
        )


def drain(stream: BinaryIO, buffer: DualBuffer, *, chunk_size: int = CHUNK_BYTES) -> int:
    """Read ``stream`` to EOF into ``buffer``. Returns the total bytes read.

    The whole point is that this is called *while the child runs*, on its own thread, so the
    child's ``write()`` never blocks on a full pipe.
    """
    total = 0
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            return total
        total += len(chunk)
        buffer.feed(chunk)


def drain_text(stream, buffer: DualBuffer, *, chunk_size: int = CHUNK_BYTES) -> int:
    """Drain a *text* stream, encoding each chunk before it enters the window.

    Chunked reads rather than line iteration: a child that writes 20 MB with no newline would be
    buffered whole by a line iterator, which is exactly the flood this module exists to bound.
    ``TextIOWrapper.read(n)`` returns as soon as some characters are available, so it does not
    wait for a terminator.
    """
    total = 0
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            return total
        total += len(chunk)
        buffer.feed(chunk.encode("utf-8", "replace") if isinstance(chunk, str) else chunk)


def drain_async(stream: BinaryIO, buffer: DualBuffer, *, chunk_size: int = CHUNK_BYTES) -> threading.Thread:
    """Start :func:`drain` on a daemon thread and return it."""
    thread = threading.Thread(target=drain, args=(stream, buffer), kwargs={"chunk_size": chunk_size}, daemon=True)
    thread.start()
    return thread


def decode(data: bytes) -> str:
    """The receipt as text, tolerant of a chunk boundary cutting a character in half."""
    return data.decode("utf-8", errors="replace")


def drain_pair(
    stdout: BinaryIO, stderr: BinaryIO
) -> Tuple[DualBuffer, DualBuffer, threading.Thread, threading.Thread]:
    """Drain both streams concurrently -- reading one while the other fills would deadlock."""
    out_buffer, err_buffer = DualBuffer(), DualBuffer()
    out_thread = drain_async(stdout, out_buffer)
    err_thread = drain_async(stderr, err_buffer)
    return out_buffer, err_buffer, out_thread, err_thread
