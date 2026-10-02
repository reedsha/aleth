"""Phase 18: the autonomous loop, and the hard ceiling outside the agent's control.

Once an ``execute_plan`` intent is dequeued the engine drives the DAG itself instead of stopping
after one pass: it resolves the next move from the graph, runs it, and repeats. There is no model in
this decision -- the move is read from the same ``tasks`` table the dispatcher reads, in **one
query**, so the loop cannot be talked into a different plan than the one on disk.

Two facts shape the whole design:

* **The human gate is not bypassed.** An artifact that has been planned is *waiting for approval*,
  and a loop that treated that as progress would spin forever re-planning it. ``AWAIT_REVIEW`` is
  therefore a terminal stop, exactly as the directive requires: the engine does everything it can
  unattended and then stops where a person is needed.
* **The ceiling is physical, not advisory.** ``CircuitBreaker`` counts transitions and wall-clock
  independently of the agent, so a hallucinated retry loop hits a wall it cannot negotiate with.
  When it trips the caller fails the intent, and the ledger's failure gate (`storage.intents`)
  keeps the UI from accepting new work until a human acknowledges it.

The driver is injected with three callables -- ``progress``, ``execute_next``, ``plan_next`` -- so
the whole loop is testable with fakes and a fake clock, and the production wiring in ``app.py``
supplies the real store, the real swarm and the real workflow.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import time
from typing import Callable, Optional

from orchestration.scheduler import DEFAULT_MAX_RETRIES

# The hard ceiling on continuous autonomous execution of one plan. Generous enough for a real
# roadmap, small enough that a hallucinated retry loop cannot run for an afternoon.
MAX_TRANSITIONS = 25
# The wall-clock ceiling for one plan's autonomous run. A transition is one unit of engine work.
PLAN_TTL_SECONDS = 900.0
# How long one ``plan_next`` waits for the swarm to settle a dispatch. A planner that hangs past
# this is a fault to report, not a reason to keep the loop alive.
PLAN_DRAIN_SECONDS = 120.0

# -- outcomes -------------------------------------------------------------------------
DONE = "done"
AWAITING_REVIEW = "awaiting_review"
STALLED = "stalled"
FAILED = "failed"
TRIPPED = "tripped"

# -- moves ----------------------------------------------------------------------------
RUN_APPROVED = "run_approved"
PLAN_NEXT = "plan_next"
AWAIT_REVIEW = "await_review"
DEADLOCK = "deadlock"
COMPLETE = "complete"

# One aggregate statement. The counts come back together, so the loop costs one read per
# transition rather than one read per node -- the N+1 the manifest forbids.
#
# ``runnable`` re-uses the scheduler's own eligibility rule (``tasks`` pending, within the retry
# budget, no incomplete blocker) rather than re-deriving it, so the loop and the dispatcher can
# never disagree about what "next" means.
_PROGRESS_SQL = """
SELECT
    COUNT(*) AS total,
    COALESCE(SUM(t.status = 'completed'), 0) AS completed,
    COALESCE(SUM(t.status = 'failed'), 0) AS failed,
    COALESCE(SUM(t.status = 'planned'), 0) AS planned,
    COALESCE(SUM(t.status = 'in_progress'), 0) AS in_progress,
    COALESCE(SUM(t.status = 'pending'), 0) AS pending,
    COALESCE(SUM(
        t.status = 'pending'
        AND t.rejection_attempts <= ?
        AND NOT EXISTS (
            SELECT 1
            FROM task_dependencies td
            JOIN tasks parent
              ON parent.plan_id = td.plan_id AND parent.id = td.depends_on
            WHERE td.plan_id = t.plan_id
              AND td.task_id = t.id
              AND parent.status != 'completed'
        )
    ), 0) AS runnable
