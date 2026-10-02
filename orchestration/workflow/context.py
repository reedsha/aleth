"""The ambient dependencies a workflow action runs against, and the context it is given.

The line-anchored slicer is gone. It used to hand the Coder a slice of the *plan* built by
the compiled core's line walker, and nothing at all about the code it was about to change
-- so the model rewrote whole files from memory. Context is now assembled from two real
sources:

* the task's own structured slice (built here, in Python, from the plan dictionary -- no
  line arithmetic, no truncation), and
* the **exact AST nodes** the task concerns, retrieved from the workspace's LanceDB index
  (``tools.ast_context``) as whole classes/methods/functions with their byte spans.

So the Coder is told what to do *and* shown the precise nodes to do it in.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from tools.payloads import PlanPayload, validated
from tools.plan_parser import BEHAVIORAL_LOG_PREFIX

# The four status marks, and what each renders as in a slice. A task's mark *is* its
# status in the plan document, so the slice carries the same glyph the document does.
_STATUS_MARKS = {
    "pending": "[ ]",
    # ``planned`` is the Artifact Gate's state: a plan exists and execution is halted until
    # it is approved. It has no document mark of its own, so the slice names it.
    "planned": "[~]",
    "in_progress": "[-]",
    "completed": "[x]",
    "failed": "[!]",
}


@dataclass
class WorkflowContext:
    """Per-run dependencies shared by every action branch.

    ``mcp_session`` is the run's MCP lifecycle: the live servers, their bound tools, and the
    I/O helpers that reach them. Every branch takes its file and shell access from here
    rather than importing a helper module, so the run has exactly one source of I/O and the
    children are reaped when it ends (see ``orchestration.mcp_session``).
    """

    emit_fn: Any
    stream_text: Any
    should_stop: Any
    plan_file: str
    mcp_session: Any = None
    # The root agent I/O is granted for this run (Phase 23): the active shadow, captured once when
    # the run starts and injected into every write and read-modify-write read. A tool resolves the
    # root it was handed, never one of its own, so it cannot be pointed at the user's live tree.
    execution_root: str = ""


def _mark(status: Any) -> str:
    """The checkbox glyph for a status, defaulting to the pending mark."""
    return _STATUS_MARKS.get(str(status or ""), "[ ]")


def _tag_prefix(tag: Any) -> str:
    """``"[FE] "`` for a tagged entry, ``""`` for an untagged one."""
    text = str(tag or "").strip()
    return f"[{text}] " if text else ""


def _tasks(plan: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """Every task, from the flat view when it has one and the nested one otherwise."""
    steps = [task for task in (plan.get("steps") or []) if isinstance(task, Mapping)]
    if steps:
        return steps
    return [
        task
        for section in (plan.get("sections") or [])
        for task in (section.get("tasks") or [])
        if isinstance(task, Mapping)
    ]


def _find_task(plan: Mapping[str, Any], task_id: str) -> Optional[Mapping[str, Any]]:
    """A task by its id or its title (the lookup every caller holds one of)."""
    for task in _tasks(plan):
        if task.get("id") == task_id or task.get("title") == task_id:
            return task
    return None


def state_summary_block(plan: Mapping[str, Any]) -> str:
    """The plan's standing context: its title and Global State Summary bullets.

    This is the part of the plan that is true for every task, so it is carried into every
    prompt. Returns "" when the plan carries no summary rather than a heading with nothing
    under it.
    """
    summary = plan.get("state_summary") or {}
    bullets = [str(bullet) for bullet in (summary.get("bullets") or []) if str(bullet).strip()]
    if not bullets:
        return ""
    title = str(plan.get("title") or "").strip()
    lines = [f"# {title}"] if title else []
    lines.extend(f"- {bullet}" for bullet in bullets)
    return "\n".join(lines)


def task_slice(plan: Mapping[str, Any], task_id: str) -> str:
    """Everything about one task and nothing about any other, built structurally.

    Anchored on the task: its section, its own line (mark, tag, title), its sub-steps with
    their marks, every detail note and every declared deliverable. Nothing is truncated,
    and no other task or piece of machine state can appear in it.
    """
    task = _find_task(plan, task_id)
    if task is None:
        return ""

    lines: List[str] = []
    section = str(task.get("section") or "").strip()
    if section:
        lines.append(f"## {section}")
    lines.append(
        f"- {_mark(task.get('status'))} {_tag_prefix(task.get('tag'))}{task.get('title') or ''}".rstrip()
    )
    # The Living Behavioral Ledger: the durable evidence each finished task records, kept
    # in the slice so the next task is read against what actually happened.
    for entry in task.get("behavioral_log") or []:
        lines.append(f"  - {BEHAVIORAL_LOG_PREFIX} {entry}")
    for sub_step in task.get("sub_steps") or []:
        lines.append(
            f"  - {_mark(sub_step.get('status'))} "
            f"{_tag_prefix(sub_step.get('tag'))}{sub_step.get('title') or ''}".rstrip()
        )
    for detail in task.get("details") or []:
        lines.append(f"  - {detail}")
    for name in task.get("files") or []:
        lines.append(f"  - Deliverable: {name}")
    return "\n".join(lines)


def roadmap_brief(plan: Mapping[str, Any], max_tasks: int = 40) -> str:
    """A compact whole-roadmap view for an administrative answer.

    The standing summary first, then one line per task carrying only what a decision needs:
    its mark, its tag, its title and where its deliverable went. Capped, because the
    Architect is being asked about the shape of the roadmap, not handed the document.
    """
    lines: List[str] = []
    header = state_summary_block(plan)
    if header:
        lines.append(header)

    tasks = _tasks(plan)
    for task in tasks[:max_tasks]:
        deliverable = ""
        files = [str(name) for name in (task.get("files") or []) if str(name).strip()]
        if files:
            deliverable = f" -> {files[0]}"
        lines.append(
            f"- {_mark(task.get('status'))} {_tag_prefix(task.get('tag'))}"
            f"{task.get('title') or ''}{deliverable}"
        )
    if len(tasks) > max_tasks:
        lines.append(f"- \u2026 (+{len(tasks) - max_tasks} more tasks)")
    return "\n".join(lines)


def slice_task_context(plan: Mapping[str, Any], task_id: str) -> str:
    """The plan-only prompt block for one task: standing context, then the target slice."""
    summary = state_summary_block(plan)
    slice_text = task_slice(plan, task_id)
    parts = [part for part in (summary, slice_text) if part]
    return "\n\n".join(parts)


def assemble_coder_context(
    plan: Mapping[str, Any],
    task_id: str,
    *,
    workspace_dir: Optional[str] = None,
    k: int = 6,
    embedder: Any = None,
) -> str:
    """The full prompt block a Coder is given for one task.

    The plan slice (what to do) followed by the **exact AST nodes** the task concerns,
    retrieved from the workspace index (where to do it). The retrieved block is best-effort:
    a workspace that cannot be indexed yields the plan slice alone rather than an error,
    because the slice is still a usable prompt.
    """
    validated(PlanPayload, plan)
    parts = [slice_task_context(plan, task_id)]
    if workspace_dir:
        try:
            from tools import ast_context

            task = _find_task(plan, task_id) or {}
            query = " ".join(
                [str(task.get("title") or "")]
                + [str(detail) for detail in (task.get("details") or [])]
                + [str(name) for name in (task.get("files") or [])]
            ).strip()
            block = ast_context.context_for_query(workspace_dir, query, k=k, embedder=embedder)
            if block:
                parts.append("### Relevant code (exact AST nodes)\n\n" + block)
        except Exception:
            # Retrieval is context, not correctness: a failure must not cost the run.
            pass
    return "\n\n".join(part for part in parts if part)
