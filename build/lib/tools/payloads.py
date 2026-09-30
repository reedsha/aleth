"""Strict, typed contracts for every JSON boundary in the Python backend.

## Strict Payload Contracts

Every dictionary that is *parsed from* or *serialised to* JSON at a module boundary in
this backend is validated against a Pydantic v2 model defined here. A boundary is any
point where a shape stops being owned by the code that produced it: the JSON the
compiled core returns, the ``plan.json`` and ``_meta.json`` files on disk, the event
payloads the bridge hands to the webview, and the tool-result envelopes the workflow
returns.

Every model's ``model_config`` is ``ConfigDict(extra="forbid", strict=False)`` (declared once
as the ``_STRICT`` constant below). That is a hard rule, and it means two things:

* **An unknown field is rejected.** A JSON document that carries a key the model does
  not know raises ``ValidationError`` at the boundary, so a renamed, mistyped or
  invented field cannot quietly ride across it. The permissive ``extra`` policies are
  never used anywhere.
* **A missing field is not an error.** It takes the model's default. This is not
  leniency for its own sake: several real writers legitimately emit a *partial* plan --
  the Rust core's ``prepare_plan_state`` passes keys through untouched, the plan
  scaffold writes tasks with only the fields it knows, and hand-built callers exist -- so
  the defaults here describe reality rather than convenience.

``strict=False`` is deliberate: it keeps Pydantic's ordinary coercions (an ``int``
accepted where a ``float`` is declared, for example) while the ``extra`` rule stays
hard. It is never ``extra`` that is relaxed.

If real data carries a field a model lacks, the fix is to **add that field to the
model** -- typed and documented -- never to relax the model. The contract must describe
exactly what crosses the boundary.

Where a field is genuinely free-form (a caller-echoed id, an opaque tool argument bag)
it is typed ``Any`` on purpose and noted as such; everything else is typed concretely.
"""

from typing import Annotated, Any, Dict, List, Literal, Optional, Union, get_args

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

# The one configuration every boundary model carries. Spelled out on each model below so
# the rule is visible at the model, not only in a base class.
_STRICT = ConfigDict(extra="forbid", strict=False)


def validated(model: type, data: Any) -> Any:
    """Validate ``data`` against ``model`` and return ``data`` unchanged.

    The original object is returned rather than the model instance so a caller keeps the
    exact dict it built -- its key order (part of the event wire contract and of the
    ``plan.json`` a human may read) and any value Pydantic would have coerced. Raising is
    the point: a shape that does not match the contract stops here.
    """
    model.model_validate(data)
    return data


def validated_map(data: Any) -> Any:
    """Validate a ``{str: str}`` mapping (e.g. ``explicit_tags``) and return it unchanged."""
    TypeAdapter(Dict[str, str]).validate_python(data)
    return data


def validated_str_list(data: Any) -> Any:
    """Validate a ``list[str]`` payload and return it unchanged."""
    TypeAdapter(List[str]).validate_python(data)
    return data


def validated_tasks(data: Any) -> Any:
    """Validate a flat ``list[PlanTaskPayload]`` (the ``steps`` view) and return it unchanged."""
    TypeAdapter(List[PlanTaskPayload]).validate_python(data)
    return data


# --------------------------------------------------------------------------- plan tree
#
# The plan dictionary the compiled core emits (``parse_markdown_to_plan_dict``) and the
# one ``plan.json`` stores. Every field carries a default because a plan can be written
# before every field exists: the core's ``prepare_plan_state`` never invents keys, so a
# plan saved from a caller that supplied only ``title``/``sections`` stays that sparse.


class SubStepPayload(BaseModel):
    """One nested checkbox folded into its parent task (never a milestone of its own)."""

    model_config = _STRICT

    id: str = ""
    title: str = ""
    # One of pending / in_progress / completed / failed. Typed ``str`` rather than a
    # Literal because the core's ``update_plan_task`` stores whatever status it is handed
    # verbatim; pinning the four marks would reject a plan the engine itself can write.
    status: str = "pending"
    tag: Optional[str] = None
    details: List[str] = []
    files: List[str] = []
    behavioral_log: List[str] = []


