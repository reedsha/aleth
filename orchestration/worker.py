"""The swarm's worker: one DAG node, executed in its own process.

A worker is deliberately a **plain function with picklable arguments**. That is not a style
preference -- it is the only shape that can cross a ``ProcessPoolExecutor`` boundary. Everything a
node needs is either in the descriptor (the role, its hot-reloaded prompt, the plan and workspace
it belongs to) or reachable from ``db_path``. Nothing live is passed:

* **no SQLite connection** -- connections are bound to the process that opened them, so the worker
  opens its own;
* **no MCP session** -- a session owns child processes, which cannot be pickled either; the worker
  spawns its own and reaps it before returning;
* **no closure over the parent's memory** -- a hot-reloaded prompt mutated in the parent would not
  reliably reach a child, so the descriptor carries the prompt *as it is at dispatch time*. That
  is why the descriptor is serialised per submission rather than shared by reference.

What the worker does, in order: read the node, open a session **scoped to the node's declared
capabilities**, ask the Knowledge Graph for the context that node needs, run the LLM loop to
produce the artifact, tear the session down, and return the artifact. The artifact is the Phase 3.5
payload -- the exact bytes a human will approve -- and the worker does **not** apply it. Approval is
the human's, and it happens in the parent.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any, Callable, Dict, Optional

# The keys a role descriptor must carry. A descriptor is a plain dict on purpose: it is what gets
# pickled to the child, so it has to be data.
REQUIRED_ROLE_KEYS = ("name", "system_prompt")

# Where a node's plan comes from when the descriptor does not say.
DEFAULT_PLAN_ID = "PLAN"


def _task_for(connection: sqlite3.Connection, task_id: str) -> Dict[str, Any]:
    """The node as the planner expects it: id, title, details and declared files."""
    row = connection.execute(
        "SELECT id, title, details, files FROM tasks WHERE plan_id = ? AND id = ?",
        (str(task_id).split("::")[0], str(task_id).split("::")[-1]),
    ).fetchone()
    if row is None:
        raise LookupError(f"no task {task_id!r} in the plan database")

    def _list(raw: Any) -> list:
        try:
            value = json.loads(raw or "[]")
        except (TypeError, ValueError):
            return []
        return [str(item) for item in value] if isinstance(value, list) else []

    return {
        "id": str(row["id"]),
        "title": str(row["title"] or ""),
        "details": _list(row["details"]),
        "files": _list(row["files"]),
    }


def _resolve_completer(spec: Optional[str]) -> Optional[Callable[..., Any]]:
    """A completer named as ``module:function``, imported in the child.

    A callable cannot be pickled, so a scripted or injected planner travels as a *reference*. The
    child imports it in its own interpreter, which is also what keeps the seam honest: the worker
    calls something real, in its own process.
    """
    if not spec:
        return None
    module_name, _, attribute = str(spec).partition(":")
    if not module_name or not attribute:
        raise ValueError(f"planner_spec must be 'module:function', not {spec!r}")
    module = __import__(module_name, fromlist=[attribute])
    resolved = getattr(module, attribute)
    if not callable(resolved):
        raise TypeError(f"{spec!r} is not callable")
    return resolved


def _announce_route(task_id: str, role_descriptor: Dict[str, Any], model: str) -> None:
    """Report the model and the tool scope, once, as the worker boots.

    Written to stderr from the child, because that is the only place the decision is actually
    *used*: the parent routed it, but the child is where the provider call happens, and a worker
    that ran on a model -- or with a tool set -- nobody chose would otherwise be invisible.
    Reported, not decided -- the worker has no authority to pick either.
    """
    routing = role_descriptor.get("routing") or {}
    capabilities = role_descriptor.get("required_capabilities") or []
    # The Phase 19 agent decision, reported beside the model route: which brain the classifier
    # chose, on what band, and whether it faulted. The child applies all of it verbatim.
    classifier = routing.get("classifier") or {}
    print(
        f"[Worker] {task_id}: model={model or '<none>'} "
        f"endpoint={routing.get('base_url') or '<env>'} "
        f"tier={routing.get('tier', '?')} "
        f"complexity={routing.get('complexity_score')!r} "
        f"rejections={routing.get('rejection_attempts')!r} "
        f"agent={classifier.get('route', '?')} "
        f"band={classifier.get('complexity', '?')} "
        f"fault={classifier.get('fault') or '-'} "
        f"capabilities={list(capabilities)!r}",
        file=sys.stderr,
    )


def execute_node(
    task_id: str,
    role_descriptor: Dict[str, Any],
    db_path: str,
) -> Any:
    """Plan one node, in this process, and return its ``ImplementationPlanArtifact``.

    ``task_id`` is ``"<plan_id>::<node_id>"`` so a worker can address a node without being handed
    a connection. ``role_descriptor`` is the serialised role -- name, prompt, the **route the
    parent's router selected** (plus the decision behind it, for the log), and the node's
    **required capabilities**. ``db_path`` is the plan database the worker opens for itself.

    The model is taken from the descriptor and used verbatim: this process does not consult
    ``agents.model_routing`` for a default, because a worker that guessed a model would be a second,
    invisible routing authority. Its **endpoint** is taken from the descriptor in the same way, so
    a tier that declared a ``base_url``/``api_key`` is called there and with that credential rather
    than with whatever the environment happens to hold. A descriptor that carries no route is not a
    licence to guess -- the planner then resolves the router's own decision for the node, which is
    still the router's call and never a hardcoded one.

    The capability set is used verbatim too, and it is the *strongest* form of the same rule: the
    session is scoped to exactly what the node declared, so a node that declared nothing gets
    **no tools**. There is deliberately no "if the list is empty, load everything" fallback -- the
    planner must ask for what it needs, and a plan that cannot say so fails instead of being
    silently handed the world.

    Raises on failure rather than returning a partial answer: the parent receives the exception
    from the future and decides what to do. An artifact that could not be produced is not a result,
    and a placeholder would defeat the Artifact Gate the parent is about to submit it to.
    """
    from orchestration.mcp_session import MCPSessionContext
    from orchestration.workflow import planner

    plan_id, _, node_id = str(task_id).partition("::")
    plan_id = plan_id or str(role_descriptor.get("plan_id") or DEFAULT_PLAN_ID)
    node_id = node_id or str(task_id)

    workspace_dir = str(role_descriptor.get("workspace_dir") or ".")
    completer = _resolve_completer(role_descriptor.get("planner_spec"))
    model = str(role_descriptor.get("model") or "")
    # The tier's endpoint travels with its model, in the same decision: a model name and the
    # credential that reaches it are one thing, and the worker uses both verbatim.
    routing = role_descriptor.get("routing") or {}
    base_url = str(routing.get("base_url") or "")
    api_key = str(routing.get("api_key") or "")
    # A descriptor with no capability list is treated as an empty one, not as "all": the safe
    # reading of a missing declaration is the one that hands the model nothing.
    capabilities = [str(name) for name in (role_descriptor.get("required_capabilities") or [])]
    _announce_route(task_id, role_descriptor, model)

    from storage.connection import connect

    connection = connect(db_path)
    try:
        task = _task_for(connection, f"{plan_id}::{node_id}")

        # A human's rejection is not a status change, it is information. It is appended to the
        # node's notes so the planner reads the critique as part of the task it is planning --
        # otherwise the retry asks the identical question and produces the identical artifact,
        # which is compute spent to arrive where it already was.
        feedback = [str(note) for note in (role_descriptor.get("rejection_feedback") or []) if str(note).strip()]
        if feedback:
            task = dict(task)
            task["details"] = list(task.get("details") or []) + [
                f"REJECTED BY REVIEW (the previous artifact was refused): {note}"
                for note in feedback
            ]

        # The session is the worker's own, and it is torn down on every path out -- including the
        # exception path -- so a failed node cannot leave MCP children behind. Its scope is the
        # node's declaration, so the model is handed exactly the tools this node asked for.
        with MCPSessionContext(workspace_dir, capabilities=capabilities) as session:
            return planner.plan_task(
                task,
                plan_id=plan_id,
                workspace_dir=workspace_dir,
                session=session,
                completer=completer,
                # The parent's routing decision, applied verbatim -- model and endpoint both.
                model=model,
                base_url=base_url,
                api_key=api_key,
                # The node's own role, so the planning pass asks the *live* session for that
                # role's tool manifest rather than a role hardcoded in the planner. There is no
                # import-time catalog on either side of the boundary: the servers' ``tools/list``
                # is the manifest.
                role=str(role_descriptor.get("name") or ""),
                # The same declaration the session was scoped to, so the planning pass can select
                # the skill playbooks for exactly these capabilities (Phase 7.5).
                capabilities=capabilities,
                # The parent's run identity, carried in the descriptor (Phase 27): a fault this
                # child records has to be joinable to the intent the parent is executing, and the
                # child has no other way to know it.
                intent_id=str(role_descriptor.get("intent_id") or ""),
                # ...and the database the parent's ledger lives in, which this process cannot
                # derive: its own plan directory is not the parent's (Phase 28).
                ledger_path=str(db_path or ""),
            )
    finally:
        connection.close()


def role_payload(role: Any, *, plan_id: str, workspace_dir: str,
                 planner_spec: Optional[str] = None,
                 rejection_feedback: Optional[list] = None,
                 model: Optional[str] = None,
                 routing: Optional[Dict[str, Any]] = None,
                 capabilities: Optional[list] = None,
                 intent_id: str = "") -> Dict[str, Any]:
    """A role descriptor as a picklable payload, snapshotted at dispatch time.

    This is the fix for the hot-reload/concurrency hazard: the prompt is copied into a plain dict
    *now*, so a child receives the prompt as it stood when the node was dispatched rather than
    whatever the parent's memory holds by the time the child starts.

    ``rejection_feedback`` rides along for the same reason -- it is data the child needs, and it
    must be the critique as it stood at dispatch, not a live list the parent may append to while
    the child is planning.

    ``model`` is the **route the router selected for this node**, and it overrides the role's own
    default: the role says what kind of agent to build, the router says which brain runs it. The
    decision travels whole in ``routing`` so the child can log the tier and the inputs behind it.

    ``capabilities`` is the node's ``required_capabilities``, and it always travels -- as an empty
    list when there are none. The key is never omitted, because for the child "absent" and "empty"
    would be indistinguishable, and the whole point of the diet is that an empty declaration means
    no tools rather than all of them.

    ``intent_id`` is the parent's run identity (Phase 27). It travels for the same reason as
    everything else here: the child records telemetry, and a fault it records has to be joinable to
    the intent the parent is executing. The child cannot look it up -- it is a different process.
    """
    payload: Dict[str, Any] = {
        "name": str(getattr(role, "name", "")),
        "display_name": str(getattr(role, "display_name", "")),
        "description": str(getattr(role, "description", "")),
        "system_prompt": str(getattr(role, "system_prompt", "")),
        "model": str(model or getattr(role, "model", "")),
        "plan_id": str(plan_id),
        "workspace_dir": str(workspace_dir),
        # Always present, empty list included: the child must be able to tell a node that declared
        # nothing from a descriptor that forgot to say.
        "required_capabilities": [str(name) for name in (capabilities or [])],
        "intent_id": str(intent_id or ""),
    }
    if routing:
        # Plain JSON-able data: it crosses a process boundary and must stay picklable.
        payload["routing"] = dict(routing)
    if planner_spec:
        payload["planner_spec"] = str(planner_spec)
    if rejection_feedback:
        payload["rejection_feedback"] = [str(note) for note in rejection_feedback]
    missing = [key for key in REQUIRED_ROLE_KEYS if not payload.get(key)]
    if missing:
        raise ValueError(f"role descriptor is missing {missing}")
    return payload
