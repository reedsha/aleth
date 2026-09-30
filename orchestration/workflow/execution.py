"""The execution half of the two-phase lifecycle: apply an approved artifact, dumbly.

Once a human approves an artifact the task is ``in_progress`` and the plan is locked. This
module is the **only** path an approval takes, and it deliberately touches neither decision
system:

* **System 1 (the Gatekeeper router, ``agents.laya_model``)** is never called. Which Coder a
  directive *would* have been routed to is a decision the run no longer makes.
* **System 2 (the planner, ``orchestration.workflow.planner``)** is never called. The plan
  was written, reviewed and approved; re-planning it would be re-deciding it.

Re-entering an action branch to execute -- the shape this replaced -- re-ran both: the branch
re-classified the directive and could then answer differently the second time, stranding the
artifact a human had approved. Nothing about an approval is a decision. Everything it needs
is already in the store: the artifact carries the exact code and the exact spans, the task
carries its id.

What is left is mechanical, and that is all this module does -- apply the artifact
(:mod:`orchestration.workflow.executor`), check that the result compiles and that the task's
own tests pass, and record the outcome on the task. The verification is kept because it is
what makes a ``completed`` mark trustworthy: reporting success for code that does not compile
is the false-success failure this codebase has already been audited for.
"""

from __future__ import annotations

import sys
import time
from typing import Any, List, Mapping, Optional

from agents.model_routing import coder_model
from orchestration.swarm import UNVERIFIED_MESSAGE, verification_refusal
from orchestration.workflow import executor
from orchestration.workflow.events import tool_call, tool_result
from tools.file_tools import compile_plan_json_to_markdown, load_plan_state
from tools.shell_result import command_failed
from tools.test_runner import run_task_tests
from tools.workspace import get_active_plan_filename, get_project_dir
from storage.db import get_store, plan_id_for

# The executor is not a router. An approval applies the plan a human read; deriving an agent
# for it would be a routing decision, so the pass reports itself under one fixed identity.
EXECUTOR_AGENT = "coder-deep"
EXECUTOR_DISPLAY = "Senior Backend Coder"


def _find_task(plan_state: Mapping[str, Any], task_id: str) -> Mapping[str, Any]:
    """A task by id or title, from the flat view when it has one and the nested one otherwise."""
    tasks = [task for task in (plan_state.get("steps") or []) if isinstance(task, Mapping)]
    if not tasks:
        tasks = [
            task
            for section in (plan_state.get("sections") or [])
            for task in (section.get("tasks") or [])
            if isinstance(task, Mapping)
        ]
    for task in tasks:
        if str(task.get("id")) == task_id or str(task.get("title")) == task_id:
            return task
    return {}


def _target_files(artifact: Mapping[str, Any]) -> List[str]:
    """The workspace files an artifact touches, in target order, without duplicates."""
    seen: List[str] = []
    for target in (artifact.get("ast_targets") or []):
        name = str(target.get("file_path") or "").strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def _compile_check(ctx: Any, targets: List[str]) -> str:
    """The first compile error among the applied ``.py`` targets, or "" when they are clean.

    Run through the run's MCP session as the Architect's restricted verification command: the
    allow-list lives in the exec server, so this path cannot be widened by a caller here.
    """
    for name in targets:
        if not name.endswith(".py"):
            continue
        result = ctx.mcp_session.execute(f"python -m py_compile {name}", restricted=True)
        if command_failed(result):
            return result.strip()
    return ""


