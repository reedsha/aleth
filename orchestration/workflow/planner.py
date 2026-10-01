"""The planning phase: the model writes the payload, then the run halts.

**The LLM is the planner.** The engine does not guess which symbols to touch -- it builds
the context (the task's slice plus the exact AST nodes its files declare) and asks System 2
for an :class:`~tools.payloads.ImplementationPlanArtifact` as structured JSON. Every
:class:`~tools.payloads.ASTTarget` in that artifact carries the exact code to inject, so the
artifact a human approves *is* the execution payload.

Planning then yields: the task halts in the store's ``planned`` state and the run ends. No
source is written here. :mod:`orchestration.workflow.executor` applies the approved artifact
-- one splice per target, no model call, no runtime context map.

Two things are deliberate:

* **DAG supremacy.** If an action touches the filesystem it exists in the graph. A directive
  that named no plan task gets an ephemeral node (:func:`ensure_task_for_directive`) and then
  follows the identical Plan -> Yield -> Approve -> Execute lifecycle.
* **An offline fallback.** A checkout with no API key still has to work, so when System 2 is
  unavailable the plan is built from the engine's own deterministic deliverable -- the same
  template code the offline path always wrote, now carried in the artifact as ``content``.
  That is a fallback, not the design: with a provider configured, the model plans.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from storage.db import TaskNode, get_store, plan_id_for
from tools.payloads import ASTTarget, ImplementationPlanArtifact
from tools.workspace import get_active_plan_filename, get_project_dir

# Where an ad-hoc directive's task is filed. One section, so a run of untasked fixes does
# not scatter nodes across the roadmap's own sections.
ADHOC_SECTION = "Ad-hoc"
ADHOC_SECTION_ID = "sec-adhoc"

# What the Architect declares for a directive it has not yet planned. A directive is *planned*
# before it runs, and planning means reading the workspace and mapping the symbols in the files it
# names -- so those two capabilities are the honest declaration at birth, not a fallback. The
# artifact the Architect produces for the node then replaces it with a precise set, and nothing
# here is a licence for the worker to decide for itself.
DIRECTIVE_CAPABILITIES = ("fs", "ast")

# The role whose live tool set the *engine's* planning pass binds -- the workflow's own planning
# pass, and the default for a direct caller. A dispatched worker passes its node's role instead
# (``worker.execute_node``), so the tool manifest is the node's, read from the live MCP servers,
# rather than an assumption baked into this module.
DEFAULT_PLANNER_ROLE = "architect"

_SYSTEM_PROMPT = """You are the planning half of a two-phase IDE agent.

You do NOT execute anything. You return ONE JSON object describing exactly what you would
change, and nothing else -- no prose, no markdown fences.

Schema:
{
  "plan_id": string,
  "task_id": string,
  "summary": string,
  "estimated_impact": string,
  "complexity_score": integer,     // REQUIRED, 1-5. 1 is a one-line change; 5 is system-wide.
  "required_capabilities": [string], // REQUIRED. The capabilities this work needs, from this
                                   // vocabulary and no other:
                                   //   "fs"   -- read workspace files and create new ones
                                   //   "exec" -- run a shell command in the workspace
                                   //   "ast"  -- list a file's symbols and replace exactly one node
                                   //   "net"  -- reach the network. Baseline is sealed: name this
                                   //             ONLY when the work genuinely needs egress
                                   //             (fetching a URL, calling an API, installing a
                                   //             package). Everything else runs with no network.
                                   // A task that touches the workspace must name what it needs:
                                   // this list is exactly the tool set the worker is given, so an
                                   // empty list hands it no tools at all. Empty is correct only for
                                   // work that needs no tool.
  "ast_targets": [
    {
      "file_path": string,        // workspace-relative
      "symbol_name": string,      // the qualified name of the node to replace, or "" to replace the whole file
      "byte_range": [int, int],   // the exact span, as given to you below
      "operation": "insert" | "replace" | "delete",
      "content": string           // THE COMPLETE NEW SOURCE for that node. This is what a human approves and what is applied verbatim.
    }
  ]
}

Rules:
- Every target's `content` must be the complete, compilable replacement for that exact node.
- Use the byte ranges given to you in the context; do not invent spans.
- Prefer a small number of precise node replacements over whole-file rewrites.
- `complexity_score` and `required_capabilities` are mandatory. Omitting either rejects the whole
  payload: the score decides which model runs the work, and a plan that cannot be routed cannot be
  executed.
- `required_capabilities` is not decoration: it is the tool set. Name every capability the work
  needs, and no capability it does not.
