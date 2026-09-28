"""Workflow sequencing: intent resolution, dispatch and failure handling.

``AgentRegistry.run_agent_workflow`` is now a thin delegation to this module. The
runner owns everything shared by every branch -- clearing the stop flag, resolving an
``[ACTION: ...]`` tag in the message, building the ``WorkflowContext``, emitting the
``workflow_started`` / ``architect_spawn`` preamble, and turning any exception into an
``agent_error`` plus a terminal ``workflow_complete``.

The branches themselves live next door: ``actions_admin`` for the intents the
Architect resolves alone, ``actions_impl`` for the intents that may write code.
"""

import threading
import time
from typing import Any, Callable, Dict, Optional

from orchestration.workflow import actions_admin, actions_impl, events
from agents.model_routing import architect_model
from orchestration.workflow.context import WorkflowContext
from tools.file_tools import get_active_plan_filename, load_plan_state

# Action tags the UI appends to a generic message to request a specific intent.
# Order is significant: the first matching tag wins.
INTENT_TAGS = (
    ("[ACTION: FIX_BUG]", "fix_bug"),
    ("[ACTION: EXECUTE_NEXT_STEP]", "next_step"),
    ("[ACTION: UPDATE_PLAN]", "update_plan"),
    ("[ACTION: ANALYZE_CODEBASE]", "analyze"),
    ("[ACTION: RECOMMEND_NEXT_STEPS]", "recommend"),
    ("[ACTION: NORMALIZE_PLAN]", "normalize"),
)

def resolve_intent(action_type: str, user_message: str) -> str:
    """Maps an ``[ACTION: ...]`` tag in the message onto an explicit intent.

    Only a generic ``custom`` request is auto-detected; an explicit ``action_type``
    is never overridden. The first matching tag wins, preserving the original order.
    """
    if action_type != "custom":
        return action_type
    for tag, resolved in INTENT_TAGS:
        if tag in user_message:
            return resolved
    return action_type


def run_agent_workflow(
    *,
    coder_agents: Dict[str, Dict[str, Any]],
    stop_event: threading.Event,
    user_message: str,
    emit_fn: Callable[[Dict[str, Any]], None],
    action_type: str = "custom",
    action_params: Optional[Dict[str, Any]] = None,
) -> None:
    """Executes an intent-driven agent workflow anchored to plan.json machine state.

    - Evaluates intent: Fix Bug, Next Step, Update Plan, Analyze Code, Recommend, Custom.
    - Administrative Bypass: Executes plan edits, codebase analysis, and recommendations
      directly through the Architect with ZERO Coder spawns / tokens.
    - Delegated Implementation: Spawns a specialized Coder only when physical code must
      be written.
    - Safe Rollbacks: Snapshots deliverables into .deepagents_backups prior to
      modification.
    - Multimodal UI Handling: Identifies [UI] tasks and incorporates vision directives.

    ``stop_event`` must be fresh for this run (the registry creates one per run): it is
    deliberately *not* cleared here, because clearing would resurrect a run that a
    concurrent stop had just flagged. Whichever branch handles the intent is responsible
    for its own terminal event; if a stop makes it return early without one, the runner
    emits the terminal ``workflow_complete`` itself so the UI is never left mid-run.
    """
    if action_params is None:
        action_params = {}

    action_type = resolve_intent(action_type, user_message)

    def should_stop() -> bool:
        return stop_event.is_set()

    # Every terminal event is observed here, so the runner knows whether the branch it
    # dispatched closed the stream. This is what lets a branch return early on a stop
    # without leaving the workflow without an end.
    terminal_seen = {"value": False}

    def emit(event: Dict[str, Any]) -> None:
        if event.get("type") in ("workflow_complete", "workflow_stopped"):
            terminal_seen["value"] = True
        emit_fn(event)

    stream_text = events.make_stream_text(emit, should_stop)

    try:
        plan_file = get_active_plan_filename()
        plan_state = load_plan_state()
        ctx = WorkflowContext(
            emit_fn=emit,
            stream_text=stream_text,
            should_stop=should_stop,
            plan_file=plan_file,
        )

        emit({
            "type": "workflow_started",
            "message": user_message,
            "plan_file": plan_file,
            "action_type": action_type
        })

        emit({
            "type": "architect_spawn",
            "agent": "software-architect",
            "name": "Lead Software Architect",
            "role": "main",
            "model": architect_model()
        })
        time.sleep(0.3)

        if action_type == "update_plan":
            actions_admin.update_plan_action(ctx, plan_state, action_params, user_message)
        elif action_type == "analyze":
            actions_admin.analyze_action(ctx)
        elif action_type == "recommend":
            actions_admin.recommend_action(ctx, plan_state)
        elif action_type == "normalize":
            actions_admin.normalize_plan_action(ctx, plan_state, action_params)
        elif action_type == "fix_bug":
            actions_impl.fix_bug_action(ctx, plan_state, coder_agents, action_params, user_message)
        elif action_type == "next_step":
            actions_impl.next_step_action(ctx, plan_state, coder_agents, action_params)
        else:
            actions_impl.custom_action(ctx, plan_state, coder_agents, user_message)

        # A branch that honoured a stop request returns without its own terminal event.
        # Close the stream here so the UI leaves the running state it was told to leave.
        if not terminal_seen["value"]:
            stopped = should_stop()
            emit({
                "type": "workflow_complete",
                "status": "stopped" if stopped else "finished",
                "message": "Halted by user." if stopped else "Workflow completed."
            })

    except Exception as e:
        emit({
            "type": "agent_error",
            "agent": "software-architect",
            "error": f"Execution error: {str(e)}",
            "can_retry": True
        })
        emit({
            "type": "workflow_complete",
            "status": "error",
            "message": f"Halted due to error: {str(e)}"
        })
