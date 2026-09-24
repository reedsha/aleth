"""The single definition of the frontend event wire format.

Every payload reaches JavaScript through pywebview's `evaluate_js`, and the UI's
`onAgentEvent` handler switches on `type`. The `tool_call` / `tool_result` pair is
animated as one unit (call, pause, result), so both halves must be built here or
the two sides drift apart.

Key order is part of the contract: payloads are JSON-serialised in insertion
order, so these constructors mirror the field order the UI was written against.
"""

from typing import Any, Dict


def tool_call(agent: str, tool: str, args: Dict[str, Any], description: str) -> Dict[str, Any]:
    """A tool invocation, rendered by the UI before its result arrives."""
    return {
        "type": "tool_call",
        "agent": agent,
        "tool": tool,
        "args": args,
        "description": description
    }


def tool_result(agent: str, tool: str, result: str) -> Dict[str, Any]:
    """The outcome paired with the preceding tool_call for the same tool."""
    return {
        "type": "tool_result",
        "agent": agent,
        "tool": tool,
        "result": result
    }
