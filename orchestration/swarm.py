"""The swarm: a reactive, event-driven DAG dispatcher.

There is no continuous loop and no paused generator. The swarm is a **stateless evaluator** driven
by explicit events: a ``tick()`` dispatches whatever is runnable, and a worker's completion is
delivered by a ``Future`` callback that runs on the *parent's* thread -- which is the only thread
that can speak to the UI.

Why a callback rather than polling the database: the parent owns the ``Future`` returned by
``executor.submit``, so ``add_done_callback`` is an exact notification with no diffing, no
``is_announced`` column, and no schema polluted with ephemeral UI-notification state. It is also
the only correct place to emit: a worker runs in another interpreter with its own bridge bus, so an
event raised there would never reach the window.

Crash resilience follows from that design rather than from bookkeeping:

* a node is ``planned`` only after ``plan_artifact`` commits the artifact *and* the state together,
  so a crash leaves an approvable node the UI renders on reboot;
* a crash mid-generation leaves the node ``pending``, and the next ``tick()`` re-dispatches it;
* the retry bound is persisted on the node (``rejection_attempts``), so a restart cannot resurrect
  the rejection loop.

Nothing about a node's history lives in this object. ``_in_flight`` is the only in-memory state,
and it is exactly what the process currently has running -- losing it costs a re-dispatch, not a
correctness guarantee.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set

from orchestration.model_router import TaskUnroutable
from orchestration.scheduler import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_MAX_SYSTEM_RETRIES,
    DEFAULT_MAX_WORKERS,
    get_executable_nodes,
)
from orchestration.worker import execute_node, role_payload

# The exception types that mean "the machine failed", as opposed to "the work is wrong". Kept as
# a tuple so ``isinstance`` is one call, and deliberately narrow: anything not listed is treated as
# the work's problem, because silently retrying a contract violation forever is the failure mode
# this segregation exists to prevent.
SYSTEM_FAILURE_TYPES = (
    BrokenPipeError,
    ConnectionError,
    ConnectionResetError,
    TimeoutError,
    EOFError,
    MemoryError,
    OSError,
)


def is_system_failure(error: str) -> bool:
    """Whether a worker's reported error is the environment's fault rather than the work's.

    The worker sends its exception as ``"TypeName: message"``, because an exception object cannot
    travel through a ``Future`` across processes. The type name is matched against the system
    failures above, plus the two that arrive as text rather than as an exception type in a child:
    an unreadable MCP reply (``JSONDecodeError``/``ValueError`` from the protocol layer) and an
    explicit protocol timeout.
    """
    text = str(error or "")
    name = text.split(":", 1)[0].strip()
    if name in {kind.__name__ for kind in SYSTEM_FAILURE_TYPES}:
        return True
    return name in {"JSONDecodeError", "MCPError"} or "timed out" in text.lower()


# A node that required this capability ran a command, so it changed something and must prove it.
VERIFICATION_CAPABILITY = "exec"

# The message an unverified node is re-queued with. The wording is the contract the engine, the
# stored rejection note and the UI all share.
UNVERIFIED_MESSAGE = (
    "Task unverified: Tests must be executed via test_runner and return exit_code 0"
)


def verification_refusal(
    verdict: Mapping[str, Any], required_capabilities: Sequence[str]
) -> Optional[str]:
    """Why a task may not be marked ``completed``, or ``None`` when it may.

    The engine gate (Phase 7.5). A node that required ``exec`` ran a command -- it *did*
    something -- so it must carry a ``tools/test_runner.py`` receipt whose exit code is 0 before
    its work can be trusted. The distinctions that matter:

    * **No receipt is not a pass.** "No test file was recorded" and "the suite could not be
      collected" are the *absence* of evidence. Accepting them is exactly the false-success
      failure this codebase has already been audited for.
    * **A failing receipt is not a pass.** One file exiting 0 while another failed is a failure.
    * **A node that ran nothing is unaffected.** Without ``exec`` there is nothing to verify, so
      the gate stays out of the way -- an administrative or planning-only node is not held to a
      test it could not write.

    The return value is the *detail*; the caller composes it into the re-queue note
    (:data:`UNVERIFIED_MESSAGE`), so the wording the user sees is decided in one place.
    """
    capabilities = {str(name) for name in (required_capabilities or [])}
    if VERIFICATION_CAPABILITY not in capabilities:
        return None

    receipts = [entry for entry in (verdict.get("tests") or []) if isinstance(entry, Mapping)]
    if not receipts:
        return (
            "no test receipt was recorded: a task that ran commands must have a test file that "
            "ran and exited 0"
        )

    passing = [entry for entry in receipts if entry.get("returncode") == 0]
    if not passing:
        detail = "; ".join(
            f"{entry.get('filename')}: {entry.get('verdict')}" for entry in receipts
        )
        return f"no test receipt exited 0 ({detail})"

    if str(verdict.get("verdict") or "") in ("failed", "error"):
        return (
            f"the recorded tests did not pass "
            f"({verdict.get('summary') or verdict.get('verdict')})"
        )
    return None


@dataclass
class NodeOutcome:
    """One worker's result: its artifact, or why there is none."""

    task_id: str
    node_id: str
    artifact: Any = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.artifact is not None and not self.error