class PlanTaskPayload(BaseModel):
    """A milestone: the unit that carries a checkbox mark and a metric slot."""

    model_config = _STRICT

    id: str = ""
    # The owning section's title, stamped onto the task by the parser. Absent from a
    # sub-step (which inherits its parent's section) -- hence a separate SubStepPayload.
    section: str = ""
    title: str = ""
    status: str = "pending"
    tag: Optional[str] = None
    details: List[str] = []
    files: List[str] = []
    behavioral_log: List[str] = []
    # The ids of the milestones that must complete before this one. The real blocker edges
    # from ``task_dependencies``, carried so the DAG draws the graph the store holds rather
    # than inferring one from document order. Optional on the wire: a plan that declares no
    # dependencies arrives with an empty list, which is the truth.
    dependencies: List[str] = []
    # The tools this milestone's work needs, declared by the Architect **when the milestone is
    # created** -- not discovered by the worker that runs it. The worker's session is scoped to
    # exactly this list, so a node born without a declaration is born without tools. The
    # vocabulary lives in ``orchestration.mcp_session`` (``fs``, ``exec``, ``ast``).
    required_capabilities: List[str] = []
    sub_steps: List[SubStepPayload] = []


class SectionPayload(BaseModel):
    """A ``##`` section and the milestones it owns."""

    model_config = _STRICT

    id: str = ""
    title: str = ""
    tasks: List[PlanTaskPayload] = []


class StateSummaryPayload(BaseModel):
    """The standing Global State Summary: true for every task, so carried into every prompt."""

    model_config = _STRICT

    title: str = ""
    bullets: List[str] = []


class MetricsPayload(BaseModel):
    """Recomputed progress counters stored beside the tree."""

    model_config = _STRICT

    total_tasks: int = 0
    completed_tasks: int = 0
    in_progress_tasks: int = 0
    failed_tasks: int = 0
    pending_tasks: int = 0
    # ``float`` because the percentage is a ratio; an integral value (100) validates fine.
    progress_percent: float = 0.0


class PlanPayload(BaseModel):
    """The whole plan envelope: the same shape ``plan.json`` holds and the core returns."""

    model_config = _STRICT

    version: str = "1.0"
    plan_file: str = ""
    title: str = ""
    # ``None`` is real and meaningful: a plan with no `## 🌍 Global State Summary` header
    # carries no summary, and the parser reports that as null rather than omitting it.
    state_summary: Optional[StateSummaryPayload] = None
    updated_at: float = 0.0
    sections: List[SectionPayload] = []
    # The flat view of every milestone, kept in lock-step with the nested one.
    steps: List[PlanTaskPayload] = []
    metrics: MetricsPayload = Field(default_factory=MetricsPayload)


class VocabularyPayload(BaseModel):
    """The tag vocabulary handed to the core on every parse/compile call."""

    model_config = _STRICT

    canonical: Dict[str, str]
    ui_tag: str
    ui_keyword_pattern: str


class PlanStructureCounts(BaseModel):
    """The tallies inside a structure verdict."""

    model_config = _STRICT

    sections: int = 0
    milestones: int = 0
    bullets: int = 0
    has_title: bool = False


class PlanStructureReport(BaseModel):
    """``check_plan_structure``'s shape verdict: is this document readable as an AST?"""

    model_config = _STRICT

    structured: bool = False
    issues: List[str] = []
    summary: str = ""
    counts: PlanStructureCounts = Field(default_factory=PlanStructureCounts)


# ------------------------------------------------------- implementation artifacts
#
# The Artifact Gate's contracts. System 2 no longer executes blindly: it emits an
# ImplementationPlanArtifact naming the exact AST nodes it intends to change, the task
# halts in ``planned``, and a human approval unlocks execution.


class ASTTarget(BaseModel):
    """One node an artifact proposes to change, by exact byte range, with its payload.

    ``content`` is the exact code the plan intends to inject. The artifact a human approves
    therefore *is* the execution payload: what is reviewed is what is applied, byte for
    byte, with no second generation step and no runtime content map.
    """

    model_config = _STRICT

    file_path: str = ""
    symbol_name: str = ""
    # The exact span in the file (Python slice semantics: end is exclusive).
    byte_range: List[int] = []
    operation: Literal["insert", "replace", "delete"] = "replace"
    # The code to inject at ``byte_range``. Empty for a ``delete``.
    content: str = ""


