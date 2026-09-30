"""The Artifact Gate: nothing executes until a human approves the plan.

System 2 does not execute blindly any more. It emits an
:class:`~tools.payloads.ImplementationPlanArtifact` naming the exact AST nodes it intends
to change; the task halts in the store's ``planned`` state; and the execution loop is
stopped -- not merely discouraged -- until an approval arrives.

Enforcement has two layers, because "the model was told not to" is not a boundary:

* **State.** A task in ``planned`` is not executable. Any write or node splice aimed at a
  file a ``planned`` task targets raises :class:`StateExecutionError`. Approving the task
  moves it to ``in_progress``, which is what unlocks execution.
* **Context.** :func:`approval_context` marks a task as approved *for the duration of the
  executing turn*, so a worker that was spawned by an approval is distinguishable from one
  that simply happens to run while a plan is pending.

The refusal is a raise, never a return value: a swallowed refusal is exactly the "false
success" this module exists to prevent.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
from typing import Any, Dict, Iterator, List, Optional

import bridge_bus
from storage.db import get_store, plan_id_for
from tools.payloads import ASTTarget, ImplementationPlanArtifact
from tools.workspace import get_active_plan_filename

# The task ids approved for the current execution context. A ContextVar, not a global: two
# workers running concurrently must not be able to approve each other's work.
_APPROVED: contextvars.ContextVar[frozenset] = contextvars.ContextVar(
    "deepagents_approved_tasks", default=frozenset()
)


class StateExecutionError(RuntimeError):
    """Execution was attempted for a task the Artifact Gate has not released."""

    def __init__(self, task_id: str, filename: str):
        super().__init__(
            f"Task {task_id} is PLANNED and has not been approved; "
            f"execution against {filename} is forbidden. Call approve_artifact first."
        )
        self.task_id = task_id
        self.filename = filename


def approved_tasks() -> frozenset:
    """The task ids approved in this execution context."""
    return _APPROVED.get()


@contextlib.contextmanager
def approval_context(task_id: str) -> Iterator[None]:
    """Mark ``task_id`` approved for the duration of the block."""
    token = _APPROVED.set(_APPROVED.get() | {str(task_id)})
    try:
        yield
    finally:
        _APPROVED.reset(token)


# --------------------------------------------------------------------------- matching


def _normalise(path: str) -> str:
    return str(path or "").replace("\\", "/").strip().lstrip("./").lower()


def _task_targets(node: Any) -> List[str]:
    """Every path a node claims: its declared deliverables and its AST targets."""
    targets = [str(name) for name in (getattr(node, "files", None) or [])]
    targets += [str(name) for name in (getattr(node, "ast_targets", None) or [])]
    return targets


def _node_targets_file(node: Any, filename: str) -> bool:
    """Whether a node's declared targets name ``filename`` (path or basename match)."""
    wanted = _normalise(filename)
    wanted_base = os.path.basename(wanted)
    for target in _task_targets(node):
        candidate = _normalise(target)
        if not candidate:
            continue
        if candidate == wanted or os.path.basename(candidate) == wanted_base:
            return True
    return False


def blocking_task(filename: str, plan_id: Optional[str] = None) -> Optional[str]:
    """The id of a ``planned``, unapproved task that targets ``filename``, or ``None``.

    ``None`` means execution is permitted: either no task claims the file, or every task
    that does has been approved (or was never gated).
    """
    store = get_store()
    resolved = plan_id or plan_id_for(get_active_plan_filename())
    dag = store.get_dag(resolved)
    if dag is None:
        return None
    approved = approved_tasks()
    for node in dag.ordered_nodes():
        if node.status != "planned":
            continue
        if node.id in approved:
            continue
        if _node_targets_file(node, filename):
            return node.id
    return None


def guard_write(filename: str, plan_id: Optional[str] = None) -> None:
    """Raise :class:`StateExecutionError` when ``filename`` is gated by a planned task."""
    blocker = blocking_task(filename, plan_id)
    if blocker is not None:
        raise StateExecutionError(blocker, filename)


# --------------------------------------------------------------------------- transitions


def plan_artifact(
    *,
    plan_id: str,
    task_id: str,
    summary: str,
    ast_targets: List[ASTTarget],
    estimated_impact: str = "",
    complexity_score: int = 1,
    required_capabilities: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Record an artifact and halt the task in ``planned``, atomically.

    The status change is one SQLite transaction (via the store's ``update_task_status``),
    and the ``artifact_planned`` event is emitted only *after* it commits -- so the UI can
    never be told about a plan the database does not hold.

    The Architect's assessment (``complexity_score``, ``required_capabilities``) is written onto the
    node in the same transaction. It is what Phase 7's router reads, and a score that lived only in
    the artifact payload would be lost the moment the artifact was replaced by a re-plan.
    """
    artifact = ImplementationPlanArtifact(
        plan_id=plan_id,
        task_id=task_id,
        summary=summary,
        ast_targets=list(ast_targets),
        estimated_impact=estimated_impact,
        complexity_score=int(complexity_score),
        required_capabilities=[str(name) for name in (required_capabilities or [])],
    )
    store = get_store()
    if store.get_dag(plan_id) is None:
        raise StateExecutionError(task_id, "<no plan>")
    if not store.update_task_status(plan_id, task_id, "planned"):
        raise StateExecutionError(task_id, "<unknown task>")
    store.record_assessment(
        plan_id, task_id, artifact.complexity_score, artifact.required_capabilities
    )
    # Persist the artifact so the approval (which may arrive in a later request) and the
    # executor can both read back exactly what was proposed.
    store.save_artifact(plan_id, task_id, artifact.model_dump())

    bridge_bus.emit({"type": "artifact_planned", "artifact": artifact.model_dump()})
    bridge_bus.emit({
        "type": "task_state_updated",
        "plan_id": plan_id,
        "task_id": task_id,
        "status": "planned",
    })
    return artifact.model_dump()


def approve_artifact(task_id: str, plan_id: str) -> Dict[str, Any]:
    """Approve a ``planned`` task, moving it to ``in_progress`` and unlocking execution.

    Validates the current state first: approving anything that is not ``planned`` is a
    refusal, so a double-click or a stale UI cannot re-run a finished task.
    """
    store = get_store()
    dag = store.get_dag(plan_id)
    if dag is None:
        return {"success": False, "error": f"No plan {plan_id!r} in the store."}
    node = dag.nodes.get(task_id)
    if node is None:
        return {"success": False, "error": f"No task {task_id!r} in plan {plan_id!r}."}
    if node.status != "planned":
        return {
            "success": False,
            "error": f"Task {task_id!r} is {node.status!r}, not 'planned'; nothing to approve.",
        }

    if not store.update_task_status(plan_id, task_id, "in_progress"):
        return {"success": False, "error": f"Task {task_id!r} could not be updated."}

    bridge_bus.emit({"type": "artifact_approved", "plan_id": plan_id, "task_id": task_id})
    bridge_bus.emit({
        "type": "task_state_updated",
        "plan_id": plan_id,
        "task_id": task_id,
        "status": "in_progress",
    })
    return {"success": True, "plan_id": plan_id, "task_id": task_id, "status": "in_progress"}
