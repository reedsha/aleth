"""The SQLite state kernel: strict models, WAL, atomic mutations, and DAG edges.

    .\\venv\\Scripts\\python.exe -m pytest tests/test_state_store.py -n0 -q

The edge contract these pin: ``task_dependencies`` is the **source of truth** for topology.
A save reconciles it only on a plan's first write (``sync_edges=True``), and every other
write -- a status change, a legacy-dictionary save -- leaves it alone. Eligibility is a
database query, not a caller's opinion.
"""

import os
import sqlite3
import tempfile
import unittest

from pydantic import ValidationError

from storage import db as sdb


class ModelTests(unittest.TestCase):
    """The state models enforce ``extra="forbid"`` and a closed status set."""

    def test_task_node_forbids_an_unknown_field(self):
        with self.assertRaises(ValidationError):
            sdb.TaskNode(id="task-1", plan_id="PLAN", surprise=1)

    def test_plan_dag_forbids_an_unknown_field(self):
        with self.assertRaises(ValidationError):
            sdb.PlanDAG(plan_id="PLAN", surprise=1)

    def test_a_status_outside_the_four_marks_is_rejected(self):
        with self.assertRaises(ValidationError):
            sdb.TaskNode(id="task-1", plan_id="PLAN", status="bogus")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = sdb.PlanStore(os.path.join(self._tmp.name, "state.db"))

    def _dag(self):
        return sdb.PlanDAG(
            plan_id="PLAN",
            title="T",
            order=["task-1", "task-2"],
            nodes={
                "task-1": sdb.TaskNode(
                    id="task-1", plan_id="PLAN", title="one",
                    section="1. S", section_id="sec-1", order_index=0,
                ),
                "task-2": sdb.TaskNode(
                    id="task-2", plan_id="PLAN", title="two",
                    section="1. S", section_id="sec-1", dependencies=["task-1"],
                    ast_targets=["mod.f"], order_index=1,
                ),
            },
        )

    def _create(self):
        """The plan's first write: the incoming document defines the graph, so edges sync."""
        return self.store.save_dag(self._dag(), sync_edges=True)

    def test_wal_mode_is_enabled(self):
        connection = sqlite3.connect(self.store.path)
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        connection.close()
        self.assertEqual(str(mode).lower(), "wal")

    def test_a_saved_dag_round_trips(self):
        self._create()
        dag = self.store.get_dag("PLAN")
        self.assertEqual(dag.title, "T")
        self.assertEqual(list(dag.nodes), ["task-1", "task-2"])
        self.assertEqual(dag.nodes["task-2"].dependencies, ["task-1"])
        self.assertEqual(dag.nodes["task-2"].ast_targets, ["mod.f"])

    def test_dependencies_live_in_the_mapping_table(self):
        self._create()
        connection = sqlite3.connect(self.store.path)
        rows = connection.execute("SELECT task_id, depends_on FROM task_dependencies").fetchall()
        connection.close()
        self.assertEqual(rows, [("task-2", "task-1")])

    def test_a_write_without_sync_edges_does_not_touch_the_table(self):
        """A save has no opinion about topology unless it is the plan's first."""
        self.store.save_dag(self._dag())
        connection = sqlite3.connect(self.store.path)
        rows = connection.execute("SELECT COUNT(*) FROM task_dependencies").fetchone()[0]
        connection.close()
        self.assertEqual(rows, 0)

    def test_the_projection_carries_metrics_and_sections(self):
        dag = self._dag()
        dag.nodes["task-1"].status = "completed"
        self.store.save_dag(dag, sync_edges=True)
        plan = sdb.dag_to_plan_dict(self.store.get_dag("PLAN"))
        self.assertEqual(plan["metrics"]["total_tasks"], 2)
        self.assertEqual(plan["metrics"]["progress_percent"], 50)
        self.assertEqual([t["id"] for t in plan["steps"]], ["task-1", "task-2"])
        self.assertEqual([s["id"] for s in plan["sections"]], ["sec-1"])

    def test_update_task_status_is_atomic_and_reports_an_unknown_id(self):
        self._create()
        self.assertTrue(
            self.store.update_task_status("PLAN", "task-1", "completed", "done", ["a.py"])
        )
        node = self.store.get_dag("PLAN").nodes["task-1"]
        self.assertEqual(node.status, "completed")
        self.assertIn("done", node.details)
        self.assertEqual(node.files, ["a.py"])
        self.assertFalse(self.store.update_task_status("PLAN", "nope", "completed"))

    def test_topological_order_puts_blockers_first(self):
        self._create()
        self.assertEqual(self.store.topological_order("PLAN"), ["task-1", "task-2"])

    def test_deleting_a_plan_cascades_to_tasks_and_edges(self):
        self._create()
        self.store.delete_plan("PLAN")
        self.assertIsNone(self.store.get_dag("PLAN"))
        connection = sqlite3.connect(self.store.path)
        remaining = connection.execute("SELECT COUNT(*) FROM task_dependencies").fetchone()[0]
        connection.close()
        self.assertEqual(remaining, 0)

    def test_deleting_a_task_cascades_its_edges(self):
        """No dangling edge can survive a task's removal, so eligibility never lies."""
        self._create()
        trimmed = self._dag()
        trimmed.order = ["task-1"]
        trimmed.nodes.pop("task-2")
        self.store.save_dag(trimmed)
        connection = sqlite3.connect(self.store.path)
        remaining = connection.execute("SELECT COUNT(*) FROM task_dependencies").fetchone()[0]
        connection.close()
        self.assertEqual(remaining, 0)

    def test_render_plan_markdown_projects_the_stored_plan(self):
        self._create()
        markdown = self.store.render_plan_markdown("PLAN")
        self.assertIn("one", markdown)
        self.assertIn("two", markdown)

    def test_dag_only_fields_survive_a_legacy_dict_save(self):
        """A save driven by the legacy dictionary must not erase dependencies/ast_targets."""
        self._create()
        legacy = sdb.dag_to_plan_dict(self.store.get_dag("PLAN"))
        legacy["steps"][0]["status"] = "completed"
        rebuilt = sdb.plan_dict_to_dag(legacy, "PLAN")
        sdb.merge_dag_fields(rebuilt, self.store.get_dag("PLAN"))
        self.store.save_dag(rebuilt)
        node = self.store.get_dag("PLAN").nodes["task-2"]
        self.assertEqual(node.dependencies, ["task-1"])
        self.assertEqual(node.ast_targets, ["mod.f"])