class ImplementationPlanArtifact(BaseModel):
    """What System 2 intends to do to the code, before it is allowed to do it.

    ``complexity_score`` and ``required_capabilities`` have **no defaults on purpose**. They are the
    Architect's own assessment of the work, and an assessment that can be omitted is one that will
    be: a payload missing them is rejected here rather than silently routed to the cheapest model
    (Phase 7). The engine owns ``plan_id`` and ``task_id``; it does not own these.
    """

    model_config = _STRICT

    plan_id: str = ""
    task_id: str = ""
    summary: str = ""
    ast_targets: List[ASTTarget] = []
    estimated_impact: str = ""
    # 1 is a one-line change; 5 is a system-wide one. Bounded, because a score outside the domain
    # is a routing failure the router would have to refuse later -- better to refuse it here, where
    # the payload that caused it is still in hand.
    complexity_score: int = Field(..., ge=1, le=5)
    # What the work needs beyond a plain code model -- a shell, a browser, a database driver. Free
    # text for now; the router reads the score, not these, but the plan carries them so the
    # dispatch decision has them when it needs them.
    required_capabilities: List[str]


# ----------------------------------------------------------------- plan state envelopes
#
# The wrapper objects the compiled core returns from the dual-sync engine. Their inner
# ``plan`` is a PlanPayload; the flags differ per call. ``plan``/``task`` are Optional so
# the failure branches (``ok: false``) validate too, even though the existing callers
# raise before reading them.


class PlanLoadEnvelope(BaseModel):
    """``core.load_plan_state`` -> the rehydrated plan plus any read error."""

    model_config = _STRICT

    error: str = ""
    plan: PlanPayload


class PlanPrepareEnvelope(BaseModel):
    """``core.prepare_plan_state`` -> the canonicalised plan about to be written."""

    model_config = _STRICT

    ok: bool
    plan: Optional[PlanPayload] = None
    error: str = ""


class PlanUpdateEnvelope(BaseModel):
    """``core.update_plan_task`` -> the plan, and whether the task id/title was found."""

    model_config = _STRICT

    found: bool
    plan: PlanPayload


class AppendTaskEnvelope(BaseModel):
    """``core.append_pending_task`` -> the plan and the task it just created."""

    model_config = _STRICT

    ok: bool
    plan: Optional[PlanPayload] = None
    task: Optional[PlanTaskPayload] = None
    error: str = ""


class PlanWriteEnvelope(BaseModel):
    """``core.write_plan_markdown`` -> where the markdown landed, or why it did not."""

    model_config = _STRICT

    ok: bool
    path: str = ""
    error: str = ""


class PlanWritePairEnvelope(BaseModel):
    """``core.write_plan_pair`` -> whether both representations were written."""

    model_config = _STRICT

    ok: bool
    error: str = ""


# ------------------------------------------------------------------------- event wire
#
# Every payload the backend pushes to the webview. This is the *complete* vocabulary of
# the inbound bus (``bridge_bus``): each event is a strict model with ``extra="forbid"``,
# so an undocumented key on any event crashes the boundary at point-of-entry rather than
# riding across it, and the client mirrors this list in ``ui/js/bridge-bus.js``.
#
# ``orchestration/workflow/events.py`` builds ``tool_call``/``tool_result``/``log``; the
# workflow branches and ``app.py`` build the rest. Field *order* remains the wire
# contract (payloads serialise in insertion order), which is why ``validated_bus_event``
# returns the caller's dict unchanged rather than a re-serialised model.


class SummaryPayload(BaseModel):
    """The ``summary`` object both card summaries carry.

    Every field defaults because the two producers write different subsets: a
    ``coder_summary`` never carries ``proposals`` or ``metrics``; only ``analyze``
    carries ``metrics``; and only the final ``fix_bug`` verdict carries
    ``root_cause``/``fix_spec``. ``extra="forbid"`` still refuses an unknown key.
    """

    model_config = _STRICT

    title: str = ""
    status: str = ""
    files: List[str] = []
    deliverables: List[str] = []
    proposals: List[str] = []
    # The code-metrics blob ``analyze`` attaches, owned by ``tools/code_metrics.py``.
    # Structurally rich and opaque here on purpose, like ``ToolCallEvent.args``.
    metrics: Optional[Dict[str, Any]] = None
    root_cause: str = ""
    fix_spec: str = ""


class WorkspaceFileEntry(BaseModel):
    """One row of the workspace listing (``tools.workspace_io.list_workspace_files``)."""

    model_config = _STRICT

    name: str = ""
    path: str = ""
    size: int = 0
    # The VCS letter ("M"/"U"), or "" when the file is unchanged.
    vcs: str = ""


class ToolCallEvent(BaseModel):
    """A tool invocation, rendered by the UI before its result arrives."""

    model_config = _STRICT

    type: Literal["tool_call"]
    agent: str
    tool: str
    # Opaque, tool-specific argument bag -- intentionally free-form.
    args: Dict[str, Any]
    description: str