FROM tasks t
WHERE t.plan_id = ?
"""


@dataclasses.dataclass(frozen=True)
class PlanProgress:
    """The DAG's shape at one instant, as counts. Every field is a read, never a guess."""

    total: int
    completed: int
    failed: int
    planned: int
    in_progress: int
    pending: int
    # Pending *and* eligible: the nodes a tick could actually dispatch.
    runnable: int

    @property
    def settled(self) -> bool:
        """Nothing is running, waiting for approval, or dispatchable."""
        return self.in_progress == 0 and self.planned == 0 and self.runnable == 0

    @property
    def finished(self) -> bool:
        """Every task in the plan reached ``completed``."""
        return self.total > 0 and self.completed == self.total


@dataclasses.dataclass(frozen=True)
class Move:
    """What the engine should do next, and why it decided that."""

    kind: str
    reason: str


@dataclasses.dataclass(frozen=True)
class DriverReport:
    """How the loop ended: its outcome, how many transitions it took, and the reason."""

    outcome: str
    transitions: int
    reason: str = ""


def _connect(db_path: str) -> sqlite3.Connection:
    # One configured connection (``storage.connection``): WAL, ``synchronous=NORMAL``, a bounded
    # wait and foreign keys. The breaker reads while the engine writes, so the journal mode is what
    # keeps a status poll from colliding with a task write.
    from storage.connection import connect

    return connect(db_path)


def plan_progress(
    db_path: str,
    plan_id: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> PlanProgress:
    """The plan's counts, in one statement. An unknown plan is all zeroes, not an error."""
    connection = _connect(db_path)
    try:
        row = connection.execute(_PROGRESS_SQL, (int(max_retries), str(plan_id))).fetchone()
    finally:
        connection.close()
    if row is None:
        return PlanProgress(0, 0, 0, 0, 0, 0, 0)
    return PlanProgress(
        total=int(row["total"] or 0),
        completed=int(row["completed"] or 0),
        failed=int(row["failed"] or 0),
        planned=int(row["planned"] or 0),
        in_progress=int(row["in_progress"] or 0),
        pending=int(row["pending"] or 0),
        runnable=int(row["runnable"] or 0),
    )


def next_approved_task(db_path: str, plan_id: str) -> str:
    """The approved (``in_progress``) node to execute, in document order, or ``""``.

    Approval is what puts a node in ``in_progress``; a node that is still there was approved while
    a run was live and never got its execution pass, so the loop is the thing that finishes it.
    """
    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT id FROM tasks WHERE plan_id = ? AND status = 'in_progress'"
            " ORDER BY order_index ASC, id ASC LIMIT 1",
            (str(plan_id),),
        ).fetchone()
    finally:
        connection.close()
    return str(row["id"]) if row is not None else ""


def next_move(progress: PlanProgress) -> Move:
    """The next move for the DAG, decided by the store's own counts.

    The order is the order the lifecycle demands: an approved node is drained before anything new
    is planned (so an approval is never starved by fresh work), planning happens before review (so a
    runnable node is not left unplanned because some other node is waiting), and a review stop is
    never mistaken for progress.
    """
    if progress.in_progress > 0:
        return Move(RUN_APPROVED, "an approved task is ready to be executed")
    if progress.runnable > 0:
        return Move(PLAN_NEXT, f"{progress.runnable} runnable task(s) to plan")
    if progress.planned > 0:
        return Move(AWAIT_REVIEW, f"{progress.planned} artifact(s) await human approval")
    if progress.pending > 0:
        return Move(
            DEADLOCK,
            f"{progress.pending} pending task(s) are blocked by work that is not completed",
        )
    if progress.failed > 0:
        return Move(DEADLOCK, f"{progress.failed} task(s) failed and need attention")
    return Move(COMPLETE, "every task is completed")


