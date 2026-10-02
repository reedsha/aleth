"""The Artifact Gate: a PLANNED task cannot be executed until it is approved.

Headless, backend-only. Nothing here touches the UI.

    .\\venv\\Scripts\\python.exe -m pytest tests/test_artifact_gate.py -n0 -q
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import bridge_bus
from storage.db import PlanDAG, TaskNode, get_store, reset_stores
from tools import ast_editor, execution_gate, workspace
from tools.execution_gate import StateExecutionError
from tools.payloads import ImplementationPlanArtifact

SOURCE = "def alpha():\n    return 1\n"
REPLACEMENT = "def alpha():\n    return 2\n"


class _Recorder:
    """A transport that records every event the process-wide bus dispatches."""

    def __init__(self):
        self.events = []

    def send(self, json_text):
        self.events.append(json.loads(json_text))

    def types(self):
        return [event.get("type") for event in self.events]


class ArtifactGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="artifact_gate_")
        self._orig_plan_dir = workspace.get_plan_dir()
        self._orig_project = workspace.get_project_dir()
        self._orig_plan = workspace.get_active_plan_filename()
        workspace.PLAN_DIR = self.tmp
        workspace.PROJECT_DIR = self.tmp
        workspace.ACTIVE_PLAN_FILE = "PLAN.md"
        workspace.publish_state_root()
        reset_stores()
        self.addCleanup(self._restore)

        self.source = os.path.join(self.tmp, "mod.py")
        self._write(self.source, SOURCE)

        # A plan with one pending task whose deliverable is mod.py.
        store = get_store()
        store.save_dag(PlanDAG(
            plan_id="PLAN",
            title="Gate",
            order=["task-1"],
            nodes={
                "task-1": TaskNode(
                    id="task-1", plan_id="PLAN", title="Change alpha",
                    section="1. S", section_id="sec-1", files=["mod.py"], order_index=0,
                ),
            },
        ))

        self.recorder = _Recorder()
        bridge_bus.set_transport(self.recorder)

    def _restore(self):
        workspace.PLAN_DIR = self._orig_plan_dir
        workspace.PROJECT_DIR = self._orig_project
        workspace.ACTIVE_PLAN_FILE = self._orig_plan
        reset_stores()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, path, text):
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)

    def _read(self, path):
        with open(path, "r", encoding="utf-8", newline="") as handle:
            return handle.read()

    def _status(self, task_id="task-1"):
        return get_store().get_dag("PLAN").nodes[task_id].status

    def _plan(self):
        execution_gate.plan_artifact(
            plan_id="PLAN",
            task_id="task-1",
            summary="Change alpha to return 2",
            ast_targets=[execution_gate.ASTTarget(
                file_path="mod.py", symbol_name="alpha", byte_range=[0, 0],
                operation="replace", content=REPLACEMENT,
            )],
            estimated_impact="one function",
        )

    # -- the halt ---------------------------------------------------------------

    def test_planning_halts_the_task_in_planned_atomically(self):
        self._plan()
        self.assertEqual(self._status(), "planned")
        self.assertIn("artifact_planned", self.recorder.types())
        self.assertIn("task_state_updated", self.recorder.types())
        planned = [e for e in self.recorder.events if e["type"] == "artifact_planned"][0]
        self.assertEqual(planned["artifact"]["task_id"], "task-1")
        self.assertEqual(planned["artifact"]["ast_targets"][0]["symbol_name"], "alpha")

    def test_a_planned_task_blocks_the_ast_editor(self):
        self._plan()
        result = ast_editor.edit_symbol(
            self.source, "alpha", REPLACEMENT, workspace_dir=self.tmp
        )
        self.assertFalse(result.success)
        self.assertIn("PLANNED", result.error)
        # The file is untouched: the refusal is a boundary, not a warning.
        self.assertEqual(self._read(self.source), SOURCE)

    def test_the_write_boundary_is_blocked_too(self):
        """The gate is enforced at the boundary the write actually crosses.

        The model's ``edit_ast_node`` wrapper is gone -- file edits are MCP tools now -- so
        the thing to pin is the guard itself: a write aimed at a file a ``planned`` task
        targets is refused, whichever caller reaches it. The executor re-checks this per
        target, and the filesystem server enforces the workspace containment underneath it.
        """
        from tools.execution_gate import StateExecutionError, guard_write

        self._plan()
        with self.assertRaises(StateExecutionError) as caught:
            guard_write("mod.py")
        self.assertIn("PLANNED", str(caught.exception))
        # The file is untouched: the refusal is a boundary, not a warning.
        self.assertEqual(self._read(self.source), SOURCE)

    def test_the_executor_refuses_without_approval(self):
        # The primary control flow is the two-phase lifecycle, not write interception: the
        # executor will not apply an artifact whose task has not been approved.
        from orchestration.workflow import executor

        self._plan()
        result = executor.execute_approved("PLAN", "task-1", workspace_dir=self.tmp)
        self.assertFalse(result.success)
        self.assertIn("approval is required", result.error)
        self.assertEqual(self._read(self.source), SOURCE)

    def test_the_executor_injects_the_artifact_payload(self):
        """The artifact IS the payload: what was approved is what is applied."""
        from orchestration.workflow import executor

        self._plan()
        execution_gate.approve_artifact("task-1", "PLAN")
        result = executor.execute_approved("PLAN", "task-1", workspace_dir=self.tmp)
        self.assertTrue(result.success, result.error)
        self.assertIn("return 2", self._read(self.source))
        self.assertNotIn("return 1", self._read(self.source))

    def test_an_ephemeral_node_is_spawned_for_an_untasked_directive(self):
        """DAG supremacy: a directive that touches the filesystem exists in the graph."""
        from orchestration.workflow import planner

        node = planner.ensure_task_for_directive(
            plan_id="PLAN", task=None, title="Fix bug in mod.py", files=["mod.py"]
        )
        self.assertEqual(node["status"], "pending")
        self.assertEqual(node["files"], ["mod.py"])
        self.assertTrue(str(node["id"]).startswith("adhoc-"))
        # It is really in the DAG, and follows the same lifecycle as any other task.
        dag = get_store().get_dag("PLAN")
        self.assertIn(node["id"], dag.nodes)
        self.assertTrue(planner.needs_planning(dag.nodes[node["id"]].model_dump()))

    def test_a_file_no_planned_task_targets_is_not_blocked(self):
        self._plan()
        other = os.path.join(self.tmp, "other.py")
        self._write(other, SOURCE)
        result = ast_editor.edit_symbol(other, "alpha", REPLACEMENT, workspace_dir=self.tmp)
        self.assertTrue(result.success, result.error)

    def test_the_approval_context_releases_the_task_for_its_turn(self):
        self._plan()
        with execution_gate.approval_context("task-1"):
            # Inside the approved turn the write is allowed even though the store still says
            # ``planned``; outside it, it is refused.
            execution_gate.guard_write(self.source)
        with self.assertRaises(StateExecutionError):
            execution_gate.guard_write(self.source)

    # -- the unlock -------------------------------------------------------------

    def test_approving_unlocks_execution(self):
        self._plan()
        result = execution_gate.approve_artifact("task-1", "PLAN")
        self.assertTrue(result["success"], result)
        self.assertEqual(self._status(), "in_progress")
        self.assertIn("artifact_approved", self.recorder.types())

        # Now the splice lands: exactly the node's bytes changed, the rest of the file stays.
        edited = ast_editor.edit_symbol(self.source, "alpha", REPLACEMENT, workspace_dir=self.tmp)
        self.assertTrue(edited.success, edited.error)
        self.assertIn("return 2", self._read(self.source))
        self.assertNotIn("return 1", self._read(self.source))

    def test_approval_emits_approved_then_the_state_change(self):
        self._plan()
        self.recorder.events.clear()
        execution_gate.approve_artifact("task-1", "PLAN")
        self.assertEqual(self.recorder.types(), ["artifact_approved", "task_state_updated"])
        self.assertEqual(self.recorder.events[1]["status"], "in_progress")

    def test_approving_something_that_is_not_planned_is_refused(self):
        # task-1 is still pending: approving it must not run it.
        result = execution_gate.approve_artifact("task-1", "PLAN")
        self.assertFalse(result["success"])
        self.assertIn("not 'planned'", result["error"])
        self.assertEqual(self._status(), "pending")

    def test_approving_an_unknown_task_or_plan_is_refused(self):
        self.assertFalse(execution_gate.approve_artifact("ghost", "PLAN")["success"])
        self.assertFalse(execution_gate.approve_artifact("task-1", "NOPE")["success"])

    # -- contracts --------------------------------------------------------------

    def test_the_artifact_schema_forbids_an_unknown_key(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            ImplementationPlanArtifact(plan_id="PLAN", task_id="task-1", surprise=1)

    def test_the_store_rejects_a_status_outside_the_five_marks(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            TaskNode(id="task-1", plan_id="PLAN", status="bogus")


    # -- the planner (one LLM interaction, and the only one) ---------------------

    def test_planning_uses_the_model_payload(self):
        from orchestration.workflow import planner

        payload = json.dumps({
            "summary": "Change alpha",
            "estimated_impact": "one node",
            # Mandatory on the artifact (Phase 7): a payload without an assessment is refused, which
            # is the contract this test exists to exercise.
            "complexity_score": 3,
            "required_capabilities": ["shell"],
            "ast_targets": [{
                "file_path": "mod.py", "symbol_name": "alpha",
                "byte_range": [0, 13], "operation": "replace", "content": REPLACEMENT,
            }],
        })

        class _Completion:
            text = payload

        with mock.patch("orchestration.system2.is_enabled", return_value=True):
            artifact = planner.plan_task(
                {"id": "task-1", "title": "Change alpha", "files": ["mod.py"]},
                plan_id="PLAN", workspace_dir=self.tmp,
                completer=lambda **kwargs: _Completion(),
            )

        self.assertEqual(artifact.task_id, "task-1")
        self.assertEqual(artifact.ast_targets[0].content, REPLACEMENT)
        self.assertEqual(artifact.ast_targets[0].operation, "replace")

    def test_planning_without_system2_raises_rather_than_falling_back(self):
        # No deterministic crutch: a run that cannot be planned fails, it does not silently
        # execute a template.
        from orchestration.workflow import planner

        with mock.patch("orchestration.system2.is_enabled", return_value=False):
            with self.assertRaises(planner.PlanningUnavailable):
                planner.plan_task(
                    {"id": "task-1", "title": "x", "files": ["mod.py"]},
                    plan_id="PLAN", workspace_dir=self.tmp,
                )


if __name__ == "__main__":
    unittest.main()
