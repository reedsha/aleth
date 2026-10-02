"""Strict payload contracts: the models, and the ``extra="forbid"`` rule at every boundary.

Two things are pinned here:

* a *real* plan -- this repository's own ``PLAN.md``, parsed by the compiled core and
  compiled back -- round-trips through :mod:`tools.payloads` unchanged; and
* the ``extra="forbid"`` rule actually bites at each modelled boundary, so a JSON
  document carrying an unknown field is rejected rather than silently accepted.

The module is read-only: it parses markdown text in memory and never loads or saves the
real ``plan.json``/``PLAN.md`` (loading can rehydrate the machine state and write it).

    .\\venv\\Scripts\\python.exe -m pytest tests/test_payloads.py -n0 -q
"""

import os
import unittest

from pydantic import BaseModel, ValidationError

from tools import payloads
from tools.payloads import (
    AppendTaskEnvelope,
    AuditExistingDeliverable,
    AuditMissingDeliverable,
    AuditReport,
    BackupEntry,
    CostReportPayload,
    DiffFilePayload,
    DiffTotals,
    LayaClassifierPayload,
    LogEvent,
    MetricsPayload,
    PlanLoadEnvelope,
    PlanMutationResult,
    PlanPayload,
    PlanPrepareEnvelope,
    PlanStructureCounts,
    PlanStructureReport,
    PlanTaskPayload,
    PlanUpdateEnvelope,
    PlanWriteEnvelope,
    PlanWritePairEnvelope,
    RollbackResult,
    SectionPayload,
    SettingsField,
    SettingsPayload,
    StateSummaryPayload,
    SubStepPayload,
    TaskDiffResult,
    ToolCallEvent,
    ToolResultEvent,
    TaskStateUpdatedEvent,
    ArtifactPlannedEvent,
    ArtifactApprovedEvent,
    ASTTarget,
    ImplementationPlanArtifact,
    SummaryPayload,
    WorkspaceFileEntry,
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
    VocabularyPayload,
)
from tools import plan_parser


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# A valid, minimal payload for each model -- enough to validate, so an added unknown key is
# the only reason validation can fail. Kept beside the models so a new boundary is one line
# away from its rejection case.
VALID_PAYLOADS = {
    PlanPayload: {},
    PlanTaskPayload: {},
    SubStepPayload: {},
    SectionPayload: {},
    StateSummaryPayload: {},
    MetricsPayload: {},
    PlanStructureCounts: {},
    PlanStructureReport: {},
    VocabularyPayload: {"canonical": {"UI": "FE"}, "ui_tag": "FE", "ui_keyword_pattern": r"\bui\b"},
    PlanLoadEnvelope: {"plan": {}},
    PlanPrepareEnvelope: {"ok": True},
    PlanUpdateEnvelope: {"found": True, "plan": {}},
    AppendTaskEnvelope: {"ok": True},
    PlanWriteEnvelope: {"ok": True},
    PlanWritePairEnvelope: {"ok": True},
    ToolCallEvent: {"type": "tool_call", "agent": "a", "tool": "t", "args": {}, "description": "d"},
    ToolResultEvent: {"type": "tool_result", "agent": "a", "tool": "t", "result": "r"},
    LogEvent: {"type": "log", "agent": "a", "log_type": "thinking", "text": "line"},
    BackupEntry: {},
    DiffTotals: {},
    DiffFilePayload: {},
    TaskDiffResult: {"success": True},
    AuditMissingDeliverable: {},
    AuditExistingDeliverable: {},
    AuditReport: {"in_sync": True, "discrepancy_count": 0},
    PlanMutationResult: {"success": True},
    RollbackResult: {"success": True},
    payloads.TestTotals: {},
    payloads.TestFileResult: {},
    payloads.TestRunResult: {"success": True},
    SettingsField: {"name": "N", "label": "L", "secret": False, "help": "h", "set": False, "length": 0},
    SettingsPayload: {"success": True, "found": False, "filename": ".env"},
    LayaClassifierPayload: {},
    CostReportPayload: {
        "admin_input": 1, "spawn_turn1_input": 2, "reads_cost": 3, "spawn_total": 4, "turns": 5,
    },
    SummaryPayload: {},
    WorkspaceFileEntry: {},
    WorkflowStartedEvent: {"type": "workflow_started"},
    ArchitectSpawnEvent: {"type": "architect_spawn"},
    CoderSpawnEvent: {"type": "coder_spawn"},
    DelegationEvent: {"type": "delegation"},
    ArchitectSummaryEvent: {"type": "architect_summary"},
    CoderSummaryEvent: {"type": "coder_summary"},
    PlanUpdatedEvent: {"type": "plan_updated"},
    WorkflowCompleteEvent: {"type": "workflow_complete"},
    WorkflowStoppedEvent: {"type": "workflow_stopped"},
    AgentErrorEvent: {"type": "agent_error"},
    AgentsUpdatedEvent: {"type": "agents_updated"},
    WorkspaceChangedEvent: {"type": "workspace_changed"},
    LayaTaggingStartedEvent: {"type": "laya_tagging_started"},
    LayaTaggingProgressEvent: {"type": "laya_tagging_progress"},
    LayaTaggingDoneEvent: {"type": "laya_tagging_done"},
    ConsoleDetachedEvent: {"type": "console_detached"},
    ConsoleDetachFailedEvent: {"type": "console_detach_failed"},
    ConsoleLineEvent: {"type": "console_line"},
    TaskStateUpdatedEvent: {"type": "task_state_updated"},
    ArtifactPlannedEvent: {"type": "artifact_planned"},
    ArtifactApprovedEvent: {"type": "artifact_approved"},
    ASTTarget: {},
    ImplementationPlanArtifact: {"complexity_score": 3, "required_capabilities": []},
}