class CircuitBreaker:
    """The ceiling the agent cannot raise: a transition count and a wall-clock TTL.

    Both are checked *outside* the workflow, so nothing the model says can extend them. ``clock`` is
    injected so a test can prove the TTL without sleeping.
    """

    def __init__(
        self,
        *,
        max_transitions: int = MAX_TRANSITIONS,
        ttl_seconds: float = PLAN_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        if max_transitions < 1:
            raise ValueError("max_transitions must be at least 1")
        self.max_transitions = int(max_transitions)
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._started = float(clock())
        self._transitions = 0

    @property
    def transitions(self) -> int:
        return self._transitions

    @property
    def elapsed(self) -> float:
        return max(0.0, float(self._clock()) - self._started)

    def record(self) -> int:
        """Count one transition and return the new total."""
        self._transitions += 1
        return self._transitions

    def trip(self) -> str:
        """Why the loop must stop, or ``""`` while it may continue."""
        if self._transitions >= self.max_transitions:
            return (
                f"the autonomous run reached its ceiling of {self.max_transitions} transitions "
                f"without finishing the plan"
            )
        if self.elapsed >= self.ttl_seconds:
            return (
                f"the autonomous run exceeded its {self.ttl_seconds:.0f}s time budget "
                f"without finishing the plan"
            )
        return ""


class PlanDriver:
    """Drive a plan forward one transition at a time, under the breaker.

    The three injected callables are the engine, narrowed to what the loop needs:

    * ``progress()`` -> :class:`PlanProgress` -- the graph, read fresh each iteration;
    * ``execute_next()`` -> bool -- apply the approved node, ``False`` when the pass failed;
    * ``plan_next()`` -> bool -- dispatch the runnable nodes to the swarm and wait, ``False`` when
      nothing could be dispatched.

    A ``False`` from either is a hard stop: the engine said it could not do the work, and spinning
    would only spend the user's budget proving it again.
    """

    def __init__(
        self,
        *,
        progress: Callable[[], PlanProgress],
        execute_next: Callable[[], bool],
        plan_next: Callable[[], bool],
        on_transition: Optional[Callable[[int, Move], None]] = None,
        max_transitions: int = MAX_TRANSITIONS,
        ttl_seconds: float = PLAN_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._progress = progress
        self._execute_next = execute_next
        self._plan_next = plan_next
        self._on_transition = on_transition
        self._breaker = CircuitBreaker(
            max_transitions=max_transitions, ttl_seconds=ttl_seconds, clock=clock
        )

    def drive(self) -> DriverReport:
        """Run the loop to a stop. Every exit is an :class:`DriverReport`, never a raise."""
        while True:
            trip = self._breaker.trip()
            if trip:
                return DriverReport(TRIPPED, self._breaker.transitions, trip)

            move = next_move(self._progress())
            if move.kind == COMPLETE:
                return DriverReport(DONE, self._breaker.transitions, move.reason)
            if move.kind == AWAIT_REVIEW:
                return DriverReport(AWAITING_REVIEW, self._breaker.transitions, move.reason)
            if move.kind == DEADLOCK:
                return DriverReport(STALLED, self._breaker.transitions, move.reason)

            if move.kind == RUN_APPROVED:
                ok = bool(self._execute_next())
            else:
                ok = bool(self._plan_next())

            step = self._breaker.record()
            if self._on_transition is not None:
                self._on_transition(step, move)
            if not ok:
                return DriverReport(
                    FAILED,
                    step,
                    f"the engine could not complete the '{move.kind}' step",
                )


__all__ = [
    "AWAITING_REVIEW",
    "AWAIT_REVIEW",
    "COMPLETE",
    "CircuitBreaker",
    "DEADLOCK",
    "DONE",
    "DriverReport",
    "FAILED",
    "MAX_TRANSITIONS",
    "Move",
    "PLAN_DRAIN_SECONDS",
    "PLAN_NEXT",
    "PLAN_TTL_SECONDS",
    "PlanDriver",
    "PlanProgress",
    "RUN_APPROVED",
    "STALLED",
    "TRIPPED",
    "next_approved_task",
    "next_move",
    "plan_progress",
]
