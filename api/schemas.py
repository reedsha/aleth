"""The gateway's wire contract: strict models for every request and response.

The gateway is **unidirectional and typed**. The frontend reads state and submits intents;
nothing else crosses, and every body on both sides is a model with ``extra="forbid"``. An
undocumented key is a 400 on the way in, and impossible on the way out -- the same rule the
outbound bus applies to events (``tools.payloads``), applied to the inbound direction.

``action_params`` is the one deliberate exception: it is the pass-through the action branches
read (``targetTaskId`` and friends), so it is typed as a free mapping rather than guessed at
here. Everything around it is closed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator

from storage.db import TaskStatus

_STRICT = ConfigDict(extra="forbid")

# The intents the engine can act on, and no others. This is the authority: the API validates
# against it so a request naming an intent the engine does not implement is refused at the door
# rather than silently falling through to ``custom``.
#
# ``execute_plan`` is the one intent the *service* owns rather than the runner
# (``orchestration.workflow.runner``): the queue dispatches it to the autonomous loop, which
# decomposes it into the ``next_step`` / ``execute_artifact`` passes the runner does implement.
IntentAction = Literal[
    "custom",
    "fix_bug",
    "next_step",
    "update_plan",
    "analyze",
    "recommend",
    "normalize",
    "execute_artifact",
    "execute_plan",
]


class IntentRequest(BaseModel):
    """One execution signal from the UI.

    ``message`` is the directive text; ``action_type`` names the branch; ``action_params`` carries
    the branch's own arguments (a target task id, an artifact key). Nothing here is trusted to
    execute anything -- it is a *request*, and the queue is where it waits for the orchestrator.

    ``validate_default`` is load-bearing: without it the blank-prompt check below only fires when a
    caller *spells out* ``"message": ""``, and an omitted message sails through as the default --
    the check has to be a property of the request, not of how the JSON was written.
    """

    model_config = ConfigDict(extra="forbid", validate_default=True)

    action_type: IntentAction = "custom"
    message: str = ""
    action_params: Dict[str, Any] = {}

    @field_validator("message")
    @classmethod
    def _message_must_not_be_blank(cls, value: str, info) -> str:
        """The runner refuses a blank prompt, so the door does too -- with one exception.

        Refusing here is the difference between a 400 the frontend can act on and an intent that
        is accepted, queued, and then fails inside the orchestrator with the same complaint.

        ``execute_plan`` is the exception, and it is not a loophole: it is the plan-wide request
        (Phase 18), and the engine resolves the DAG from the store rather than from a directive, so
        there is no prompt for it to carry. Every other intent *is* a prompt.
        """
        text = str(value or "")
        if text.strip():
            return text
        if str(info.data.get("action_type") or "") == "execute_plan":
            return text
        raise ValueError("an intent needs a message; the workflow refuses a blank prompt")


class IntentView(BaseModel):
    """One intent as the queue reports it."""

    model_config = _STRICT

    intent_id: str
    action_type: str
    message: str
    status: str
    error: str = ""
    enqueued_at: float


class IntentLedgerEntry(BaseModel):
    """One row of the durable intent ledger."""

    model_config = _STRICT

    intent_id: str
    action_type: str
    message: str
    status: str
    error: str = ""
    created_at: float
    updated_at: float
    acknowledged_at: float = 0.0
    # What the run has spent (Phase 32). On the wire because the UI is where a user asks what a run
    # cost, and the ledger is the only durable answer.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # The queued steering payload (Phase 33): corrections a paused run has been handed and not yet
    # drained. On the wire so the UI can show that its input is pending rather than lost.
    pending_input: str = "[]"


class IntentQueueStatus(BaseModel):
    """The queue's own state, plus the ledger that outlives it.

    The queue is in memory and dies with the process; the ledger is what a restart reconciles
    against. ``pending_failure`` is the gate the UI reads: while it is set, the user has not yet
    been told that their last run did not do what they asked.
    """

    model_config = _STRICT

    depth: int
    capacity: int
    accepted: int
    completed: int
    failed: int
    items: List[IntentView]
    ledger: List[IntentLedgerEntry] = []
    pending_failure: Optional[IntentLedgerEntry] = None


class AcknowledgeResponse(BaseModel):
    """How many failures the user's acknowledgement cleared."""

    model_config = _STRICT

    acknowledged: int