def _record_outcome(
    ctx: Any,
    task_id: str,
    status: str,
    note: str,
    files: List[str],
    *,
    verified: Optional[bool] = None,
) -> None:
    """Record the verdict **and** its graph consequences in one transaction, then announce it.

    The status write and the knowledge-graph write share a single SQLite transaction. They have
    to: the graph is the planner's only view of reality, so a task marked ``completed`` that the
    graph has never heard of is worse than a failed write -- the planner would reason over
    topology that does not match the code on disk and overwrite work it cannot see. One COMMIT
    decides both, so either the code change and its relational record both exist or neither
    does.

    The vector push is deliberately **outside** that transaction. LanceDB cannot enlist in it,
    so the outbox pattern applies: the relational intent is durable first, then the vectors are
    pushed from committed state, and ``KnowledgeSync.reconcile`` heals anything a crash leaves
    missing. A failed push costs a repair, not the record.

    The ``task_state_updated`` event is emitted only after the commit, so the UI is never told
    about a change the database does not hold.

    ``verified`` carries the engine's verification receipt (Phase 7.5) in the same statement as
    the status, so a task cannot be marked ``completed`` without its receipt or the reverse.
    """
    from storage.db import get_store

    store = get_store()
    plan_id = action_params_plan_id()
    uris: List[str] = []

    try:
        with store.transaction() as connection:
            if not store.update_task_status(
                plan_id, task_id, status, detail_note=note, files=files,
                connection=connection, verified=verified,
            ):
                raise RuntimeError(f"no task {task_id!r} in plan {plan_id!r}")
            uris = _ingest_into_knowledge_graph(connection, task_id, files, plan_id=plan_id)
    except Exception as error:
        # The whole unit rolled back: no status change, no graph change, and nothing announced.
        print(f"[KnowledgeGraph] outcome not recorded: {type(error).__name__}: {error}", file=sys.stderr)
        return

    ctx.emit_fn({
        "type": "task_state_updated",
        "plan_id": plan_id,
        "task_id": str(task_id),
        "status": status,
    })
    _push_vectors(uris)

    saved = load_plan_state()
    try:
        markdown = compile_plan_json_to_markdown(saved)
    except Exception:
        markdown = ""
    ctx.emit_fn({
        "type": "plan_updated",
        "filename": saved.get("plan_file") or get_active_plan_filename(),
        "content": markdown,
        "tree": saved.get("steps", []),
        "plan_json": saved,
    })


def _ingest_into_knowledge_graph(
    connection: Any, task_id: str, files: List[str], *, plan_id: str
) -> List[str]:
    """Record an applied task and its files in the graph, on the caller's transaction.

    Returns the URIs it registered so the caller can queue them for the vector index *after*
    the commit. It does not open a connection, commit, or swallow a failure: a graph write that
    cannot be made must roll back the unit of work it belongs to, because the alternative is a
    completed task the graph does not know about.

    A target path the URI scheme refuses (a traversal, an absolute path, or -- documented as a
    hard limitation -- a name containing a quote) is skipped by
    :func:`register_task_entities` rather than raising, so a badly named file cannot block a
    task's completion.
    """
    from storage.knowledge_sync import register_task_entities

    return register_task_entities(
        connection, plan_id=plan_id, task_id=str(task_id), title=str(task_id), files=list(files)
    )


def _push_vectors(uris: List[str]) -> None:
    """Push the just-committed entities to the vector index. Post-commit, and repairable.

    Best-effort *by design*: the relational record is already durable, and a vector that does
    not land is healed by ``KnowledgeSync.reconcile`` -- which runs before every planner
    invocation -- rather than by failing a run whose work has already been applied and recorded.
    """
    if not uris:
        return
    try:
        from orchestration.retriever import default_embedder, default_vector_store, knowledge_connection
        from storage.knowledge_sync import KnowledgeSync

        with knowledge_connection() as conn:
            sync = KnowledgeSync(default_vector_store(), default_embedder())
            for uri in uris:
                sync.queue(uri, text=uri.replace("kernel://", "").replace("/", " "))
            sync.flush(conn)
    except Exception as error:
        print(
            f"[KnowledgeGraph] vector push deferred to reconcile: {type(error).__name__}: {error}",
            file=sys.stderr,
        )


def _required_capabilities(plan_id: str, task_id: str) -> List[str]:
    """The node's own capability declaration, as the engine gate reads it.

    Read from the graph rather than from the artifact: the declaration is a **birth** field, so
    it survives a re-plan that replaces the artifact. An unknown node declares nothing, and the
    gate then stays out of the way rather than inventing a requirement.
    """
    dag = get_store().get_dag(plan_id)
    node = dag.nodes.get(task_id) if dag is not None else None
    return [str(name) for name in (getattr(node, "required_capabilities", None) or [])]


def _requeue_unverified(ctx: Any, plan_id: str, task_id: str, refusal: str) -> None:
    """Intercept an unverified completion: record the rejection, return the node to ``pending``.

    The engine refuses the *transition*; it does not call the work failed. The artifact was
    applied -- it simply has no evidence behind it -- so the node goes back to the queue for
    another attempt, which is the outcome the manifest asks for. The note is recorded through
    ``record_rejection``, so the count that bounds the retry is the ordinary rejection budget: a
    node that never produces a receipt eventually exhausts ``max_retries`` and stops being
    dispatched, exactly as a repeatedly rejected node does. ``verified`` is left at 0.
    """
    note = f"{UNVERIFIED_MESSAGE} ({refusal})"
    if not get_store().record_rejection(plan_id, task_id, note):
        return
    # Announced after the write commits, so the UI's demotion is the backend's, not a guess.
    ctx.emit_fn({
        "type": "task_state_updated",
        "plan_id": plan_id,
        "task_id": str(task_id),
        "status": "pending",
    })