class ToolResultEvent(BaseModel):
    """The outcome paired with the preceding tool_call for the same tool."""

    model_config = _STRICT

    type: Literal["tool_result"]
    agent: str
    tool: str
    result: str


class LogEvent(BaseModel):
    """One streamed line of agent narration."""

    model_config = _STRICT

    type: Literal["log"]
    agent: str
    log_type: str
    text: str


class WorkflowStartedEvent(BaseModel):
    """The preamble emitted before any branch runs."""

    model_config = _STRICT

    type: Literal["workflow_started"]
    plan_file: str = ""
    action_type: str = ""


class ArchitectSpawnEvent(BaseModel):
    """The Architect card opening for the run."""

    model_config = _STRICT

    type: Literal["architect_spawn"]
    agent: str = ""
    name: str = ""
    model: str = ""


class CoderSpawnEvent(BaseModel):
    """A Coder card opening for a delegated task."""

    model_config = _STRICT

    type: Literal["coder_spawn"]
    agent: str = ""
    name: str = ""
    model: str = ""


class DelegationEvent(BaseModel):
    """The Architect handing a task to a Coder."""

    model_config = _STRICT

    type: Literal["delegation"]
    from_agent: str = ""
    target_agent: str = ""
    target_name: str = ""
    # ``None`` is real: ``next_step`` passes the task's title, which can be absent.
    task: Optional[str] = None


class ArchitectSummaryEvent(BaseModel):
    """The Architect's verdict card (also the run's stored summary)."""

    model_config = _STRICT

    type: Literal["architect_summary"]
    agent: str = ""
    summary: SummaryPayload = Field(default_factory=SummaryPayload)


class CoderSummaryEvent(BaseModel):
    """A Coder's completion card."""

    model_config = _STRICT

    type: Literal["coder_summary"]
    agent: str = ""
    summary: SummaryPayload = Field(default_factory=SummaryPayload)


class PlanUpdatedEvent(BaseModel):
    """The whole plan tree and its machine state, re-rendered as one unit."""

    model_config = _STRICT

    type: Literal["plan_updated"]
    filename: str = ""
    content: str = ""
    tree: List[PlanTaskPayload] = []
    plan_json: PlanPayload = Field(default_factory=PlanPayload)


class WorkflowCompleteEvent(BaseModel):
    """The terminal event of a run."""

    model_config = _STRICT

    type: Literal["workflow_complete"]
    status: str = ""
    message: str = ""


class WorkflowStoppedEvent(BaseModel):
    """The terminal event of a user halt."""

    model_config = _STRICT

    type: Literal["workflow_stopped"]
    status: str = ""
    message: str = ""


class AgentErrorEvent(BaseModel):
    """One agent's failure, before the run's terminal event."""

    model_config = _STRICT

    type: Literal["agent_error"]
    agent: str = ""
    error: str = ""


class AgentsUpdatedEvent(BaseModel):
    """The agent catalog after a system-prompt edit."""

    model_config = _STRICT

    type: Literal["agents_updated"]
    # The catalog from ``registry.get_agent_summary`` -- structurally rich and opaque
    # here on purpose, like ``ToolCallEvent.args``.
    agents: Dict[str, Any] = {}


class WorkspaceChangedEvent(BaseModel):
    """The workspace was switched, so the file tree and plan list changed."""

    model_config = _STRICT

    type: Literal["workspace_changed"]
    workspace_dir: str = ""
    files: List[WorkspaceFileEntry] = []
    plans: List[str] = []


class LayaTaggingStartedEvent(BaseModel):
    """The zero-token re-tagging pass began."""

    model_config = _STRICT

    type: Literal["laya_tagging_started"]


class LayaTaggingProgressEvent(BaseModel):
    """One step of the re-tagging pass."""

    model_config = _STRICT

    type: Literal["laya_tagging_progress"]
    done: int = 0
    total: int = 0
    title: str = ""


class LayaTaggingDoneEvent(BaseModel):
    """The re-tagging pass finished (``changed``/``engine`` on success, ``error`` on failure)."""

    model_config = _STRICT

    type: Literal["laya_tagging_done"]
    success: bool = False
    changed: List[str] = []
    engine: str = ""
    error: str = ""


class ConsoleDetachedEvent(BaseModel):
    """The detached console window opened."""

    model_config = _STRICT

    type: Literal["console_detached"]


class ConsoleDetachFailedEvent(BaseModel):
    """The detached console window could not be opened."""

    model_config = _STRICT

    type: Literal["console_detach_failed"]
    error: str = ""