class Swarm:
    """Dispatches runnable nodes to a process pool and reports finished ones to the UI."""

    def __init__(
        self,
        *,
        db_path: str,
        plan_id: str,
        workspace_dir: str,
        max_workers: int = DEFAULT_MAX_WORKERS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_system_retries: int = DEFAULT_MAX_SYSTEM_RETRIES,
        worker: Callable[..., Any] = execute_node,
        planner_spec: Optional[str] = None,
    ):
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        if max_retries < 1:
            raise ValueError("max_retries must be at least 1")
        if max_system_retries < 1:
            raise ValueError("max_system_retries must be at least 1")
        self.db_path = str(db_path)
        self.plan_id = str(plan_id)
        self.workspace_dir = str(workspace_dir)
        self.max_workers = int(max_workers)
        self.max_retries = int(max_retries)
        self.max_system_retries = int(max_system_retries)
        self.worker = worker
        self.planner_spec = planner_spec

        self._executor: Optional[ProcessPoolExecutor] = None
        # Set by ``close()``. A worker's callback ends by calling ``tick()``, and that callback can
        # land after shutdown has begun -- without this the trailing tick either schedules on a
        # dead pool or silently builds a new one that nothing will ever close.
        self._closed = False
        # The nodes this process currently has running. The only in-memory state, and the only
        # thing a crash can lose -- which costs a re-dispatch, not a guarantee.
        self._in_flight: Set[str] = set()
        # How many callbacks are currently applying an outcome. ``in_flight`` alone is not enough to
        # say the swarm is idle: a callback clears its node and *then* commits, so an observer that
        # only watched ``in_flight`` would see an idle swarm while an outcome was still being
        # written. This is the counter that makes "settled" mean settled.
        self._busy = 0
        # The last outcome per node, so the UI can hand a rejection back for a node it is showing.
        self._outcomes: Dict[str, NodeOutcome] = {}

    # -- helpers -----------------------------------------------------------------
    def _connection(self) -> sqlite3.Connection:
        """A read connection to the plan database.

        The parent reads through the path (the worker needs a *path*, not a connection, because it
        runs in another process) while writes go through the process-wide store, which is the same
        file in production. ``get_store()`` is used for the writes so the Artifact Gate's own lock
        and transaction discipline apply.
        """
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _descriptor_for(self, node: Dict[str, Any]) -> Dict[str, Any]:
        """The role payload for a node, with its prompt, critique and *route* snapshotted now.

        The router picks the role from the node's title and tag -- the same zero-token decision the
        sequential path made -- and the human's rejection notes ride along, so a retry is planned
        against a different question than the attempt that was refused.

        The model is **not** the role's default: it is the router's decision for this node, from the
        complexity the Architect assessed and how many times a human has refused the result. Both
        values -- and the critique -- arrive already hydrated on ``node``, read by the dispatch
        query rather than by a scalar SELECT here, so routing adds no per-node round trip to the
        tick. An unusable assessment routes to the top tier and is logged, never silently downgraded.

        The node's ``required_capabilities`` travels the same way, so the worker's session is scoped
        to what the node declared and the model is handed no more tools than that.
        """
        from agents import laya as laya_gate
        from agents.coders import ROLE_BY_NAME
        from orchestration.model_router import resolve_endpoint, route_or_top

        coder_id = laya_gate.route_coder(str(node.get("title") or ""), tag=node.get("tag"))
        role = ROLE_BY_NAME.get(coder_id) or next(iter(ROLE_BY_NAME.values()))
        routed = route_or_top(node.get("complexity_score"), node.get("rejection_attempts"))
        # The policy ladder says how strong the work needs the model to be; the bootloader's
        # fallback matrix says which configured endpoint that is. This can refuse the node
        # outright when the fleet is too weak for it -- ``tick`` catches that and fails the node.
        endpoint = resolve_endpoint(routed)
        return role_payload(
            role,
            plan_id=self.plan_id,
            workspace_dir=self.workspace_dir,
            planner_spec=self.planner_spec,
            rejection_feedback=node.get("rejection_feedback") or [],
            # The route, not the model name: the System 2 client expects ``provider:model`` and
            # strips the prefix itself. The whole decision travels so the child can log *why*.
            model=endpoint["route"],
            routing={**routed, **endpoint},
            # What the node says it needs, also hydrated by the dispatch query. The child scopes
            # its session to exactly this, so an empty declaration is enforced as "no tools".
            capabilities=node.get("required_capabilities") or [],
        )

    # -- the reactive loop -------------------------------------------------------
    def start(self) -> "Swarm":
        """Bring the pool up and take one tick. The cold-start entry point."""
        self.tick()
        return self

    def tick(self) -> List[str]:
        """Dispatch every runnable node not already in flight. Fires and forgets.

        Returns the ids it submitted, which is what a test asserts on. A node that is executable
        but already in flight is skipped -- that is the whole of the de-duplication, because the
        pool holds the truth about what is running.
        """
        if self._closed:
            # Closed while a callback was still in flight. Dispatching now would either raise on a
            # shut-down pool or leak a new one, so a tick after close is inert by definition.
            return []
        if self._executor is None:
            self._executor = ProcessPoolExecutor(max_workers=self.max_workers)

        connection = self._connection()
        try:
            # The retry bound is enforced by the query: a node a human has rejected to its limit is
            # excluded here and never reaches the pool.
            nodes = get_executable_nodes(connection, self.plan_id, max_retries=self.max_retries)
        finally:
            connection.close()

        dispatched: List[str] = []
        for node in nodes:
            node_id = node["id"]
            if node_id in self._in_flight:
                continue
            try:
                descriptor = self._descriptor_for(node)
            except TaskUnroutable as error:
                # The fleet cannot run this node, and no retry will change that: it is a
                # configuration fact, not a transient fault. Failed once, with the reason, rather
                # than dispatched into a model that cannot do the work.
                self._fail_unroutable(node_id, error)
                continue
            future = self._executor.submit(
                self.worker,
                f"{self.plan_id}::{node_id}",
                descriptor,
                self.db_path,
            )
            self._in_flight.add(node_id)
            # Bound by default argument: the callback fires long after this loop has moved on.
            future.add_done_callback(lambda done, nid=node_id: self._on_worker_complete(nid, done))
            dispatched.append(node_id)
        return dispatched

    def _fail_unroutable(self, node_id: str, error: Exception) -> None:
        """Mark a node the fleet cannot run as failed, with the reason on the node.

        Its own short transaction, like every other parent-side write: no transaction is held
        across the tick, and a failure to record the reason must not abort the dispatch of the
        nodes that *can* run.
        """
        from storage.db import get_store

        try:
            get_store().update_task_status(
                self.plan_id, node_id, "failed", detail_note=f"Unroutable: {error}"
            )
        except Exception:
            pass

    def _on_worker_complete(self, node_id: str, future: Future) -> None:
        """A worker finished. Runs on the **parent's** thread, which is why it can emit.

        This is the only place a finished artifact is announced, and the announcement is a side
        effect of ``commit``: ``execution_gate.plan_artifact`` writes the artifact and the ``planned``
        status in one transaction and then emits ``artifact_planned`` + ``task_state_updated`` on
        the process-wide bus, which is bound to the window. A worker cannot do that from its own
        interpreter, and a poller would need state the schema should not carry.

        **This method must never raise.** A ``Future`` callback runs on the pool's own thread and
        nothing above it can catch: ``concurrent.futures`` prints the traceback and loses it. A
        swarm whose database has gone -- a test's temporary directory, a process shutting down --
        would otherwise spray unhandled tracebacks into teardown. Failures are reported to stderr;
        the completion itself is already recorded in ``_outcomes``.
        """
        self._busy += 1
        try:
            self._in_flight.discard(node_id)
            outcome = NodeOutcome(f"{self.plan_id}::{node_id}", node_id)
            try:
                outcome.artifact = future.result()
            except Exception as error:
                outcome.error = f"{type(error).__name__}: {error}"
            self._outcomes[node_id] = outcome

            if outcome.ok:
                self.commit(outcome)
            else:
                self._handle_failure(node_id, outcome)
        except Exception as error:
            print(
                f"[Swarm] outcome for {node_id} could not be applied: "
                f"{type(error).__name__}: {error}",
                file=sys.stderr,
            )
        finally:
            # The node stays "in flight" until its outcome has been *applied*. Clearing it first --
            # which is the obvious order -- lets an observer (or the tick below) see the node as free
            # while its commit is still running, and the node is then dispatched a second time.
            self._in_flight.discard(node_id)
            self._busy -= 1

        # Whatever this completion released -- and anything else that became runnable -- goes out
        # now rather than at the next user action. Guarded for the same reason as the body: a
        # trailing tick that raises would be an unhandled traceback on the pool's thread.
        try:
            self.tick()
        except Exception as error:
            print(
                f"[Swarm] trailing tick failed: {type(error).__name__}: {error}",
                file=sys.stderr,
            )

    def _handle_failure(self, node_id: str, outcome: NodeOutcome) -> None:
        """A worker failed. Whether that is the machine's fault decides what happens next.

        **The machine failing is not the work being wrong.** A broken pipe, a timeout, an OOM or an
        unreadable MCP reply is a transient fault of the *environment*; the human reviewer never saw
        the artifact, and a branch that gets bricked for it turns the human into a babysitter for a
        process pool. So a system failure is counted separately (``system_failures``) and the node
        goes back to ``pending``, where the trailing tick sweeps it up again -- up to
        ``max_system_retries``, after which the machine has genuinely failed us repeatedly and the
        node is marked ``failed``.

        A failure that is *not* system-level (a contract violation, a planner refusal) is the work's
        problem, not the environment's, and is marked ``failed`` immediately: retrying it would just
        reproduce it.
        """
        from storage.db import get_store

        store = get_store()
        if not is_system_failure(outcome.error):
            try:
                store.update_task_status(
                    self.plan_id, node_id, "failed",
                    detail_note=f"Planning failed: {outcome.error}",
                )
            except Exception:
                pass
            return

        try:
            failures = store.record_system_failure(self.plan_id, node_id)
        except Exception:
            return

        if failures < self.max_system_retries:
            # Still ``pending``: the failure count moved, the node did not, and the trailing tick
            # re-dispatches it without a human in the loop.
            return

        try:
            store.update_task_status(
                self.plan_id, node_id, "failed",
                detail_note=(
                    f"System failure {failures} times (limit {self.max_system_retries}): "
                    f"{outcome.error}"
                ),
            )
        except Exception:
            pass

    # -- the human gate ----------------------------------------------------------
    def commit(self, outcome: NodeOutcome) -> bool:
        """Record a yielded artifact and halt its node in ``planned``.

        The parent's half of the Artifact Gate: the worker produced the payload, and this writes it
        so the node is no longer ``pending`` -- which is what stops the swarm re-dispatching it --
        and so the UI has something to approve. Applying it is a separate step a human authorises.
        """
        if not outcome.ok:
            return False

        from tools import execution_gate

        artifact = outcome.artifact
        execution_gate.plan_artifact(
            plan_id=self.plan_id,
            task_id=outcome.node_id,
            summary=str(getattr(artifact, "summary", "") or ""),
            ast_targets=list(getattr(artifact, "ast_targets", []) or []),
            estimated_impact=str(getattr(artifact, "estimated_impact", "") or ""),
        )
        return True

    def reject(self, node_id: str, feedback: str = "") -> bool:
        """Persist a human rejection and re-dispatch. Returns whether the node gets another try.

        The critique and the attempt count go to the **database**, not to this object: a restart
        must not resurrect the rejection loop, and the dispatch query reads the count to exclude a
        node that has exhausted its budget. Re-dispatching is a ``tick()`` -- the node is ``pending``
        again, and the guard decides whether it comes back.
        """
        from storage.db import get_store

        store = get_store()
        if not store.record_rejection(self.plan_id, str(node_id), feedback):
            return False
        self._outcomes.pop(str(node_id), None)
        self.tick()
        return store.rejection_state(self.plan_id, str(node_id))["attempts"] <= self.max_retries

    def outcome_for(self, node_id: str) -> Optional[NodeOutcome]:
        """The last outcome this process saw for a node, for a caller holding a node id."""
        return self._outcomes.get(str(node_id))

    # -- introspection -----------------------------------------------------------
    def executable(self) -> List[str]:
        """The nodes this swarm would submit on the next tick."""
        connection = self._connection()
        try:
            return [
                node["id"]
                for node in get_executable_nodes(connection, self.plan_id, max_retries=self.max_retries)
                if node["id"] not in self._in_flight
            ]
        finally:
            connection.close()

    @property
    def in_flight(self) -> Set[str]:
        return set(self._in_flight)

    @property
    def busy(self) -> int:
        """Callbacks currently applying an outcome. ``in_flight`` empty and ``busy`` zero is idle."""
        return self._busy

    def settled(self) -> bool:
        """Whether every dispatched node has finished *and* its outcome has been applied."""
        return not self._in_flight and self._busy == 0

    def abandoned(self) -> List[str]:
        """Nodes that have exhausted their retry budget.

        Read from the database, because that is where the count lives -- the swarm holds no history.
        Deliberately *not* filtered by status: a node whose budget is spent has spent it whether the
        dispatcher is currently looking at it as ``pending`` or it has been marked ``failed``, and
        the report is about the budget rather than about the current label.
        """
        connection = self._connection()
        try:
            rows = connection.execute(
                "SELECT id FROM tasks WHERE plan_id = ? AND rejection_attempts > ?"
                " ORDER BY order_index",
                (self.plan_id, self.max_retries),
            ).fetchall()
        finally:
            connection.close()
        return [str(row["id"]) for row in rows]

    # -- lifecycle ---------------------------------------------------------------
    def drain(self, timeout: float = 30.0) -> bool:
        """Wait until every dispatched node has settled, or ``timeout`` elapses.

        The counterpart to ``settled()`` for a caller that is about to remove the database: the
        pool's callbacks reach the store on their own thread, so a caller that unlinks the file
        without waiting races them. Returns whether the swarm is settled.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        while not self.settled() and time.monotonic() < deadline:
            time.sleep(0.02)
        return self.settled()

    def shutdown(self, timeout: float = 30.0) -> bool:
        """Drain in-flight work, then tear the pool down. Idempotent.

        ``close()`` alone is immediate: it marks the swarm closed and shuts the pool down, and any
        callback still in flight becomes inert (a tick after close is a no-op). ``shutdown()`` is
        the deterministic form -- it first waits for the work already dispatched to settle, so a
        caller that is about to remove the database (a test's temporary directory, an app exit)
        leaves nothing to run against a file that is gone. Returns whether it drained in time.
        """
        drained = self.drain(timeout=timeout)
        self.close()
        return drained

    def close(self) -> None:
        """Tear the pool down. Idempotent, and safe when nothing was ever dispatched."""
        self._closed = True
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        self._in_flight.clear()

    def __enter__(self) -> "Swarm":
        return self.start()

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()
