"""Read-only projections of engine state for the gateway.

Nothing in this module writes. Every function opens the store (or the ledger) for the length of
one query and returns a **typed** projection -- never a raw row, never a connection. That is the
whole point of the gateway: the frontend asks for state, and the shape it gets back is declared
in ``api.schemas`` rather than being whatever the database happened to hold.

The store is shared and WAL-backed, so a read here runs concurrently with a live workflow's
writes; it is the same ``get_store()`` the engine uses, which is why no second connection policy
is invented here.
"""

from __future__ import annotations

from typing import List, Optional

from api.schemas import (
    MetricsView,
    PlanListResponse,
    PlanView,
    TaskView,
    TelemetryListResponse,
    TelemetryReceiptView,
)
from storage.db import TaskNode, compute_metrics, get_store, plan_id_for


def active_plan_id() -> str:
    """The id of the plan the workspace has active (``PLAN.md`` -> ``PLAN``)."""
    from tools.workspace import get_active_plan_filename

    return plan_id_for(get_active_plan_filename())


def active_plan_file() -> str:
    """The filename of the active plan, as the workspace knows it."""
    from tools.workspace import get_active_plan_filename

    return str(get_active_plan_filename())


def _task_view(node: TaskNode) -> TaskView:
    return TaskView(
        id=node.id,
        title=node.title,
        status=node.status,
        section=node.section,
        tag=node.tag,
        dependencies=list(node.dependencies),
        required_capabilities=list(node.required_capabilities),
        assigned_agent=node.assigned_agent,
        files=list(node.files),
    )


def plan_view(plan_id: str) -> Optional[PlanView]:
    """One plan's tasks and counters, or ``None`` when the store does not hold it."""
    dag = get_store().get_dag(plan_id)
    if dag is None:
        return None
    nodes = dag.ordered_nodes()
    return PlanView(
        plan_id=dag.plan_id,
        plan_file=dag.plan_file,
        title=dag.title,
        goal=dag.goal,
        updated_at=float(dag.updated_at),
        tasks=[_task_view(node) for node in nodes],
        metrics=MetricsView(**compute_metrics(nodes)),
    )


def plans_view() -> PlanListResponse:
    """What the store holds and what the workspace has, side by side.

    Both halves matter and they are not the same set: a plan file on disk that was never loaded
    has no DAG yet, and a stored plan whose file was deleted still has state.
    """
    from tools.workspace import list_plan_files

    store = get_store()
    return PlanListResponse(
        active_plan_id=active_plan_id(),
        active_plan_file=active_plan_file(),
        stored_plan_ids=sorted(store.list_plan_ids()),
        plan_files=sorted(str(name) for name in (list_plan_files() or [])),
    )


def telemetry_view(*, session_id: Optional[str] = None, limit: int = 50) -> TelemetryListResponse:
    """The newest ledger receipts, optionally narrowed to one run session.

    ``limit`` is clamped rather than trusted: an unbounded ``?limit=`` would let a caller ask the
    server to materialise the whole ledger into memory.
    """
    from storage import telemetry

    path = get_store().path
    bounded = max(1, min(int(limit), 500))
    if session_id:
        # ``receipts_for_session`` returns oldest-first, which is the right order for a forensic
        # read; the API hands the newest first so the UI's default view is the recent one.
        rows: List[dict] = list(reversed(telemetry.receipts_for_session(path, session_id)))[:bounded]
    else:
        rows = telemetry.recent(path, bounded)
    receipts = [TelemetryReceiptView(**row) for row in rows]
    return TelemetryListResponse(
        count=len(receipts), total=int(telemetry.count(path)), receipts=receipts
    )


def receipt_view(execution_id: str) -> Optional[TelemetryReceiptView]:
    """One ledger row by execution id, or ``None`` when there is no such execution."""
    from storage import telemetry

    row = telemetry.receipt(get_store().path, execution_id)
    return TelemetryReceiptView(**row) if row is not None else None