def action_params_plan_id() -> str:
    """The active plan's id, for the graph's task entity."""
    from storage.db import plan_id_for
    from tools.workspace import get_active_plan_filename

    return plan_id_for(get_active_plan_filename())


def _finish(
    ctx: Any,
    title: str,
    status: str,
    files: List[str],
    deliverables: List[str],
    proposals: List[str],
    message: str,
) -> None:
    """Close the run: the Architect's review, then the terminal event."""
    ctx.emit_fn({
        "type": "architect_summary",
        "agent": "software-architect",
        "summary": {
            "title": f"Lead Architect Execution Review: {title}",
            "status": status,
            "files": files,
            "deliverables": deliverables,
            "proposals": proposals,
        },
    })
    time.sleep(0.3)
    ctx.emit_fn({"type": "workflow_complete", "status": "finished", "message": message})


def run_approved_artifact(
    ctx: Any,
    plan_state: Mapping[str, Any],
    coder_agents: Mapping[str, Any],
    action_params: Mapping[str, Any],
) -> None:
    """Apply the approved artifact for one task and record the outcome. Deterministic."""
    plan_id = str(action_params.get("planId") or plan_id_for(get_active_plan_filename()))
    task_id = str(action_params.get("taskId") or "")

    task = _find_task(plan_state, task_id)
    title = str(task.get("title") or task_id or "the approved task")

    executor_agent = coder_agents.get(EXECUTOR_AGENT) or {}
    display_name = str(executor_agent.get("display_name") or EXECUTOR_DISPLAY)
    model = str(executor_agent.get("model") or coder_model(EXECUTOR_AGENT))

    artifact = get_store().get_artifact(plan_id, task_id)
    if artifact is None:
        # An approval with no artifact is an inconsistent state, not a silent no-op: say so
        # and fail the task rather than leaving it stuck blue with no explanation.
        ctx.stream_text(
            "software-architect",
            f"> [EXECUTION PHASE] No approved artifact is stored for '{title}'. "
            f"Nothing can be applied; the task is marked failed.",
            log_type="decision",
            delay=0.02,
        )
        _record_outcome(
            ctx, task_id, "failed",
            "Execution refused: no approved artifact was stored for this task.", [],
        )
        _finish(
            ctx, title, "Execution Refused", [],
            ["No approved artifact was found for this task."],
            ["Plan the task again, then approve the artifact."],
            "No approved artifact was found; nothing was applied.",
        )
        return

    targets = _target_files(artifact)
    ctx.stream_text(
        "software-architect",
        f"> [EXECUTION PHASE] Applying the approved artifact for '{title}'.\n"
        f"> {len(artifact.get('ast_targets') or [])} AST target(s), applied verbatim. "
        f"No routing decision and no model call happen in this phase.",
        log_type="decision",
        delay=0.02,
    )
    ctx.emit_fn({
        "type": "delegation",
        "from_agent": "software-architect",
        "target_agent": EXECUTOR_AGENT,
        "target_name": display_name,
        "task": title,
    })
    ctx.emit_fn({
        "type": "coder_spawn",
        "agent": EXECUTOR_AGENT,
        "name": display_name,
        "model": model,
    })
    time.sleep(0.3)
    if ctx.should_stop():
        return

    ctx.emit_fn(tool_call(
        EXECUTOR_AGENT, "apply_artifact", {"task_id": task_id},
        f"Applying the approved plan to {', '.join(targets) or 'its targets'}",
    ))
    time.sleep(0.3)
    applied = executor.execute_approved(plan_id, task_id, workspace_dir=get_project_dir())
    ctx.emit_fn(tool_result(
        EXECUTOR_AGENT, "apply_artifact",
        f"Applied {len(applied.applied)} target(s)" if applied.success else applied.error,
    ))

    if not applied.success:
        ctx.emit_fn({
            "type": "coder_summary",
            "agent": EXECUTOR_AGENT,
            "summary": {
                "title": f"Execution Failed: {title}",
                "status": "Failed",
                "files": targets,
                "deliverables": [f"Could not apply the approved artifact: {applied.error}"],
            },
        })
        _record_outcome(
            ctx, task_id, "failed",
            f"Verification failed: {applied.error}", targets,
        )
        _finish(
            ctx, title, "Verification Failed", targets,
            [f"The approved artifact could not be applied: {applied.error}"],
            ["Re-plan the task, then approve the corrected artifact."],
            "The approved artifact could not be applied.",
        )
        return

    # Verification. Mechanical, and the only reason a `completed` mark can be trusted: a
    # compile error or a regression test that ran and failed is a failed task, while an
    # inconclusive check (no test file, an uncollectable suite) is not.
    ctx.stream_text(
        "software-architect",
        "> Verifying the applied artifact via restricted shell...",
        delay=0.02,
    )
    ctx.emit_fn(tool_call(
        "software-architect", "execute_restricted_command",
        {"command": "python -m py_compile"},
        "Validating the syntax of the applied targets",
    ))
    time.sleep(0.3)
    compile_error = _compile_check(ctx, targets)
    ctx.emit_fn(tool_result(
        "software-architect", "execute_restricted_command",
        compile_error or "Syntax and compilation verified with 0 errors",
    ))

    verdict = run_task_tests(task_id)
    test_summary = verdict.get("summary") or "No test file was recorded for this task."
    ctx.emit_fn(tool_call(
        "software-architect", "run_task_tests", {"task_id": task_id},
        "Executing the task's regression tests",
    ))
    time.sleep(0.3)
    ctx.emit_fn(tool_result(
        "software-architect", "run_task_tests", f"Tests: {test_summary}",
    ))

    failed = bool(compile_error) or verdict.get("verdict") == "failed"
    reason = compile_error.splitlines()[-1] if compile_error else f"tests did not pass ({test_summary})"

    # The engine gate (Phase 7.5). A node that required ``exec`` ran a command, so it must carry a
    # test receipt that exited 0 before it can be trusted as completed. An inconclusive check --
    # no test file, an uncollectable suite -- is the *absence* of evidence, and the engine
    # intercepts the transition: the node is re-queued for another attempt rather than marked
    # completed (or failed), and the refusal is recorded as a rejection so the retry is bounded.
    refusal = verification_refusal(verdict, _required_capabilities(plan_id, task_id))
    if refusal:
        note = f"{UNVERIFIED_MESSAGE} ({refusal})"
        ctx.stream_text(
            "software-architect",
            f"> [ENGINE GATE] {note}",
            log_type="decision",
            delay=0.02,
        )
        _requeue_unverified(ctx, plan_id, task_id, refusal)
        _finish(
            ctx, title, "Unverified", targets,
            [
                f"Applied the approved artifact to '{title}'.",
                note,
                "The task was returned to the queue for another attempt.",
            ],
            [
                "Produce a test receipt that exits 0, then approve the task again.",
                "An unverified task is never marked completed.",
            ],
            note,
        )
        return

    ctx.emit_fn({
        "type": "coder_summary",
        "agent": EXECUTOR_AGENT,
        "summary": {
            "title": f"Task Completed: {title}",
            "status": "Implemented",
            "files": targets,
            "deliverables": [
                f"Applied the approved plan to {', '.join(targets) or 'its targets'}.",
                "No model call was made during execution.",
            ],
        },
    })
    time.sleep(0.3)

    _record_outcome(
        ctx, task_id, "failed" if failed else "completed",
        f"Verification failed: {reason}" if failed
        else f"Applied the approved artifact to {', '.join(targets) or 'its targets'}.",
        targets,
        # The receipt is written with the status: `verified` is 1 only for a completion the gate
        # accepted, and 0 for one it refused.
        verified=not failed,
    )

    if failed:
        _finish(
            ctx, title, "Verification Failed", targets,
            [
                f"Applied the approved artifact to '{title}'.",
                f"Verification failed: {reason}",
                "The applied code needs attention before this task is trusted.",
            ],
            ["Re-plan the task once the failure above is addressed, then approve the new artifact."],
            f"Task '{title}' failed verification and needs attention.",
        )
        return

    _finish(
        ctx, title, "Verified & Approved", targets,
        [
            f"Applied the approved artifact for '{title}' verbatim.",
            f"Deliverables active in workspace: {', '.join(targets) or 'none'}.",
            "No routing decision and no model call were made during execution.",
        ],
        [
            "Execute Next Step to advance the roadmap.",
            "Review the applied diff in the artifact DAG.",
        ],
        f"Task '{title}' completed and verified.",
    )
