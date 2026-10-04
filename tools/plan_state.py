"""SQLite-backed plan state: the single source of truth.

The engine is now :mod:`storage.db` -- a SQLite DAG. This module is the Python face of
it: it resolves the paths, performs the one-time import of a plan authored before the
store existed, projects the DAG onto the plan dictionary every caller already expects,
and renders the markdown projection for humans.

**Markdown never determines state.** The plan file is written *from* the store as a
read-only projection, and nothing parses it back: the old ``plan.json`` + mtime
rehydration machine is gone, and the one-time import below is the only read of an
authored document. ``plan_structure_report`` reads it for a shape verdict only -- it
never writes state.
"""

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

import deepagents_core as _core

import bridge_bus
from storage.db import (
    PlanDAG,
    PlanStore,
    dag_to_plan_dict,
    get_store,
    merge_dag_fields,
    plan_dict_to_dag,
    plan_id_for,
)
from tools import atomic_io
from tools.payloads import (
    AppendTaskEnvelope,
    PlanPayload,
    PlanPrepareEnvelope,
    validated,
)
from tools.plan_parser import (
    check_plan_structure,
    compile_plan_json_to_markdown,
    parse_markdown_to_plan_dict,
)
from tools.workspace import (
    get_active_plan_filename,
    get_plan_dir,
    get_plan_markdown_path,
)


class PlanWriteError(RuntimeError):
    """A plan write (or its markdown projection) failed.

    Raised rather than swallowed: a caller must not be able to report ``success`` for
    state the store never received. The workflow runner turns this into an
    ``agent_error`` plus a terminal ``workflow_complete``; a bridge method lets it
    surface as a rejected call.
    """


# The most recent reason the plan could not be read or imported, if any. Read by the UI
# so a store that cannot be opened is explained rather than silently shown as an empty
# plan (audit F3).
_LAST_LOAD_ERROR = ""

# The old machine-state file. Read exactly once, to import a plan authored before the
# store existed; then removed. Never written.
_LEGACY_JSON = "plan.json"

# One lock for every plan-level read-modify-write and every PLAN.md write in this process.
#
# The store's own lock serialises each *transaction*, not the logical RMW: a caller reads the
# DAG, merges, and writes the whole task set back -- two overlapping saves each replace the set
# from their own snapshot, and the slower writer silently erases the faster one's task additions
# and status writes. The tagging daemon, workflow threads and API threads all write plans
# concurrently, so the read, the merge and the save (and the markdown projection rendered from
# the committed state) belong to one critical section. RLock because ``update_plan_task_status``
# nests a projection write inside the same section. The engine holds a process lock at boot, so
# in-process serialisation is the whole story; the Rust core's fs2 PlanLock guards only its own
# (unused-by-Python) write path.
_PLAN_WRITE_LOCK = threading.RLock()


def last_plan_load_error() -> str:
    """The reason the last plan load could not read the store, or "" when it was fine."""
    return _LAST_LOAD_ERROR


def _adopt(target: Dict[str, Any], replacement: Dict[str, Any]) -> Dict[str, Any]:
    """Replaces ``target``'s contents in place, so a caller's own dict stays current.

    Callers build a plan, save it, and then read the recomputed metrics back out of the
    same object, so the contents are moved across rather than rebinding the caller's
    name.
    """
    target.clear()
    target.update(replacement)
    return target


def _empty_plan() -> Dict[str, Any]:
    """The skeleton a plan with nothing stored resolves to (the old ``empty_plan``)."""
    return {
        "version": "1.0",
        "plan_file": get_active_plan_filename(),
        "title": "New Project Plan",
        "state_summary": None,
        "updated_at": time.time(),
        "sections": [],
        "steps": [],
        "metrics": {
            "total_tasks": 0,
            "completed_tasks": 0,
            "in_progress_tasks": 0,
            "failed_tasks": 0,
            "pending_tasks": 0,
            "progress_percent": 0,
        },
    }


def _plan_id() -> str:
    """The store key for the active plan file."""
    return plan_id_for(get_active_plan_filename())


