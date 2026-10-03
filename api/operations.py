"""The typed operation surface: every backend capability the UI may invoke, and nothing else.

The desktop UI used to call these methods directly through the pywebview JS bridge -- which
meant the page could reach *any* backend method, including the ones that execute. This module is
the replacement: a closed table. Each row declares the service method, the verb, the path, and a
strict request model; an operation that is not in this table does not exist as far as the network
is concerned.

Every request is a model with ``extra="forbid"``, so an undocumented field is a 400 rather than
something silently dropped. Every answer is an envelope:

    {"ok": true,  "data": <the service's own payload>}
    {"ok": false, "error": "<why>"}

The envelope is what makes one failure branch possible on the client. The payload inside is the
service's established contract -- the same dictionaries the characterization suite already pins,
so moving the transport did not invent a second wire format for the plan.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable, Dict, List, Optional, Type

from pydantic import BaseModel, ConfigDict

from api.schemas import IntentRequest

_STRICT = ConfigDict(extra="forbid")


# -- request models ------------------------------------------------------------------
class _Empty(BaseModel):
    """An operation that takes no arguments. Declared rather than absent: an empty body is a
    fact about the operation, not a gap in the table."""

    model_config = _STRICT


class AgentPromptRequest(BaseModel):
    model_config = _STRICT
    agent_id: str
    new_prompt: str
    is_custom_only: bool = False


class TaskIdRequest(BaseModel):
    model_config = _STRICT
    task_id: str


class PlanFileRequest(BaseModel):
    model_config = _STRICT
    filename: Optional[str] = None


class PreviewRequest(BaseModel):
    model_config = _STRICT
    filename: Optional[str] = None


class SpanRequest(BaseModel):
    model_config = _STRICT
    file_path: str
    start: int = 0
    end: int = 0


class ArtifactApproveRequest(BaseModel):
    model_config = _STRICT
    task_id: str
    plan_id: Optional[str] = None
    # The run whose artifact this releases. The UI tracks the intent id it was given when it
    # submitted the run, so the approved write joins that run's shadow (Phase 21).
    intent_id: Optional[str] = None


class ArtifactRejectRequest(BaseModel):
    model_config = _STRICT
    task_id: str
    feedback: str = ""
    plan_id: Optional[str] = None


class ArtifactTargetRequest(BaseModel):
    model_config = _STRICT
    task_id: str
    index: int
    content: str
    plan_id: Optional[str] = None


class DependencyRequest(BaseModel):
    model_config = _STRICT
    parent_id: str
    child_id: str
    plan_id: Optional[str] = None


class AddTaskRequest(BaseModel):
    model_config = _STRICT
    title: str


class SetActivePlanRequest(BaseModel):
    model_config = _STRICT
    filename: str


class CreatePlanRequest(BaseModel):
    model_config = _STRICT
    filename: str
    project_idea: str


class ResolveSyncRequest(BaseModel):
    model_config = _STRICT
    resolution_type: str


class RetryRequest(BaseModel):
    model_config = _STRICT
    agent_id: Optional[str] = None
    user_message: Optional[str] = None


class SaveSettingsRequest(BaseModel):
    model_config = _STRICT
    values: Dict[str, Any]


class AcknowledgeRequest(BaseModel):
    """Acknowledge a failed intent, or every unacknowledged one when no id is given."""

    model_config = _STRICT
    intent_id: Optional[str] = None


class InterruptRequest(BaseModel):
    """Hold a running intent for the user (Phase 33)."""

    model_config = _STRICT
    intent_id: str
    note: str = ""


class ResumeRequest(BaseModel):
    """Release a paused intent, carrying the user's correction.

    ``correction`` is what makes this the steering wheel rather than a resume button: it is injected
    into the run's context window as a ``[user]`` message before the loop continues.
    """

    model_config = _STRICT
    intent_id: str
    correction: str = ""


class IntentIdRequest(BaseModel):
    """An operation that names one intent and nothing else. A GET carries it in the query."""

    model_config = _STRICT
    intent_id: str


class RollbackRequest(BaseModel):
    """Rewind a held run to ``step`` (Phase 35).

    The step is the run's own step number, not an index into a UI list: it is what the shadow's
    snapshot tags and the per-step ledger rows are keyed on, so the files, the context window and
    the bill all resolve to the same point.
    """

    model_config = _STRICT
    intent_id: str
    step: int


class ConsoleOpenRequest(BaseModel):
    model_config = _STRICT
    backlog: List[Dict[str, Any]] = []


class ConsoleLineRequest(BaseModel):
    model_config = _STRICT
    kind: str = "log"
    text: str = ""


class WorkspaceStagingRequest(BaseModel):
    """Which shadow to read the delta of. The intent id is required: a shadow maps 1:1 to an
    execution, so a request that cannot name one is a request the engine must refuse."""

    model_config = _STRICT
    intent_id: str


class WorkspaceMergeRequest(BaseModel):
    """Apply the shadow, or (``approve=False``) discard it and leave the host untouched.

    ``intent_id`` is required for the same reason as the diff: a merge that cannot name the run
    that produced the changes would have to guess, and guessing is how the wrong changes land.
    """

    model_config = _STRICT
    intent_id: str
    approve: bool = True


# -- the table -----------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Operation:
    """One capability: a verb, a path, a strict body, and the service method behind it."""

    name: str
    verb: str
    path: str
    request: Optional[Type[BaseModel]]
    call: Callable[[Any, BaseModel], Any]


def _invoke(name: str) -> Callable[[Any, BaseModel], Any]:
    """Call ``service.<name>(**body)``: the model's fields are the method's keyword arguments.

    One shape for every operation on purpose. The typing lives in the *model*, which is strict
    per operation; the call itself is the boring part and does not deserve a helper per verb.
    """

    def call(service: Any, body: BaseModel) -> Any:
        return getattr(service, name)(**body.model_dump())

    return call


OPERATIONS: tuple = (
    # -- reads -------------------------------------------------------------------
    Operation("get_agents", "GET", "/api/agents", _Empty, _invoke("get_agents")),
    Operation("get_workspace_info", "GET", "/api/workspace", _Empty, _invoke("get_workspace_info")),
    Operation("get_active_plan", "GET", "/api/plan/document", _Empty, _invoke("get_active_plan")),
    Operation("get_plan_files", "GET", "/api/plan/files", _Empty, _invoke("get_plan_files")),
    Operation("get_run_state", "GET", "/api/run", _Empty, _invoke("get_run_state")),
    Operation("get_environment_variables", "GET", "/api/env", _Empty, _invoke("get_environment_variables")),
    Operation("get_settings", "GET", "/api/settings", _Empty, _invoke("get_settings")),
    Operation("validate_plan_structure", "GET", "/api/plan/structure", _Empty, _invoke("validate_plan_structure")),
    Operation("audit_codebase_sync", "GET", "/api/audit", _Empty, _invoke("audit_codebase_sync")),
    Operation("get_task_diff", "GET", "/api/task/diff", TaskIdRequest, _invoke("get_task_diff")),
    Operation("get_preview_source", "GET", "/api/preview", PreviewRequest, _invoke("get_preview_source")),
    Operation("extract_plan_steps", "GET", "/api/plan/steps", PlanFileRequest, _invoke("extract_plan_steps")),
    Operation("get_source_span", "GET", "/api/artifact/span", SpanRequest, _invoke("get_source_span")),
    # The merge boundary's read side: the delta between the run's shadow workspace and the live
    # host tree. Nothing is applied by asking; this is what a person reviews before approving.
    Operation("workspace_diff", "GET", "/api/workspace/diff", WorkspaceStagingRequest, _invoke("workspace_diff")),
    # -- mutations ---------------------------------------------------------------
    Operation("save_system_prompt", "POST", "/api/agent/prompt", AgentPromptRequest, _invoke("save_system_prompt")),
    Operation("select_workspace", "POST", "/api/workspace/select", _Empty, _invoke("select_workspace")),
    Operation("add_plan_task", "POST", "/api/plan/task", AddTaskRequest, _invoke("add_plan_task")),
    Operation("revert_plan_update", "POST", "/api/plan/revert", _Empty, _invoke("revert_plan_update")),
    Operation("retag_plan_with_laya", "POST", "/api/plan/retag", _Empty, _invoke("retag_plan_with_laya")),
    Operation("set_active_plan", "POST", "/api/plan/active", SetActivePlanRequest, _invoke("set_active_plan")),
    Operation("normalize_plan", "POST", "/api/plan/normalize", _Empty, _invoke("normalize_plan")),
    Operation("create_plan_file", "POST", "/api/plan/create", CreatePlanRequest, _invoke("create_plan_file")),
    Operation("stop_execution", "POST", "/api/run/stop", _Empty, _invoke("stop_execution")),
    Operation("retry_execution", "POST", "/api/run/retry", RetryRequest, _invoke("retry_execution")),
    Operation("resolve_sync", "POST", "/api/plan/sync", ResolveSyncRequest, _invoke("resolve_sync")),
    Operation("rollback_task", "POST", "/api/task/rollback", TaskIdRequest, _invoke("rollback_task")),
    Operation("run_task_tests", "POST", "/api/task/tests", TaskIdRequest, _invoke("run_task_tests")),
    Operation("save_settings", "POST", "/api/settings", SaveSettingsRequest, _invoke("save_settings")),
    Operation("approve_artifact", "POST", "/api/artifact/approve", ArtifactApproveRequest, _invoke("approve_artifact")),
    # The merge boundary's write side. It is the *only* way a staged run's changes reach the
    # user's files, and it verifies the plan is finished before it does.
    Operation("workspace_merge", "POST", "/api/workspace/merge", WorkspaceMergeRequest, _invoke("workspace_merge")),
    Operation("reject_artifact", "POST", "/api/artifact/reject", ArtifactRejectRequest, _invoke("reject_artifact")),
    Operation("add_task_dependency", "POST", "/api/plan/dependency", DependencyRequest, _invoke("add_task_dependency")),
    Operation("update_artifact_target", "POST", "/api/artifact/target", ArtifactTargetRequest, _invoke("update_artifact_target")),
    Operation("open_console_window", "POST", "/api/console/open", ConsoleOpenRequest, _invoke("open_console_window")),
    Operation("push_console_line", "POST", "/api/console/line", ConsoleLineRequest, _invoke("push_console_line")),
    Operation("ui_ready", "POST", "/api/ui/ready", _Empty, _invoke("ui_ready")),
    Operation("get_console_backlog", "GET", "/api/console/backlog", _Empty, _invoke("get_console_backlog")),
    # -- intents -----------------------------------------------------------------
    # The queue's state and the one mutation that changes it. Both are operations, so the
    # intent surface has no privileged route beside the table and the frontend's client table
    # maps 1:1 onto this one (``tests/test_api_gateway.py::OperationTableTests``).
    Operation("get_intent_status", "GET", "/api/intent/status", _Empty, _invoke("get_intent_status")),
    Operation("submit_intent", "POST", "/api/intent/execute", IntentRequest, _invoke("submit_intent")),
    Operation("acknowledge_intent", "POST", "/api/intent/acknowledge", AcknowledgeRequest, _invoke("acknowledge_intent")),
    # The steering wheel (Phase 33). Interrupting *holds* the run -- nothing failed, a person wants
    # to change its course -- and resuming carries their correction into the context window. Both
    # are operations, so the intent surface still has no privileged route beside the table.
    Operation("interrupt_intent", "POST", "/api/intent/interrupt", InterruptRequest, _invoke("interrupt_intent")),
    Operation("resume_intent", "POST", "/api/intent/resume", ResumeRequest, _invoke("resume_intent")),
    # The rewind (Phase 35). A read of the steps a held run can be rewound to, and the rewind
    # itself. Both are operations, so the intent surface still has no privileged route beside the
    # table -- including the one the directive names with the id in the path.
    Operation("get_intent_steps", "GET", "/api/intent/steps", IntentIdRequest, _invoke("get_intent_steps")),
    Operation("rollback_intent", "POST", "/api/intent/rollback", RollbackRequest, _invoke("rollback_intent")),
    # The extraction gate (Phase 38). The only way staged work reaches the host tree, and the only
    # apply path there is: it is collision-gated and atomic, so ``workspace_merge``'s approve
    # branch delegates here rather than being a second way in.
    Operation("egress_intent", "POST", "/api/intent/egress", WorkspaceStagingRequest, _invoke("egress_intent")),
)


def operation_for(verb: str, path: str) -> Optional[Operation]:
    """The operation a verb and path name, or ``None``. The whole surface, by lookup."""
    wanted = (verb or "").upper()
    for operation in OPERATIONS:
        if operation.path == path and operation.verb == wanted:
            return operation
    return None


def is_known_path(path: str) -> bool:
    """Whether any verb serves this path -- the difference between a 404 and a 405."""
    return any(operation.path == path for operation in OPERATIONS)
