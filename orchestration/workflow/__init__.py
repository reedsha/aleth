"""Workflow-execution internals for the agent registry.

`events` owns the wire format the desktop UI consumes; later steps add the
per-action implementations and the runner that sequences them.
"""

from orchestration.workflow.events import tool_call, tool_result

__all__ = ["tool_call", "tool_result"]