def _ensure_imported(store: PlanStore, plan_id: str) -> None:
    """Import a pre-existing plan into the store, exactly once.

    ``plan.json`` (the old machine state) wins when it is present; otherwise the markdown
    plan is parsed once. After this the store is authoritative and neither file is read
    for state again -- ``plan.json`` is removed so it cannot be mistaken for one.
    """
    global _LAST_LOAD_ERROR
    if store.has_plan(plan_id):
        return

    legacy_path = os.path.join(get_plan_dir(), _LEGACY_JSON)
    if os.path.isfile(legacy_path):
        try:
            with open(legacy_path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            plan = validated(PlanPayload, raw)
            store.save_dag(plan_dict_to_dag(plan, plan_id))
            _discard_legacy(legacy_path)
            return
        except Exception as error:  # a corrupt machine-state file is reported, not hidden
            _LAST_LOAD_ERROR = f"plan.json could not be read ({error})"

    markdown_path = get_plan_markdown_path()
    if os.path.isfile(markdown_path):
        try:
            with open(markdown_path, "r", encoding="utf-8") as handle:
                content = handle.read()
            plan = parse_markdown_to_plan_dict(content, get_active_plan_filename())
            store.save_dag(plan_dict_to_dag(plan, plan_id))
        except Exception as error:
            _LAST_LOAD_ERROR = f"the markdown plan could not be imported ({error})"


def _discard_legacy(path: str) -> None:
    """Remove the old ``plan.json`` once its state lives in the store (best effort)."""
    try:
        os.remove(path)
    except OSError:
        pass


def load_plan_state(force_sync: bool = False) -> Dict[str, Any]:
    """The active plan, read from the store and projected onto the plan dictionary.

    ``force_sync`` is accepted because callers used to pass it to force a markdown
    rehydration; it is now a no-op. The store is always authoritative, so there is
    nothing to force.
    """
    global _LAST_LOAD_ERROR
    _LAST_LOAD_ERROR = ""
    store = get_store()
    plan_id = _plan_id()
    _ensure_imported(store, plan_id)
    dag = store.get_dag(plan_id)
    if dag is None:
        return _empty_plan()
    return dag_to_plan_dict(dag)


def read_plan_markdown() -> str:
    """The active plan's markdown projection as text, or ``""`` when it is absent.

    Read-only, for display. It is never parsed to decide state.
    """
    path = get_plan_markdown_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def write_plan_markdown(content: str) -> str:
    """Writes the active plan's markdown (the explicit-edit / projection path).

    Atomically, like every durable write: ``PLAN.md`` is git-tracked and human-authored, so a
    process killed mid-write must not be able to leave a half document where a whole one was.
    """
    path = get_plan_markdown_path()
    try:
        with _PLAN_WRITE_LOCK:
            atomic_io.write_text_atomic(path, content)
    except OSError as error:
        raise PlanWriteError(f"Failed writing the plan markdown: {error}") from error
    return path


def plan_structure_report(content: Optional[str] = None) -> Dict[str, Any]:
    """AST structure verdict for a plan document (the active plan by default).

    The Normalization Gate reads this to decide whether an imported ``.md`` can be
    parsed into sections and milestones, or has to be reformatted first. Read-only: it
    never writes the store, so asking the question cannot change the answer.
    """
    text = read_plan_markdown() if content is None else content
    return check_plan_structure(text)


def _write_projection(plan_id: str) -> None:
    """Re-render PLAN.md from the store (a projection, never a source of state)."""
    try:
        with _PLAN_WRITE_LOCK:
            markdown = get_store().render_plan_markdown(plan_id)
            atomic_io.write_text_atomic(get_plan_markdown_path(), markdown)
    except OSError as error:
        print(f"[PlanState] Could not write the markdown projection: {error}")


def save_plan_state(plan_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Persist ``plan_dict`` to the store, then render PLAN.md from it.

    The compiled core still canonicalises the dictionary (flattening ``steps``,
    regrouping a flat plan, recomputing metrics) so the projection and the store agree
    with the shape every caller already writes. Raises :class:`PlanWriteError` if either
    half fails, so a caller cannot report success for state the store never received.
    """
    validated(PlanPayload, plan_dict)
    prepared = validated(PlanPrepareEnvelope, json.loads(_core.prepare_plan_state(
        json.dumps(plan_dict),
        get_active_plan_filename(),
    )))
    if not prepared["ok"]:
        raise PlanWriteError(prepared["error"])
    plan = prepared["plan"]

    with _PLAN_WRITE_LOCK:
        plan_id = _plan_id()
        store = get_store()
        existing = store.get_dag(plan_id)
        dag = plan_dict_to_dag(
            plan, plan_id, created_at=existing.created_at if existing else None
        )
        # The legacy dictionary has no dependency/target/agent fields; carry the stored ones
        # forward so an ordinary status write does not erase what a later phase wrote.
        merge_dag_fields(dag, existing)
        # ``task_dependencies`` is the source of truth for topology, so the incoming document
        # defines the edges only on the plan's FIRST write -- creation or import. Afterwards an
        # ordinary save must not reconcile the table from whatever the caller's nodes carry: an
        # edge added through ``add_task_dependency`` would be erased by the next status write.
        store.save_dag(dag, sync_edges=existing is None)

        # Compiled through this module's own name on purpose: the compile is the seam a test
        # replaces to prove a failed compile cannot be reported as a successful save.
        try:
            compiled_md = compile_plan_json_to_markdown(plan)
        except Exception as error:
            raise PlanWriteError(f"Failed compiling to PLAN.md: {error}") from error
        try:
            atomic_io.write_text_atomic(get_plan_markdown_path(), compiled_md)
        except OSError as error:
            raise PlanWriteError(f"Failed writing PLAN.md: {error}") from error

    return _adopt(plan_dict, plan)


def sync_plan_on_disk() -> Dict[str, Any]:
    """The current stored state (the old "check the disk" call is now a plain read)."""
    return load_plan_state()


def _resolve_node_id(dag: PlanDAG, key: str) -> Optional[str]:
    """A node's id from its id or its title (the lookup the old engine used)."""
    if key in dag.nodes:
        return key
    for node in dag.ordered_nodes():
        if node.title == key:
            return node.id
    return None


def update_plan_task_status(
    task_id: str,
    new_status: str,
    detail_note: Optional[str] = None,
    files: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Move one task's status in a single transaction and announce the change.

    The mutation is committed first, then ``task_state_updated`` is emitted, so the UI is
    only ever told about a change the database actually holds.
    """
    store = get_store()
    with _PLAN_WRITE_LOCK:
        plan_id = _plan_id()
        dag = store.get_dag(plan_id)
        if dag is None:
            return load_plan_state()
        resolved = _resolve_node_id(dag, task_id)
        if resolved is None:
            return dag_to_plan_dict(dag)
        if not store.update_task_status(plan_id, resolved, new_status, detail_note, files):
            return dag_to_plan_dict(dag)
        bridge_bus.emit({
            "type": "task_state_updated",
            "plan_id": plan_id,
            "task_id": resolved,
            "status": new_status,
        })
        _write_projection(plan_id)
        return dag_to_plan_dict(store.get_dag(plan_id))


def append_pending_task(
    plan_state: Dict[str, Any],
    title: str,
    note: Optional[str] = None,
    tag: Optional[str] = None,
) -> Dict[str, Any]:
    """Add a pending task to the plan's last section, creating one for an empty plan.

    One definition of the shape of a task added to a plan, shared by the Architect's
    Update Plan action and the result view's one-click "Add to Plan". Mutates
    ``plan_state`` in place and returns the new task, as it always has: callers add the
    task and then save the same dictionary.
    """
    validated(PlanPayload, plan_state)
    result = validated(AppendTaskEnvelope, json.loads(_core.append_pending_task(
        json.dumps(plan_state),
        title,
        note,
        tag,
    )))
    if not result["ok"]:
        raise PlanWriteError(result["error"])
    _adopt(plan_state, result["plan"])
    return result["task"]


__all__ = [
    "PlanWriteError",
    "append_pending_task",
    "last_plan_load_error",
    "load_plan_state",
    "plan_structure_report",
    "read_plan_markdown",
    "save_plan_state",
    "sync_plan_on_disk",
    "update_plan_task_status",
    "write_plan_markdown",
]