class ConsoleLineEvent(BaseModel):
    """One transcript line mirrored into the *detached* console window.

    Emitted only to that second window, never to the main one -- it is the bus
    equivalent of the old ``window.appendLine`` call.
    """

    model_config = _STRICT

    type: Literal["console_line"]
    kind: str = "log"
    text: str = ""


class TaskStateUpdatedEvent(BaseModel):
    """A task's status changed in the store.

    Emitted by the SQLite kernel *after* the mutation's transaction commits, so the UI
    is only ever told about a change the database actually holds.
    """

    model_config = _STRICT

    type: Literal["task_state_updated"]
    plan_id: str = ""
    task_id: str = ""
    status: str = ""


class ArtifactPlannedEvent(BaseModel):
    """System 2 produced an ImplementationPlanArtifact and execution has halted.

    The wire name is ``artifact_planned``; the state it announces is the Artifact Gate's
    ``planned``, which nothing may execute past until an approval arrives.
    """

    model_config = _STRICT

    type: Literal["artifact_planned"]
    artifact: ImplementationPlanArtifact = Field(default_factory=ImplementationPlanArtifact)


class ArtifactApprovedEvent(BaseModel):
    """A human approved an artifact, so the task may now be executed."""

    model_config = _STRICT

    type: Literal["artifact_approved"]
    plan_id: str = ""
    task_id: str = ""


# The event envelope: any event the backend may push, discriminated on ``type``.
EventEnvelope = Annotated[
    Union[
        WorkflowStartedEvent,
        ArchitectSpawnEvent,
        CoderSpawnEvent,
        DelegationEvent,
        ArchitectSummaryEvent,
        CoderSummaryEvent,
        PlanUpdatedEvent,
        WorkflowCompleteEvent,
        WorkflowStoppedEvent,
        AgentErrorEvent,
        AgentsUpdatedEvent,
        WorkspaceChangedEvent,
        LayaTaggingStartedEvent,
        LayaTaggingProgressEvent,
        LayaTaggingDoneEvent,
        ConsoleDetachedEvent,
        ConsoleDetachFailedEvent,
        ConsoleLineEvent,
        TaskStateUpdatedEvent,
        ArtifactPlannedEvent,
        ArtifactApprovedEvent,
        ToolCallEvent,
        ToolResultEvent,
        LogEvent,
    ],
    Field(discriminator="type"),
]

# ``BusEnvelope`` is the name ``bridge_bus`` uses; ``EventEnvelope`` is kept because the
# contract tests and the workflow modules already import it.
BusEnvelope = EventEnvelope

_EVENT_ADAPTER = TypeAdapter(EventEnvelope)


def validated_bus_event(data: Any) -> Any:
    """Validate one outbound bus event against the full union and return it unchanged.

    Raises ``ValidationError`` for an unknown ``type`` or any undocumented key, which is
    the point: the bus must crash at the boundary, not carry a drifted payload across it.
    """
    _EVENT_ADAPTER.validate_python(data)
    return data


def _bus_event_type_names() -> frozenset:
    """Every ``type`` literal in :data:`EventEnvelope` (the client mirrors this set)."""
    names = []
    for member in get_args(get_args(EventEnvelope)[0]):
        names.extend(get_args(member.model_fields["type"].annotation))
    return frozenset(names)


BUS_EVENT_TYPES = _bus_event_type_names()


# --------------------------------------------------------------- tool-result envelopes


class BackupEntry(BaseModel):
    """One file's record in a task's ``_meta.json`` snapshot."""

    model_config = _STRICT

    action: str = "modified"
    # ``None`` for a file the task *created* (there is no prior copy to point at).
    backup: Optional[str] = None
    timestamp: Optional[float] = None


# A whole task snapshot: ``{workspace-relative path: BackupEntry}``. Modelled as a
# ``TypeAdapter`` rather than a ``RootModel`` because Pydantic's ``RootModel`` forbids an
# ``extra`` policy, and the strict rule has to live on the entry itself: every record under
# the mapping is a ``BackupEntry``, which rejects an unknown key.
BackupMeta = TypeAdapter(Dict[str, BackupEntry])


def validated_backup_meta(data: Any) -> Any:
    """Validate a task snapshot mapping and return it unchanged."""
    BackupMeta.validate_python(data)
    return data


class DiffTotals(BaseModel):
    """Line tallies for a task's diff."""

    model_config = _STRICT

    files: int = 0
    added: int = 0
    removed: int = 0


