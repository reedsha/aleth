"""The hard ceiling on what one tool result may hand the model.

An agent that runs ``cat build.log`` on a 50 MB log, or reads a generated file with a million lines,
does not get a long answer -- it gets an API token-limit error and a dead run. The context window is
a resource like any other in this engine, so it gets a budget like any other: every string a **tool**
returns to the model is capped, and what was dropped is stated in the result so the model can adapt
instead of assuming it saw everything.

``tools.execution_io.read_source`` is deliberately *not* routed through this. It is the engine's own
reader, and its caller writes the content back (a bug patch, a rebuild), so a truncation there would
be silently written to disk. The cap belongs on the model-facing side: the MCP servers' tool results.
"""

from __future__ import annotations

import os

# 16 KB, counted in **bytes** rather than characters: the budget that matters is the payload, and a
# file of multi-byte characters costs more of it than its character count suggests.
MAX_RESULT_BYTES = 16 * 1024


def truncate_result(text: str, *, max_bytes: int = MAX_RESULT_BYTES) -> str:
    """Cap ``text`` at ``max_bytes`` UTF-8 bytes, keeping both ends and saying what was dropped.

    Middle-truncated on purpose. A head-only slice is the obvious implementation and the wrong one:
    the **tail is where a traceback lives**, so cutting it away would leave the model holding a
    truncated error with no way to read the rest. Head and tail together let it diagnose while still
    being told the middle is gone -- and the marker is explicit so it reaches for ``grep`` or a line
    reader rather than reasoning about a file it never saw.

    The cap counts the marker, because the cap is on the payload the model actually receives. The
    result is a whole number of characters: the budget is met by decoding what fits, not by slicing
    bytes and hoping the cut landed on a character boundary.
    """
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text
    marker = f"\n...[TRUNCATED at {max_bytes // 1024}KB: {len(raw) - max_bytes} bytes omitted]\n"
    budget = max(0, max_bytes - len(marker.encode("utf-8")))
    keep = budget // 2
    head = raw[:keep].decode("utf-8", errors="ignore")
    tail = raw[len(raw) - keep:].decode("utf-8", errors="ignore")
    return f"{head}{marker}{tail}"


def read_file_window(path: str, *, max_bytes: int = MAX_RESULT_BYTES) -> str:
    """A file's text capped at ``max_bytes`` UTF-8 bytes, read through a bounded window.

    :func:`truncate_result` needs only the two ends of the payload, so reading the *whole* file to
    hand it over is memory spent on bytes nobody sees: a multi-gigabyte (or model-generated) file
    OOMed the server before the cap applied. The head and tail windows are seeked for directly and
    the true size comes from ``stat``, so the result is what :func:`truncate_result` would have
    produced -- without ever holding more than twice the budget.
    """
    size = os.path.getsize(path)
    if size <= max_bytes:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
            return handle.read()

    marker = f"\n...[TRUNCATED at {max_bytes // 1024}KB: {size - max_bytes} bytes omitted]\n"
    budget = max(0, max_bytes - len(marker.encode("utf-8")))
    keep = budget // 2
    with open(path, "rb") as handle:
        head_raw = handle.read(keep)
        handle.seek(max(0, size - keep))
        tail_raw = handle.read(keep)
    return (
        head_raw.decode("utf-8", errors="ignore")
        + marker
        + tail_raw.decode("utf-8", errors="ignore")
    )


__all__ = ["MAX_RESULT_BYTES", "truncate_result", "read_file_window"]