- Return only the JSON object.
"""


def _strip_fence(text: str) -> str:
    """The JSON body of a model reply, with any surrounding markdown fence removed."""
    body = (text or "").strip()
    fenced = re.match(r"^```[^\n]*\n(?P<body>.*?)\n?```$", body, re.DOTALL)
    return fenced.group("body").strip() if fenced else body


class PlanningUnavailable(RuntimeError):
    """System 2 could not produce an artifact, so the run cannot be planned.

    Raised rather than worked around. There is no deterministic fallback by design: an AI
    IDE without System 2 is a broken text editor, and a second execution path would double
    the test surface for a behaviour nobody wants.
    """


def _declared_files(task: Mapping[str, Any]) -> List[str]:
    """The workspace-relative files a task declares, in declaration order."""
    seen: List[str] = []
    for name in (task.get("files") or []):
        cleaned = str(name).strip().replace("\\", "/").lstrip("./")
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return seen


def _target_context(workspace_dir: str, files: List[str]) -> str:
    """The nodes the model may target, with their exact byte ranges.

    This is the map the plan is written against: a ``replace`` target must name a span the
    engine will recognise, so the model is handed the real spans rather than guessing.
    """
    lines: List[str] = []
    for relative in files:
        absolute = os.path.join(workspace_dir, relative)
        if not os.path.isfile(absolute):
            lines.append(f"- `{relative}` does not exist yet (operation: insert, whole file)")
            continue
        try:
            from tools.ast_editor import symbols_in_file

            units = symbols_in_file(absolute).symbols
        except Exception:
            lines.append(f"- `{relative}` could not be parsed (replace the whole file)")
            continue
        for unit in units:
            lines.append(
                f"- `{relative}` :: `{unit.qualified_name}` ({unit.kind}) "
                f"bytes {unit.byte_start}..{unit.byte_end}"
            )
    return "\n".join(lines) or "- (the task declares no files)"


def _prompt(task: Mapping[str, Any], *, plan_id: str, workspace_dir: str, files: List[str]) -> str:
    title = str(task.get("title") or "Untitled task")
    details = "\n".join(f"- {note}" for note in (task.get("details") or []))
    parts = [
        f"plan_id: {plan_id}",
        f"task_id: {task.get('id') or title}",
        f"Task: {title}",
    ]
    if details:
        parts.append("Notes:\n" + details)
    parts.append("Declared deliverables and their AST nodes:\n" + _target_context(workspace_dir, files))
    # The knowledge graph is the code context. This *replaces* the previous
    # ``ast_context.context_for_query`` call: that returned nodes matching the title by vector
    # similarity alone, and building its index loads the semantic embedder over the whole
    # workspace -- a heavyweight, network-dependent step on the planner's hot path. The graph
    # retriever returns the same kind of nodes *and the relations around them* -- what the task
    # touches and what already touches it -- which is what a planner needs before it commits to
    # an artifact, and it costs one LanceDB lookup rather than an index build.
    #
    # Retrieval is context, not correctness, so a failure here (no index yet, no model weights)
    # leaves the prompt as it was rather than failing the plan.
    try:
        from orchestration import retriever

        graph_context = retriever.retrieve_for_plan(task, top_k=5, max_depth=2)
        if graph_context:
            parts.append("Knowledge graph:\n" + graph_context)
    except Exception:
        pass
    parts.append("Return the JSON ImplementationPlanArtifact now.")
    return "\n\n".join(parts)


def _skills_for(capabilities: Optional[Sequence[str]]) -> str:
    """The playbooks a node's capabilities select, or "" when there are none.

    One query (``retriever.skills_for_capabilities``), resolved once per planning pass and handed
    to the transport as text. Retrieval is context, not correctness: a store that cannot answer
    leaves the prompt as it was rather than failing the plan -- the same rule the knowledge-graph
    block follows.
    """
    if not capabilities:
        return ""
    try:
        from orchestration import retriever

        return retriever.skills_for_capabilities(capabilities)
    except Exception:
        return ""


def _from_llm(
    task: Mapping[str, Any],
    *,
    plan_id: str,
    workspace_dir: str,
    files: List[str],
    model: str,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    completer: Any = None,
    session: Any = None,
    role: str = DEFAULT_PLANNER_ROLE,
    capabilities: Optional[Sequence[str]] = None,
) -> Optional[ImplementationPlanArtifact]:
    """Ask System 2 for the artifact, or ``None`` when it cannot be produced.

    ``model`` is the route the caller resolved -- from the router for a dispatched node, or the
    architect's own route for a directive. It is a parameter and **not** a lookup here: a planner
    that reached for a default would be a second, invisible routing authority, and the whole point
    of Phase 7 is that exactly one component decides which brain runs the work.

    ``base_url``/``api_key`` are that route's *endpoint*, threaded from the bootloader's tier
    configuration by the same caller. They travel together with the model on purpose: a model name
    and the credential that reaches it are one decision, and splitting them is how a validated tier
    ends up calling a different provider than the one it named.

    With a live ``session`` the planner runs as an **agent**: the model is sent the tools
    bound from the MCP servers and may call them (``read_file``, ``list_symbols``) to inspect
    the workspace before it commits to a plan, instead of being handed a static context blob.
    That is the whole difference between a model that answers and a model that acts -- and it
    is why the session is threaded down here rather than reconstructed.

    ``role`` names whose tools those are. It is a parameter and not a constant here: a
    dispatched worker passes the node's own role, so the manifest is read live for *that* node
    rather than assumed to be the engine's. There is no import-time catalog to consult -- the
    tools are whatever the servers advertise at the moment the session binds them.

    Without a session the call is the single-shot one it always was, so an offline checkout
    and every existing caller behave exactly as before.
    """
    from orchestration import system2

    if not system2.is_enabled(base_url=base_url, api_key=api_key):
        return None

    prompt = _prompt(task, plan_id=plan_id, workspace_dir=workspace_dir, files=files)
    # The playbooks this node's capabilities select, resolved once for the whole pass and
    # prepended to the system prompt by the transport (Phase 7.5).
    skills = _skills_for(capabilities)

    if session is not None:
        from orchestration.workflow.agent_loop import run_tool_loop

        def _loop_completer(*, system: str, messages: List[Any], tools: List[Any]) -> Any:
            return system2.complete_with_tools(
                model=model, base_url=base_url, api_key=api_key,
                system=system, messages=messages, tools=tools, skills=skills,
            )

        try:
            text, _transcript = run_tool_loop(
                session=session,
                role=role,
                system_prompt=_SYSTEM_PROMPT,
                user_message=prompt,
                # An injected completer drives the loop too, so a test can script the model's
                # decisions while the tools still go over the real MCP transport.
                completer=completer if completer is not None else _loop_completer,
            )
        except Exception:
            return None
        answer_text = text
    else:
        if completer is None:
            completer = system2.complete
        try:
            completion = completer(
                model=model, base_url=base_url, api_key=api_key,
                system=_SYSTEM_PROMPT, user=prompt, skills=skills,
            )
        except Exception:
            return None
        if completion is None:
            return None
        answer_text = str(getattr(completion, "text", "") or "")

    try:
        payload = json.loads(_strip_fence(answer_text))
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    # The engine owns these two fields: a model must not be able to plan against a
    # different plan or task than the one it was asked about.
    payload["plan_id"] = plan_id
    payload["task_id"] = str(task.get("id") or task.get("title") or "")
    try:
        return ImplementationPlanArtifact.model_validate(payload)
    except Exception:
        return None


def _routed_route(plan_id: str, task: Mapping[str, Any]) -> str:
    """The router's route for a node, from its stored assessment and rejection count.

    The deterministic answer for a caller that supplied no route of its own -- a direct
    ``plan_task`` call. It reads the node rather than reaching for a hardcoded default: a node with
    no assessment yet (the schema's ``complexity_score`` starts at 1) routes to the tier that score
    names, and an unusable one routes to the top tier with a ``CRITICAL`` log, both of which are the
    router's decisions to make. Never raises: an unknown node is routed as unassessed.
    """
    from orchestration.model_router import route_or_top

    task_id = str(task.get("id") or "")
    try:
        store = get_store()
        assessment = store.assessment_state(plan_id, task_id)
        rejections = store.rejection_state(plan_id, task_id)["attempts"]
    except Exception:
        return route_or_top(None)["route"]
    return route_or_top(assessment.get("complexity_score"), rejections)["route"]


def plan_task(
    task: Mapping[str, Any],
    *,
    plan_id: Optional[str] = None,
    workspace_dir: Optional[str] = None,
    completer: Any = None,
    session: Any = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    role: Optional[str] = None,
    capabilities: Optional[Sequence[str]] = None,
) -> ImplementationPlanArtifact:
    """The artifact for a task, produced by System 2. Raises when it cannot be planned.

    ``model`` is the route to plan with. A dispatched worker passes the route the parent's router
    selected; a directive passes the architect's route. When it is omitted the router's own decision
    for the node is resolved here (:func:`_routed_route`) -- still the router, never a guess.
    ``base_url``/``api_key`` are that route's endpoint, supplied by the same caller.

    ``session`` is the run's MCP lifecycle. When it is supplied the model plans as an agent --
    it may read and inspect the workspace through the bound tools -- and when it is not, the
    call is the single-shot one it has always been. ``role`` names whose tools the agent is
    handed; a dispatched worker passes its node's role, so the manifest comes from the live
    servers for that node. ``capabilities`` is the node's declaration, and it selects the skill
    playbooks the transport prepends to the system prompt (Phase 7.5). It defaults to the
    engine's own planning role. Execution never calls a model either way: the artifact carries
    the code. When System 2 is unconfigured, unreachable, or answers off-contract, this raises
    :class:`PlanningUnavailable` -- the runner turns that into ``agent_error`` plus a terminal
    ``workflow_complete``, which is the honest outcome for a request that cannot be planned.
    """
    resolved_plan = plan_id or plan_id_for(get_active_plan_filename())
    resolved_workspace = workspace_dir or get_project_dir()
    files = _declared_files(task)
    resolved_model = str(model or "") or _routed_route(resolved_plan, task)

    planned = _from_llm(
        task, plan_id=resolved_plan, workspace_dir=resolved_workspace, files=files,
        model=resolved_model, base_url=base_url, api_key=api_key,
        completer=completer, session=session, role=str(role or DEFAULT_PLANNER_ROLE),
        capabilities=capabilities,
    )
    if planned is None:
        raise PlanningUnavailable(
            "System 2 could not produce an ImplementationPlanArtifact; planning requires a "
            "configured model."
        )
    return planned


# ------------------------------------------------------------------ DAG supremacy


def task_by_id(plan_state: Mapping[str, Any], task_id: Optional[str]) -> Optional[Mapping[str, Any]]:
    """A plan task by id or title, or ``None`` when the directive named none."""
    if not task_id:
        return None
    for section in (plan_state.get("sections") or []):
        for task in (section.get("tasks") or []):
            if task.get("id") == task_id or task.get("title") == task_id:
                return task
    return None


def spawn_ephemeral_task(
    *,
    plan_id: str,
    title: str,
    files: List[str],
    tag: Optional[str] = None,
    details: Optional[List[str]] = None,
    required_capabilities: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Add an ad-hoc node to the DAG and return it as a plain mapping.

    A directive that touches the filesystem exists in the graph: there are no phantom
    tasks, so an untasked fix gets a real node and the identical lifecycle. ``details``
    carries the directive itself (the bug report, the free-form prompt) so the planner
    plans against the request and not only its truncated title.

    ``required_capabilities`` is declared **here, at birth** (Phase 7.4): the node must not
    depend on the worker that runs it to say what it needs. Callers pass
    :data:`DIRECTIVE_CAPABILITIES` unless they know better; the artifact the Architect produces
    for the node replaces the declaration with a precise set.
    """
    store = get_store()
    dag = store.get_dag(plan_id)
    if dag is None:
        raise ValueError(f"no plan {plan_id!r} in the store")

    existing = set(dag.nodes)
    index = 1
    while f"adhoc-{index}" in existing:
        index += 1
    node_id = f"adhoc-{index}"

    node = TaskNode(
        id=node_id,
        plan_id=plan_id,
        title=title,
        section=ADHOC_SECTION,
        section_id=ADHOC_SECTION_ID,
        tag=tag,
        files=[str(name) for name in files if str(name).strip()],
        details=[str(note) for note in (details or []) if str(note).strip()],
        status="pending",
        order_index=len(dag.order),
        required_capabilities=list(required_capabilities or []),
    )
    dag.nodes[node_id] = node
    dag.order.append(node_id)
    store.save_dag(dag)
    return node.model_dump()


def _adhoc_for(plan_id: str, files: List[str]) -> Optional[Mapping[str, Any]]:
    """An existing ad-hoc node for this work, so a re-run finds the approved one.

    Without this, every run of a directive would spawn a fresh node and re-plan it -- the
    approval would be stranded on a node nobody looks at again.
    """
    store = get_store()
    dag = store.get_dag(plan_id)
    if dag is None:
        return None
    wanted = {str(name).replace("\\", "/").lstrip("./") for name in files if str(name).strip()}
    candidates = [
        node for node in dag.ordered_nodes()
        if node.id.startswith("adhoc-") and node.status in ("planned", "in_progress")
    ]
    for node in candidates:
        node_files = {str(name).replace("\\", "/").lstrip("./") for name in node.files}
        if wanted and node_files & wanted:
            return node.model_dump()
    return candidates[-1].model_dump() if candidates else None


def task_by_dag_id(plan_id: str, task_id: Optional[str]) -> Optional[Mapping[str, Any]]:
    """A node by id straight from the store, for a task the plan document does not carry.

    An ad-hoc node lives in the DAG and is projected into the plan document, but an
    approval names it by id before any projection has run -- so the id is resolved against
    the store rather than by scanning the plan dictionary.
    """
    if not task_id:
        return None
    store = get_store()
    dag = store.get_dag(plan_id)
    if dag is None:
        return None
    node = dag.nodes.get(str(task_id))
    return node.model_dump() if node is not None else None


def ensure_task_for_directive(
    *,
    plan_id: str,
    task: Optional[Mapping[str, Any]],
    title: str,
    files: List[str],
    tag: Optional[str] = None,
    details: Optional[List[str]] = None,
    task_id: Optional[str] = None,
) -> Mapping[str, Any]:
    """The task a directive belongs to, spawning an ephemeral node when it names none.

    Resolution order: an explicit plan task wins; then the exact node an approval named by
    id; then an existing ad-hoc node for the same files (so a re-run finds the approved one
    rather than spawning a second); only then a fresh node.
    """
    if task is not None:
        return task
    if task_id:
        exact = task_by_dag_id(plan_id, task_id)
        if exact is not None:
            return exact
    existing = _adhoc_for(plan_id, files)
    if existing is not None:
        return existing
    return spawn_ephemeral_task(
        plan_id=plan_id, title=title, files=files, tag=tag, details=details,
        required_capabilities=list(DIRECTIVE_CAPABILITIES),
    )


def needs_planning(task: Mapping[str, Any]) -> bool:
    """Whether a task must be planned before it may run.

    ``planned`` is the halted state and ``in_progress`` is the approved one: the first has
    already been planned (re-planning would loop), the second has been approved by a human
    and is what execution runs against. Everything else needs a plan first.
    """
    return str(task.get("status") or "pending") not in ("planned", "in_progress")


def plan_and_yield(
    ctx: Any,
    *,
    task: Mapping[str, Any],
    plan_file: str,
    plan_id: Optional[str] = None,
    completer: Any = None,
) -> Dict[str, Any]:
    """Plan the task, halt it in ``planned``, announce it, and end the run.

    Returns the artifact that was recorded. The caller must return immediately afterwards:
    this phase produces a reviewable payload, it does not execute it.
    """
    from orchestration.model_router import endpoint_for_tier
    from tools import execution_gate

    # Planning a directive is the Architect's own work, so it runs on the architect's tier -- read
    # from the *fleet* the bootloader proved, not from the environment directly, so a config file
    # governs this path too. The endpoint travels with the route: a tier that declares a base_url
    # and key is called there, with that credential.
    endpoint = endpoint_for_tier("architect")
    artifact = plan_task(
        task,
        plan_id=plan_id,
        session=getattr(ctx, "mcp_session", None),
        completer=completer,
        model=endpoint["route"],
        base_url=endpoint["base_url"],
        api_key=endpoint["api_key"],
    )
    recorded = execution_gate.plan_artifact(
        plan_id=artifact.plan_id,
        task_id=artifact.task_id,
        summary=artifact.summary,
        ast_targets=artifact.ast_targets,
        estimated_impact=artifact.estimated_impact,
        # The Architect's own assessment travels with the plan: it is what Phase 7's router reads
        # when the node is dispatched, and it is written onto the node in the same transaction.
        complexity_score=artifact.complexity_score,
        required_capabilities=artifact.required_capabilities,
    )

    targets = recorded.get("ast_targets", [])
    ctx.stream_text(
        "software-architect",
        f"> [PLANNING PHASE] Implementation plan proposed for '{artifact.summary}'.\n"
        f"> {len(targets)} AST target(s) carry their exact replacement code. "
        f"Execution is halted pending approval.",
        log_type="decision",
        delay=0.02,
    )
    ctx.emit_fn({
        "type": "architect_summary",
        "agent": "software-architect",
        "summary": {
            "title": f"Plan Proposed: {artifact.summary}",
            "status": "Awaiting Approval",
            "files": [str(target.get("file_path", "")) for target in targets] or [plan_file],
            "deliverables": [
                f"{len(targets)} AST target(s) carry their exact code; nothing has been written.",
                "Approve the artifact to apply it verbatim.",
            ],
            "proposals": ["Approve the plan to inject its code payload."],
        },
    })
    ctx.emit_fn({
        "type": "workflow_complete",
        "status": "planned",
        "message": "Implementation plan proposed; awaiting approval.",
    })
    return recorded
