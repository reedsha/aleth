"""Phase 18: the autonomous loop, and the ceiling outside the agent's control.

Three things are proven here, and they are the three the directive names:

* the loop reads the DAG in **one** query and decides the next move from that, never from a model;
* it stops for a person at exactly one place -- an artifact awaiting approval -- and never mistakes
  that for progress;
* the circuit breaker is physical: a transition cap and a wall-clock TTL, both checked *outside*
  the workflow, so a hallucinated retry loop hits a wall it cannot negotiate with.

The driver is exercised with fakes and a fake clock, so a 15-minute budget is proven without
waiting 15 minutes, and no model, container or process pool is involved.
"""

from __future__ import annotations

import pytest

from orchestration import autonomy
from orchestration.autonomy import (
    AWAITING_REVIEW,
    AWAIT_REVIEW,
    COMPLETE,
    DEADLOCK,
    DONE,
    FAILED,
    PLAN_NEXT,
    RUN_APPROVED,
    STALLED,
    TRIPPED,
    CircuitBreaker,
    PlanDriver,
    PlanProgress,
)
from storage.db import PlanDAG, TaskNode


@pytest.fixture()
def plan(tmp_path, monkeypatch):
    """A real plan database on a throwaway path, like the swarm's own fixture."""
    from storage.db import get_store, reset_stores
    from tools import workspace as workspace_module

    monkeypatch.setattr(workspace_module, "PLAN_DIR", str(tmp_path))
    monkeypatch.setattr(workspace_module, "PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(workspace_module, "ACTIVE_PLAN_FILE", "PLAN.md")
    reset_stores()
    store = get_store()

    def build(tasks):
        """``tasks`` is ``(id, status, [dependencies])`` in document order."""
        store.save_dag(
            PlanDAG(
                plan_id="PLAN",
                title="T",
                plan_file="PLAN.md",
                order=[task[0] for task in tasks],
                nodes={
                    task[0]: TaskNode(
                        id=task[0], plan_id="PLAN", title=task[0],
                        status=task[1], dependencies=list(task[2]),
                    )
                    for task in tasks
                },
            ),
            sync_edges=True,
        )

    yield store, build
    reset_stores()


def _scripted(states):
    """A ``progress`` callable that walks a fixed sequence, then repeats the last state."""
    iterator = iter(states)
    last = states[-1]

    def progress():
        try:
            return next(iterator)
        except StopIteration:
            return last

    return progress


# -- reading the DAG ----------------------------------------------------------------
class TestPlanProgress:
    def test_it_counts_every_state_of_the_plan(self, plan):
        store, build = plan
        build([
            ("a", "completed", []),
            ("b", "planned", []),
            ("c", "in_progress", []),
            ("d", "failed", []),
            ("e", "pending", []),
        ])
        progress = autonomy.plan_progress(store.path, "PLAN")
        assert (progress.total, progress.completed, progress.planned) == (5, 1, 1)
        assert (progress.in_progress, progress.failed, progress.pending) == (1, 1, 1)

    def test_runnable_is_pending_and_unblocked_not_merely_pending(self, plan):
        """A blocked child is *not* runnable, so the loop never spins on work it cannot start."""
        store, build = plan
        build([("a", "planned", []), ("b", "pending", ["a"]), ("c", "pending", [])])
        progress = autonomy.plan_progress(store.path, "PLAN")
        assert progress.pending == 2
        assert progress.runnable == 1, "only the root may be planned"

    def test_the_whole_progress_is_one_query(self, plan):
        """No N+1: the counts come back together, so a transition costs one read, not one per node."""
        store, build = plan
        build([(name, "pending", []) for name in ("a", "b", "c", "d")])

        statements = []
        real_connect = autonomy.sqlite3.connect

        def traced_connect(db_path):
            connection = real_connect(str(db_path), timeout=30)
            connection.row_factory = autonomy.sqlite3.Row
            # The trace callback sees every statement the connection executes, so this counts
            # queries rather than trusting the function to be written the way it claims.
            connection.set_trace_callback(statements.append)
            return connection

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(autonomy, "_connect", traced_connect)
            progress = autonomy.plan_progress(store.path, "PLAN")
        assert progress.total == 4
        assert len(statements) == 1, f"expected one statement, saw {len(statements)}"

    def test_an_unknown_plan_is_zeroes_not_an_error(self, plan):
        store, _build = plan
        assert autonomy.plan_progress(store.path, "NOPE") == PlanProgress(0, 0, 0, 0, 0, 0, 0)

    def test_the_approved_task_is_the_one_in_progress(self, plan):
        store, build = plan
        build([("a", "pending", []), ("b", "in_progress", []), ("c", "planned", [])])
        assert autonomy.next_approved_task(store.path, "PLAN") == "b"

    def test_there_is_no_approved_task_when_none_is_in_progress(self, plan):
        store, build = plan
        build([("a", "pending", []), ("b", "planned", [])])
        assert autonomy.next_approved_task(store.path, "PLAN") == ""


# -- the decision -------------------------------------------------------------------
class TestNextMove:
    def test_an_approved_task_is_executed_first(self):
        move = autonomy.next_move(PlanProgress(3, 0, 0, 1, 1, 1, 1))
        assert move.kind == RUN_APPROVED

    def test_a_runnable_node_is_planned(self):
        assert autonomy.next_move(PlanProgress(3, 0, 0, 0, 0, 3, 2)).kind == PLAN_NEXT

    def test_a_planned_artifact_is_a_stop_for_review(self):
        """The human gate: planned is *waiting*, and treating it as progress is the spin this stops."""
        move = autonomy.next_move(PlanProgress(3, 1, 0, 2, 0, 0, 0))
        assert move.kind == AWAIT_REVIEW
        assert "approval" in move.reason

    def test_pending_but_blocked_work_is_a_deadlock(self):
        assert autonomy.next_move(PlanProgress(3, 1, 0, 0, 0, 2, 0)).kind == DEADLOCK

    def test_failed_work_with_nothing_left_is_a_deadlock(self):
        assert autonomy.next_move(PlanProgress(2, 1, 1, 0, 0, 0, 0)).kind == DEADLOCK

    def test_everything_completed_is_done(self):
        assert autonomy.next_move(PlanProgress(3, 3, 0, 0, 0, 0, 0)).kind == COMPLETE


# -- the ceiling --------------------------------------------------------------------
class TestCircuitBreaker:
    def test_the_transition_cap_trips(self):
        breaker = CircuitBreaker(max_transitions=2, ttl_seconds=10_000)
        assert breaker.trip() == ""
        breaker.record()
        assert breaker.trip() == ""
        breaker.record()
        assert "ceiling of 2 transitions" in breaker.trip()

    def test_the_wall_clock_trips_on_its_own(self):
        now = [0.0]
        breaker = CircuitBreaker(max_transitions=100, ttl_seconds=60.0, clock=lambda: now[0])
        assert breaker.trip() == ""
        breaker.record()
        now[0] = 61.0
        assert "time budget" in breaker.trip()

    def test_a_cap_below_one_is_refused(self):
        with pytest.raises(ValueError):
            CircuitBreaker(max_transitions=0)

    def test_the_no_move_viewer_reports_initial_state(self):
        breaker = CircuitBreaker(max_transitions=5)
        assert breaker.transitions == 0
        assert breaker.elapsed >= 0.0


# -- the loop -----------------------------------------------------------------------
class TestPlanDriver:
    def _driver(self, states, *, execute_next=None, plan_next=None, **kwargs):
        calls = []
        driver = PlanDriver(
            progress=_scripted(states),
            execute_next=execute_next or (lambda: calls.append("execute") or True),
            plan_next=plan_next or (lambda: calls.append("plan") or True),
            **kwargs,
        )
        return driver, calls

    def test_it_plans_then_stops_at_the_review_gate(self):
        states = [PlanProgress(2, 0, 0, 0, 0, 2, 2), PlanProgress(2, 0, 0, 2, 0, 0, 0)]
        driver, calls = self._driver(states)
        report = driver.drive()
        assert report.outcome == AWAITING_REVIEW
        assert report.transitions == 1
        assert calls == ["plan"]

    def test_it_drives_to_completion(self):
        states = [PlanProgress(1, 0, 0, 0, 0, 1, 1), PlanProgress(1, 1, 0, 0, 0, 0, 0)]
        driver, calls = self._driver(states)
        report = driver.drive()
        assert report.outcome == DONE
        assert report.transitions == 1
        assert calls == ["plan"]

    def test_an_approved_task_is_executed_without_intervention(self):
        """The unattended half: an approval the engine still owes gets run, not waited on."""
        states = [PlanProgress(1, 0, 0, 0, 1, 0, 0), PlanProgress(1, 1, 0, 0, 0, 0, 0)]
        driver, calls = self._driver(states)
        report = driver.drive()
        assert report.outcome == DONE
        assert calls == ["execute"]

    def test_an_engine_that_cannot_do_the_step_fails_loudly(self):
        states = [PlanProgress(1, 0, 0, 0, 0, 1, 1)]
        driver, calls = self._driver(states, plan_next=lambda: False)
        report = driver.drive()
        assert report.outcome == FAILED
        assert report.transitions == 1
        assert "plan_next" in report.reason

    def test_stalled_work_ends_the_loop(self):
        states = [PlanProgress(2, 1, 0, 0, 0, 1, 0)]
        driver, calls = self._driver(states)
        report = driver.drive()
        assert report.outcome == STALLED
        assert report.transitions == 0
        assert calls == []

    def test_a_loop_that_never_advances_trips_the_transition_cap(self):
        """A state that never changes cannot spin forever: the cap is outside the agent's reach."""
        progress = lambda: PlanProgress(1, 0, 0, 0, 0, 1, 1)
        calls = []
        driver = PlanDriver(
            progress=progress,
            execute_next=lambda: True,
            plan_next=lambda: calls.append("plan") or True,
            max_transitions=3,
        )
        report = driver.drive()
        assert report.outcome == TRIPPED
        assert report.transitions == 3
        assert len(calls) == 3
        assert "ceiling of 3 transitions" in report.reason

    def test_the_ttl_trips_independently_of_the_transition_cap(self):
        now = [0.0]
        progress = lambda: PlanProgress(1, 0, 0, 0, 0, 1, 1)

        def plan_next():
            now[0] += 100.0
            return True

        driver = PlanDriver(
            progress=progress,
            execute_next=lambda: True,
            plan_next=plan_next,
            max_transitions=100,
            ttl_seconds=250.0,
            clock=lambda: now[0],
        )
        report = driver.drive()
        assert report.outcome == TRIPPED
        assert "time budget" in report.reason
        assert report.transitions == 3

    def test_transitions_are_announced_as_they_happen(self):
        states = [PlanProgress(1, 0, 0, 0, 0, 1, 1), PlanProgress(1, 1, 0, 0, 0, 0, 0)]
        seen = []
        driver = PlanDriver(
            progress=_scripted(states),
            execute_next=lambda: True,
            plan_next=lambda: True,
            on_transition=lambda step, move: seen.append((step, move.kind)),
        )
        driver.drive()
        assert seen == [(1, PLAN_NEXT)]


# -- the service wiring -------------------------------------------------------------
class TestServiceRouting:
    """``execute_plan`` is the loop; everything else is one workflow pass."""

    @staticmethod
    def _intent(action_type):
        from types import SimpleNamespace

        return SimpleNamespace(id="i1", action_type=action_type, message="go", action_params={})

    def test_execute_plan_is_routed_to_the_autonomous_loop(self):
        from app import EngineService

        service = EngineService()
        seen = []
        service._drive_plan = lambda intent: seen.append(intent)
        # The two deterministic phases are not what this test is about: the temp workspace has no
        # manifests, so the gate would (correctly) refuse the run before the routing was observable.
        service._run_setup_phase = lambda: None
        service._verify_shadow = lambda shadow: None
        intent = self._intent("execute_plan")
        service._run_intent(intent)
        assert seen == [intent]

    def test_any_other_intent_is_a_single_pass_and_never_enters_the_loop(self):
        from app import EngineService

        service = EngineService()

        def refuse(_intent):
            raise AssertionError("a one-pass intent must not enter the autonomous loop")

        service._drive_plan = refuse
        service._run_blocking = lambda message, action_type, params, intent_id="": {
            "status": "finished", "message": "",
        }
        service._run_setup_phase = lambda: None
        service._verify_shadow = lambda shadow: None
        service._run_intent(self._intent("next_step"))

    def test_an_unattached_service_refuses_an_intent_rather_than_raising(self):
        from app import EngineService

        service = EngineService()
        assert service.get_intent_status()["success"] is False
        assert service.submit_intent(message="go")["success"] is False
