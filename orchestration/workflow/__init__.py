"""Workflow execution internals for the agent registry.

Modules
-------
events          the frontend event wire format (tool_call / tool_result pairs)
context         the per-run dependencies handed down to an action
templates       canned deliverable source written by the coder delegation path
actions_admin   administrative-bypass intents, resolved without a coder
"""

from orchestration.workflow import actions_admin, context, events, templates
from orchestration.workflow.context import WorkflowContext
from orchestration.workflow.events import tool_call, tool_result

__all__ = [
    "actions_admin",
    "context",
    "events",
    "templates",
    "WorkflowContext",
    "tool_call",
    "tool_result",
]
