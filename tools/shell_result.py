"""Parsers for a shell result. No execution, no policy -- just the wire format.

``[Exit Code: N]`` on the first line is the shape every runner produces: the legacy
``the legacy runner``, and the MCP exec server that replaced it. Keeping the parse beside
the format string is what stops a caller's success check from drifting away from the thing it
is checking.

This module is deliberately import-free of anything heavier than ``re``: it is read by the
workflow branches, by the exec server's callers, and by tests, and none of them should pay
for an agent SDK to read an integer out of a header.
"""

from __future__ import annotations

import re
from typing import Optional

# ``run_command_in_workspace`` and the MCP exec server both answer with this header on the
# first line. The block is *never* empty -- even a clean run returns the header plus a note --
# so a caller cannot test success with ``bool(result)``.
_EXIT_CODE_RE = re.compile(r"^\[Exit Code:\s*(-?\d+)\]")


def command_exit_code(result: str) -> Optional[int]:
    """The exit code a shell result reports, or ``None``.

    ``None`` means no exit status was produced at all -- the command timed out, could not
    start, or was refused before it ran. That is *inconclusive*, deliberately distinct from a
    command that actually ran and exited non-zero.
    """
    match = _EXIT_CODE_RE.match((result or "").strip())
    return int(match.group(1)) if match else None


def command_failed(result: str) -> bool:
    """True only when the command ran and exited non-zero.

    An inconclusive result -- no exit status at all -- reports ``False``: a verification gate
    must not read "could not judge" as "failed".
    """
    code = command_exit_code(result)
    return code is not None and code != 0