def _all_models():
    """Every BaseModel defined in :mod:`tools.payloads`."""
    return [
        obj for obj in vars(payloads).values()
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel
    ]


class ModelConfigTests(unittest.TestCase):
    """The rule itself: every model forbids extra fields, and none ignores them."""

    def test_every_model_forbids_extra_and_never_allows_or_ignores(self):
        models = _all_models()
        self.assertTrue(models, "payloads should define at least one model")
        for model in models:
            with self.subTest(model=model.__name__):
                config = model.model_config
                self.assertEqual(config.get("extra"), "forbid")
                self.assertNotIn(config.get("extra"), ("allow", "ignore"))
                self.assertIs(config.get("strict"), False)

    def test_the_rule_is_stated_in_the_module_docstring(self):
        self.assertIn("## Strict Payload Contracts", payloads.__doc__)
        self.assertIn('extra="forbid"', payloads.__doc__)


class UnknownFieldRejectionTests(unittest.TestCase):
    """A document with a field the model does not know is refused at the boundary."""

    def test_every_modelled_boundary_rejects_an_unknown_field(self):
        for model, valid in VALID_PAYLOADS.items():
            with self.subTest(model=model.__name__):
                payload = dict(valid)
                payload["aleth_unexpected_field"] = "surprise"
                with self.assertRaises(ValidationError):
                    model.model_validate(payload)

    def test_a_missing_field_is_allowed_and_takes_its_default(self):
        # The other half of the rule: partial writers (the core's prepare_plan_state, the
        # plan scaffold) are real, so an absent field is a default, not an error.
        plan = PlanPayload.model_validate({})
        self.assertEqual(plan.version, "1.0")
        self.assertIsNone(plan.state_summary)
        self.assertEqual(plan.steps, [])

    def test_an_unknown_field_nested_in_a_task_is_rejected(self):
        with self.assertRaises(ValidationError):
            PlanPayload.model_validate({
                "title": "T",
                "sections": [{"id": "sec-1", "title": "S", "tasks": [
                    {"id": "task-1", "title": "x", "status": "pending", "surprise": True},
                ]}],
            })

    def test_an_unknown_field_nested_in_a_sub_step_is_rejected(self):
        with self.assertRaises(ValidationError):
            PlanTaskPayload.model_validate({
                "id": "task-1",
                "sub_steps": [{"id": "task-1-sub-1", "title": "y", "surprise": True}],
            })

    def test_backup_meta_rejects_an_unknown_entry_field(self):
        with self.assertRaises(ValidationError):
            payloads.BackupMeta.validate_python({
                "main.py": {"action": "modified", "surprise": True},
            })

    def test_backup_meta_accepts_the_real_snapshot_shape(self):
        # Exactly what backup_file_for_task writes, and the sparser shapes the tests seed.
        payloads.BackupMeta.validate_python({
            "main.py": {"action": "modified", "backup": "C:/x/main.py", "timestamp": 1.5},
            "new_file.py": {"action": "created", "backup": None},
            "bare.py": {"action": "created"},
            "actionless.py": {},
        })

    def test_event_envelope_discriminates_on_type(self):
        from pydantic import TypeAdapter

        envelope = TypeAdapter(payloads.EventEnvelope)
        self.assertEqual(envelope.validate_python(
            {"type": "log", "agent": "a", "log_type": "thinking", "text": "t"}
        ).type, "log")
        # A documented event with an extra field is still refused.
        with self.assertRaises(ValidationError):
            envelope.validate_python(
                {"type": "log", "agent": "a", "log_type": "thinking", "text": "t", "extra": 1}
            )