class DiffFilePayload(BaseModel):
    """One file's before/after pair and its unified diff."""

    model_config = _STRICT

    filename: str = ""
    action: str = "modified"
    before: Optional[str] = None
    after: Optional[str] = None
    diff: List[str] = []
    added: int = 0
    removed: int = 0
    available: bool = False


class TaskDiffResult(BaseModel):
    """``recovery.task_diff``'s envelope."""

    model_config = _STRICT

    success: bool
    found: bool = False
    error: str = ""
    # Echoed straight back from the caller, so its type is the caller's.
    task_id: Any = None
    files: List[DiffFilePayload] = []
    totals: DiffTotals = Field(default_factory=DiffTotals)


class AuditMissingDeliverable(BaseModel):
    """A completed task whose declared deliverables are not on disk."""

    model_config = _STRICT

    task_id: Optional[str] = None
    title: Optional[str] = None
    section: Optional[str] = None
    missing_files: List[str] = []


class AuditExistingDeliverable(BaseModel):
    """A pending/in-progress task whose deliverables already exist on disk."""

    model_config = _STRICT

    task_id: Optional[str] = None
    title: Optional[str] = None
    section: Optional[str] = None
    existing_files: List[str] = []


class AuditReport(BaseModel):
    """``recovery.audit_codebase_plan_sync``'s envelope."""

    model_config = _STRICT

    in_sync: bool
    discrepancy_count: int
    completed_missing_files: List[AuditMissingDeliverable] = []
    pending_existing_files: List[AuditExistingDeliverable] = []
    untracked_files: List[str] = []
    summary: str = ""


class PlanMutationResult(BaseModel):
    """Envelope of the sync-resolution and revision-revert tools.

    All of them return a freshly saved plan (``plan_json``), its recompiled markdown
    (``content``) and the flat tree, with an ``action`` naming the mutation.
    """

    model_config = _STRICT

    success: bool
    error: str = ""
    action: str = ""
    modified_tasks: List[str] = []
    plan_json: Optional[PlanPayload] = None
    content: str = ""
    tree: List[PlanTaskPayload] = []


class RollbackResult(BaseModel):
    """``recovery.rollback_task_state``'s envelope."""

    model_config = _STRICT

    success: bool
    error: str = ""
    task_id: Optional[str] = None
    task_title: Optional[str] = None
    restored_files: List[str] = []
    restore_errors: List[str] = []
    plan_json: Optional[PlanPayload] = None
    content: str = ""
    tree: List[PlanTaskPayload] = []


class TestTotals(BaseModel):
    """Aggregated pass/fail/error/skip tallies over a task's test files."""

    model_config = _STRICT

    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0


class TestFileResult(BaseModel):
    """One test file's run outcome."""

    model_config = _STRICT

    filename: str = ""
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    verdict: str = "missing"
    # ``None`` when the runner never produced an exit code (timeout, spawn failure).
    returncode: Optional[int] = None
    output: str = ""
    error: Optional[str] = None


class TestRunResult(BaseModel):
    """``test_runner.run_task_tests``'s envelope."""

    model_config = _STRICT

    success: bool
    found: bool = False
    ran: bool = False
    verdict: str = "none"
    # Echoed from the caller (may be non-str on the rejection path).
    task_id: Any = None
    tests: List[TestFileResult] = []
    totals: TestTotals = Field(default_factory=TestTotals)
    summary: str = ""
    error: str = ""


class SettingsField(BaseModel):
    """One configurable name as the settings panel sees it."""

    model_config = _STRICT

    name: str
    label: str
    secret: bool
    help: str
    set: bool
    length: int
    default: str = ""
    value: str = ""
    effective: str = ""


class SettingsPayload(BaseModel):
    """``settings.read_settings`` / ``save_settings``'s envelope.

    ``saved`` and ``ignored`` are present only on a save; they default to empty here so
    the read reply and the save reply share one contract.
    """

    model_config = _STRICT

    success: bool
    found: bool
    filename: str
    fields: List[SettingsField] = []
    error: str = ""
    saved: List[str] = []
    ignored: List[str] = []


class LayaClassifierPayload(BaseModel):
    """The JSON object the Laya classifier's model is asked to return."""

    model_config = _STRICT

    intent: str = ""
    tag: str = ""


class CostReportPayload(BaseModel):
    """``tools/system2_cost``'s machine-readable cost report."""

    model_config = _STRICT

    admin_input: int
    spawn_turn1_input: int
    reads_cost: int
    spawn_total: int
    turns: int
