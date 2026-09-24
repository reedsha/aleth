"""Workflow execution internals for the agent registry.

Modules
-------
events      the frontend event wire format (tool_call / tool_result pairs)
templates   canned deliverable source written by the coder delegation path
"""

from orchestration.workflow import events, templates
from orchestration.workflow.events import tool_call, tool_result

__all__ = [
    "events",
    "templates",
    "tool_call",
    "tool_result",
]