class RealPlanRoundTripTests(unittest.TestCase):
    """A real plan survives the models, and the compiled core, unchanged."""

    def _real_markdown(self):
        path = os.path.join(_REPO_ROOT, "PLAN.md")
        if not os.path.isfile(path):
            self.skipTest("no PLAN.md at the repository root to round-trip")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError as exc:  # a synced filesystem can hold a transient lock
            self.skipTest(f"PLAN.md could not be read right now: {exc}")

    def test_the_real_plan_validates_and_round_trips(self):
        markdown = self._real_markdown()
        plan = plan_parser.parse_markdown_to_plan_dict(markdown, "PLAN.md")

        # It validated at the parse boundary; validate explicitly too, so this test fails
        # loudly if the boundary validation is ever removed.
        validated = PlanPayload.model_validate(plan)
        self.assertTrue(validated.steps or validated.sections, "the real plan carries milestones")

        compiled = plan_parser.compile_plan_json_to_markdown(plan)
        reparsed = PlanPayload.model_validate(plan_parser.parse_markdown_to_plan_dict(compiled, "PLAN.md"))

        # The round trip must converge, field for field, on every milestone.
        self.assertEqual(
            [(t.title, t.status, t.tag, t.details, t.files) for t in validated.steps],
            [(t.title, t.status, t.tag, t.details, t.files) for t in reparsed.steps],
        )
        self.assertEqual(
            validated.state_summary.bullets if validated.state_summary else [],
            reparsed.state_summary.bullets if reparsed.state_summary else [],
        )

    def test_a_plan_with_the_four_status_marks_validates(self):
        from tools import plan_parser as parser

        markdown = (
            "# Project Plan: Marks\n\n## 1. S\n"
            "- [x] done\n- [-] doing\n- [ ] todo\n- [!] failed\n"
        )
        plan = PlanPayload.model_validate(parser.parse_markdown_to_plan_dict(markdown, "PLAN.md"))
        self.assertEqual([t.status for t in plan.steps], ["completed", "in_progress", "pending", "failed"])


class BusVocabularyTests(unittest.TestCase):
    """The outbound bus: one strict union covering the whole event vocabulary."""

    EXPECTED = frozenset({
        "workflow_started", "architect_spawn", "coder_spawn", "delegation",
        "architect_summary", "coder_summary", "plan_updated", "workflow_complete",
        "workflow_stopped", "agent_error", "agents_updated", "workspace_changed",
        "laya_tagging_started", "laya_tagging_progress", "laya_tagging_done",
        "console_detached", "console_detach_failed", "console_line", "task_state_updated",
        "artifact_planned", "artifact_approved", "intent_failed",
        "agent_thought", "tool_execution_start", "tool_execution_complete",
        "token_budget_update", "intent_paused", "intent_steered", "intent_resumed",
        "tool_call", "tool_result", "log",
    })

    def test_the_union_covers_every_emitted_event_type(self):
        self.assertEqual(payloads.BUS_EVENT_TYPES, self.EXPECTED)

    def test_an_unknown_event_type_is_rejected(self):
        with self.assertRaises(ValidationError):
            payloads.validated_bus_event({"type": "not_a_real_event"})

    def test_an_undocumented_key_on_a_real_event_is_rejected(self):
        with self.assertRaises(ValidationError):
            payloads.validated_bus_event(
                {"type": "workflow_started", "plan_file": "PLAN.md", "surprise": 1}
            )

    def test_a_plan_updated_event_validates_its_nested_tree_and_plan_json(self):
        event = {
            "type": "plan_updated",
            "filename": "PLAN.md",
            "content": "",
            "tree": [{"id": "task-1", "title": "x", "status": "pending"}],
            "plan_json": {"title": "T"},
        }
        self.assertIs(payloads.validated_bus_event(event), event)

    def test_an_undocumented_key_nested_in_a_plan_updated_tree_is_rejected(self):
        with self.assertRaises(ValidationError):
            payloads.validated_bus_event({
                "type": "plan_updated",
                "tree": [{"id": "task-1", "title": "x", "surprise": True}],
            })

    def test_the_full_event_envelope_still_discriminates_on_type(self):
        from pydantic import TypeAdapter

        envelope = TypeAdapter(payloads.EventEnvelope)
        self.assertEqual(
            envelope.validate_python({"type": "workflow_complete", "status": "finished"}).type,
            "workflow_complete",
        )


if __name__ == "__main__":
    unittest.main()
