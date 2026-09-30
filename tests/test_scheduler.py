"""Phase 6 Batch 1: the node-evaluation algorithm.

    .\\venv\\Scripts\\python.exe -m pytest tests/test_scheduler.py -n0 -q

Every test runs a real ``PlanStore`` on a real file and hands the scheduler a real connection, so
the eligibility query is the production one and the edges come from ``task_dependencies`` rather
than from a fixture's idea of them.
"""

from __future__ import annotations

import sqlite3

import pytest

from orchestration.scheduler import (
    get_executable_node_ids,
    get_executable_nodes,
    is_executable,
)
from storage.db import PlanDAG, PlanStore, TaskNode


@pytest.fixture()
def plan(tmp_path):
    """A store, a builder for a plan, and a connection to hand the scheduler."""
    store = PlanStore(str(tmp_path / "state.db"))

    def build(tasks):
        """``tasks`` is a list of ``(id, status, [dependencies])`` in document order."""
        store.save_dag(
            PlanDAG(
                plan_id="PLAN",
                title="T",
                order=[task[0] for task in tasks],
                nodes={
                    task[0]: TaskNode(
                        id=task[0],
                        plan_id="PLAN",
                        title=f"Milestone {task[0]}",
                        status=task[1],
                        dependencies=list(task[2]),
                    )
                    for task in tasks
                },
            ),
            sync_edges=True,
        )

    connection = sqlite3.connect(store.path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield store, build, connection
    finally:
        connection.close()


class TestDependencyGating:
    def test_only_the_root_of_a_chain_is_executable(self, plan):
        _, build, connection = plan
        build([("a", "pending", []), ("b", "pending", ["a"]), ("c", "pending", ["b"])])

        assert get_executable_node_ids(connection, "PLAN") == ["a"]

    def test_completing_a_blocker_releases_the_next_node(self, plan):
        store, build, connection = plan
        build([("a", "pending", []), ("b", "pending", ["a"]), ("c", "pending", ["b"])])

        store.update_task_status("PLAN", "a", "completed")

        assert get_executable_node_ids(connection, "PLAN") == ["b"]

    def test_a_node_whose_blocker_failed_is_not_executable(self, plan):
        """A failed blocker is not a completed one: the child waits until it is addressed."""
        store, build, connection = plan
        build([("a", "pending", []), ("b", "pending", ["a"])])
        store.update_task_status("PLAN", "a", "failed")

        assert get_executable_node_ids(connection, "PLAN") == []

    def test_a_node_whose_blocker_is_only_planned_is_not_executable(self, plan):
        """``planned`` is the Artifact Gate's halted state, not a completion."""
        store, build, connection = plan
        build([("a", "pending", []), ("b", "pending", ["a"])])
        store.update_task_status("PLAN", "a", "planned")

        assert get_executable_node_ids(connection, "PLAN") == []

    def test_roots_with_no_dependencies_are_all_executable(self, plan):
        _, build, connection = plan
        build([("a", "pending", []), ("b", "pending", []), ("c", "pending", [])])

        # The point of the batch: three roots come back together, so a swarm can run them at once.
        assert get_executable_node_ids(connection, "PLAN") == ["a", "b", "c"]

    def test_a_diamond_releases_both_middle_nodes_at_once(self, plan):
        store, build, connection = plan
        build([
            ("root", "pending", []),
            ("left", "pending", ["root"]),
            ("right", "pending", ["root"]),
            ("join", "pending", ["left", "right"]),
        ])

        assert get_executable_node_ids(connection, "PLAN") == ["root"]

        store.update_task_status("PLAN", "root", "completed")

        assert get_executable_node_ids(connection, "PLAN") == ["left", "right"]

    def test_the_join_waits_for_both_branches(self, plan):
        store, build, connection = plan
        build([
            ("root", "pending", []),
            ("left", "pending", ["root"]),
            ("right", "pending", ["root"]),
            ("join", "pending", ["left", "right"]),
        ])
        store.update_task_status("PLAN", "root", "completed")
        store.update_task_status("PLAN", "left", "completed")

        # ``right`` is still pending, so the join is still blocked.
        assert get_executable_node_ids(connection, "PLAN") == ["right"]

        store.update_task_status("PLAN", "right", "completed")

        assert get_executable_node_ids(connection, "PLAN") == ["join"]


class TestStatusFiltering:
    def test_only_pending_nodes_are_returned(self, plan):
        """Completed, failed, planned and in_progress are all *not* runnable."""
        _, build, connection = plan
        build([
            ("done", "completed", []),
            ("failed", "failed", []),
            ("halted", "planned", []),
            ("running", "in_progress", []),
            ("waiting", "pending", []),
        ])

        assert get_executable_node_ids(connection, "PLAN") == ["waiting"]

    def test_a_running_node_is_not_dispatched_twice(self, plan):
        store, build, connection = plan
        build([("a", "pending", []), ("b", "pending", [])])
        store.update_task_status("PLAN", "a", "in_progress")

        assert get_executable_node_ids(connection, "PLAN") == ["b"]


class TestShapeAndOrdering:
    def test_the_nodes_come_back_in_document_order(self, plan):
        _, build, connection = plan
        build([("z", "pending", []), ("m", "pending", []), ("a", "pending", [])])

        assert get_executable_node_ids(connection, "PLAN") == ["z", "m", "a"]

    def test_a_node_reports_the_blockers_it_waited_on(self, plan):
        store, build, connection = plan
        build([("a", "pending", []), ("b", "pending", []), ("c", "pending", ["a", "b"])])
        store.update_task_status("PLAN", "a", "completed")
        store.update_task_status("PLAN", "b", "completed")

        node = get_executable_nodes(connection, "PLAN")[0]

        assert node["id"] == "c"
        assert sorted(node["blockers"]) == ["a", "b"]
        assert node["title"] == "Milestone c"
        assert node["order_index"] == 2

    def test_files_are_decoded_from_the_stored_json(self, plan):
        store, build, connection = plan
        build([("a", "pending", [])])
        connection.execute(
            "UPDATE tasks SET files = ? WHERE plan_id = ? AND id = ?",
            ('["app/models.py", "app/views.py"]', "PLAN", "a"),
        )

        assert get_executable_nodes(connection, "PLAN")[0]["files"] == [
            "app/models.py", "app/views.py",
        ]

    def test_a_malformed_files_column_is_an_empty_list_not_a_crash(self, plan):
        _, build, connection = plan
        build([("a", "pending", [])])
        connection.execute(
            "UPDATE tasks SET files = ? WHERE plan_id = ? AND id = ?", ("not json", "PLAN", "a")
        )

        assert get_executable_nodes(connection, "PLAN")[0]["files"] == []

    def test_an_empty_plan_is_an_empty_set(self, plan):
        _, build, connection = plan
        build([])

        assert get_executable_nodes(connection, "PLAN") == []

    def test_an_unknown_plan_is_an_empty_set(self, plan):
        _, build, connection = plan
        build([("a", "pending", [])])

        assert get_executable_nodes(connection, "NOPE") == []


class TestSingleNodeQuery:
    def test_is_executable_agrees_with_the_set(self, plan):
        store, build, connection = plan
        build([("a", "pending", []), ("b", "pending", ["a"])])

        assert is_executable(connection, "a", "PLAN") is True
        assert is_executable(connection, "b", "PLAN") is False
        assert is_executable(connection, "ghost", "PLAN") is False

        store.update_task_status("PLAN", "a", "completed")

        assert is_executable(connection, "b", "PLAN") is True
        assert is_executable(connection, "a", "PLAN") is False