class HealthResponse(BaseModel):
    """What the gateway will say about itself."""

    model_config = _STRICT

    status: Literal["ok"] = "ok"
    service: str
    version: str
    loopback_only: bool
    read_only: bool
    subscribers: int
    active_plan_id: str


class MetricsView(BaseModel):
    """The progress counters, exactly as the engine computes them."""

    model_config = _STRICT

    total_tasks: int
    completed_tasks: int
    in_progress_tasks: int
    failed_tasks: int
    pending_tasks: int
    progress_percent: int


class TaskView(BaseModel):
    """One task's status and scheduling surface, and nothing else.

    Deliberately not the whole node: the DAG carries fields the UI has no use for, and a wire
    contract that mirrors an internal model is one that changes whenever the model does.
    """

    model_config = _STRICT

    id: str
    title: str
    status: TaskStatus
    section: str
    tag: Optional[str] = None
    dependencies: List[str] = []
    required_capabilities: List[str] = []
    assigned_agent: Optional[str] = None
    files: List[str] = []


class PlanView(BaseModel):
    """The active plan: its tasks, in document order, and its counters."""

    model_config = _STRICT

    plan_id: str
    plan_file: str
    title: str
    goal: str
    updated_at: float
    tasks: List[TaskView]
    metrics: MetricsView


class PlanListResponse(BaseModel):
    """Which plans exist: what the store holds and what the workspace has on disk."""

    model_config = _STRICT

    active_plan_id: str
    active_plan_file: str
    stored_plan_ids: List[str]
    plan_files: List[str]


class TelemetryReceiptView(BaseModel):
    """One append-only ledger row, as written by the exec server."""

    model_config = _STRICT

    execution_id: str
    session_id: str
    target_tool: str
    exit_code: int
    duration_ms: int
    stdout_hash: str
    stderr_hash: str
    outcome: str
    recorded_at: float


class TelemetryListResponse(BaseModel):
    """The newest receipts, bounded, with the ledger's own total for context."""

    model_config = _STRICT

    count: int
    total: int
    receipts: List[TelemetryReceiptView]


class ErrorResponse(BaseModel):
    """Every non-2xx body has this shape, so the frontend has one failure branch."""

    model_config = _STRICT

    error: str
    detail: str = ""


class OperationEnvelope(BaseModel):
    """The one answer shape for the typed operation surface (``api.operations``).

    ``data`` carries the service's own payload -- the dictionaries the UI and the
    characterization suite already speak -- so moving the transport did not invent a second wire
    format for the plan. The envelope is what makes a single client-side failure branch possible.
    """

    model_config = _STRICT

    ok: bool
    data: Any = None
    error: str = ""


class ServiceRefusal(BaseModel):
    """The only shape in which a service may report a failure.

    The client's type guard keys on ``success is False``: that is what it turns into a thrown
    ``ApiError``. A payload that carries an ``error`` without that flag is therefore delivered as
    a *successful* answer and read as one -- the failure is swallowed, and the caller carries on
    with state the backend just said was bad. The gateway validates every answer against this
    model (``api.gateway``), so the anomaly cannot be reintroduced by a future method.

    ``extra`` is allowed on purpose: a refusal carries its own context -- a ``task_id``, an empty
    ``variables`` list, the payload keys the caller was expecting -- and that shape differs per
    operation. What is *not* optional is the flag and the reason.
    """

    model_config = ConfigDict(extra="allow")

    success: Literal[False]
    error: str
