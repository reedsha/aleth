"""The single definition of the frontend event wire format.

Every payload these constructors build is handed to ``bridge_bus`` -- the one typed,
validated channel to the webview -- and the UI's inbound sink dispatches it on ``type``.
The ``tool_call`` / ``tool_result`` pair is animated as one unit (call, pause, result), so
both halves must be built here or the two sides drift apart.

Key order is part of the contract: payloads are JSON-serialised in insertion
order, so these constructors mirror the field order the UI was written against.
"""

import time
from typing import Any, Callable, Dict

from tools.payloads import (
    LogEvent,
    ToolCallEvent,
    ToolResultEvent,
    validated,
)


def tool_call(agent: str, tool: str, args: Dict[str, Any], description: str) -> Dict[str, Any]:
    """A tool invocation, rendered by the UI before its result arrives."""
    return validated(ToolCallEvent, {
        "type": "tool_call",
        "agent": agent,
        "tool": tool,
        "args": args,
        "description": description
    })


def tool_result(agent: str, tool: str, result: str) -> Dict[str, Any]:
    """The outcome paired with the preceding tool_call for the same tool."""
    return validated(ToolResultEvent, {
        "type": "tool_result",
        "agent": agent,
        "tool": tool,
        "result": result
    })


def log_event(agent: str, log_type: str, text: str) -> Dict[str, Any]:
    """One streamed line of agent narration."""
    return validated(LogEvent, {
        "type": "log",
        "agent": agent,
        "log_type": log_type,
        "text": text
    })


def make_stream_text(
    emit_fn: Callable[[Dict[str, Any]], None],
    should_stop: Callable[[], bool],
):
    """Builds the line-at-a-time narrator every workflow branch uses.

    Lines are paced so the UI reads them as live output, and a stop request
    aborts mid-message rather than after the whole message has been emitted.
    """
    def stream_text(agent_id: str, text: str, log_type: str = "thinking", delay: float = 0.02):
        for line in text.split("\n"):
            if should_stop():
                return
            emit_fn(log_event(agent_id, log_type, line + "\n"))
            time.sleep(delay)

    return stream_text
