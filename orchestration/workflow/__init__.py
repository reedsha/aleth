"""Workflow execution internals for the agent registry.

Modules
-------
events          the frontend event wire format (tool_call / tool_result pairs)
context         the per-run dependencies handed down to an action
templates       canned deliverable source written by the coder delegation path
generation      fills a deliverable's contents from a real System 2 call when configured
ledger          the Living Behavioral Ledger: milestone logs and core-fact wrap-up
actions_admin   administrative-bypass intents, resolved without a coder
actions_impl    implementation intents, which may write code through a coder
execution       the deterministic execution half: apply an approved artifact, no decisions
runner          intent resolution, action dispatch and failure handling
"""

from orchestration.workflow import (
    actions_admin,
    actions_impl,
    context,
    events,
    execution,
    generation,
    ledger,
    runner,
    templates,
)
from orchestration.workflow.context import WorkflowContext
from orchestration.workflow.events import tool_call, tool_result

__all__ = [
    "actions_admin",
    "actions_impl",
    "context",
    "events",
    "execution",
    "generation",
    "ledger",
    "runner",
    "templates",
    "WorkflowContext",
    "tool_call",
    "tool_result",
]