class DependencyGraphTests(unittest.TestCase):
    """Eligibility is a query against ``task_dependencies``, and the table is the truth.

    a (completed) -> b (pending) -> c (pending). Nothing runs until its blockers are done.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = sdb.PlanStore(os.path.join(self._tmp.name, "state.db"))
        self._plan()

    def _plan(self):
        self.store.save_dag(
            sdb.PlanDAG(
                plan_id="PLAN",
                title="T",
                order=["a", "b", "c"],
                nodes={
                    "a": sdb.TaskNode(id="a", plan_id="PLAN", title="a", status="completed"),
                    "b": sdb.TaskNode(id="b", plan_id="PLAN", title="b", dependencies=["a"]),
                    "c": sdb.TaskNode(id="c", plan_id="PLAN", title="c", dependencies=["b"]),
                },
            ),
            sync_edges=True,
        )

    def test_eligibility_requires_every_blocker_completed(self):
        self.assertTrue(self.store.is_task_eligible("PLAN", "a"))
        self.assertTrue(self.store.is_task_eligible("PLAN", "b"))   # its blocker is completed
        self.assertFalse(self.store.is_task_eligible("PLAN", "c"))  # its blocker is pending
        self.assertEqual(self.store.blocked_task_ids("PLAN"), ["c"])
        self.assertEqual(self.store.incomplete_blockers("PLAN", "c"), ["b"])

    def test_completing_a_blocker_makes_the_child_eligible(self):
        self.store.update_task_status("PLAN", "b", "completed")
        self.assertTrue(self.store.is_task_eligible("PLAN", "c"))
        self.assertEqual(self.store.blocked_task_ids("PLAN"), [])

    def test_an_explicitly_added_edge_survives_a_status_write(self):
        """The regression this replaced: a status write reconciled edges from node blobs."""
        self.assertTrue(self.store.add_task_dependency("PLAN", "c", "a"))
        self.assertIn("a", self.store.get_dag("PLAN").nodes["c"].dependencies)

        self.store.update_task_status("PLAN", "a", "in_progress")

        self.assertIn("a", self.store.get_dag("PLAN").nodes["c"].dependencies)
        self.assertFalse(self.store.is_task_eligible("PLAN", "c"))

    def test_a_removed_edge_unblocks_the_child(self):
        self.assertTrue(self.store.remove_task_dependency("PLAN", "c", "b"))
        self.assertTrue(self.store.is_task_eligible("PLAN", "c"))

    def test_an_unknown_or_self_edge_is_refused(self):
        self.assertFalse(self.store.add_task_dependency("PLAN", "c", "ghost"))
        self.assertFalse(self.store.add_task_dependency("PLAN", "ghost", "a"))
        self.assertFalse(self.store.add_task_dependency("PLAN", "a", "a"))

    def test_adding_an_existing_edge_is_idempotent(self):
        self.assertTrue(self.store.add_task_dependency("PLAN", "c", "b"))
        self.assertEqual(self.store.get_dag("PLAN").nodes["c"].dependencies, ["b"])


if __name__ == "__main__":
    unittest.main()
