"""The ambient dependencies a workflow action runs against.

Each action branch used to reach its emitter, narrator and plan filename through
closures created inside `run_agent_workflow`. Bundling them here keeps an
extracted action's signature about what it does rather than how it reaches the
outside world, and gives the runner one object to build and hand down.

The Line-Anchored Context Slicer at the bottom of this module builds a prompt from the
plan without reading it whole: the plan's standing context plus only the lines of the
plan that concern the task being worked on. It is pure translation -- a plan mapping in,
a string out -- so the Coder loop can carry a slice instead of re-sending the document
and its derived machine-state JSON every turn.
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Sequence

from tools.plan_parser import BEHAVIORAL_LOG_PREFIX


@dataclass
class WorkflowContext:
    """Per-run dependencies shared by every action branch."""

    emit_fn: Callable[[Dict[str, Any]], None]
    stream_text: Callable[..., None]
    should_stop: Callable[[], bool]
    plan_file: str


def state_summary_block(plan: Mapping[str, Any]) -> str:
    """The plan's standing context: its title and Global State Summary bullets.

    This is the part of the plan that is true for every task, so it is carried into
    every prompt. Returns "" when the plan carries no summary rather than a heading
    with nothing under it.
    """
    summary = plan.get("state_summary") or {}
    bullets = [b for b in (summary.get("bullets") or []) if b]
    if not bullets:
        return ""
    lines = []
    title = plan.get("title") or summary.get("title")
    if title:
        lines.append(f"# {title}")
    lines.extend(f"- {bullet}" for bullet in bullets)
    return "\n".join(lines)


def task_slice(plan: Mapping[str, Any], task_id: str) -> str:
    """The lines of the plan that concern one task: what to do and how we will know.

    Anchored on the task, not on the document: the task's own line (its section, title,
    status and tag), its sub-steps with their statuses, its detail notes and its
    declared deliverables. A Coder can act on this and nothing else in the plan changes
    what it should write.

    ``task_id`` matches on the task's ``id`` or its ``title``, the same way
    ``tools/plan_state.update_plan_task_status`` looks a task up, because callers hold
    either.

    Returns "" when no such task exists -- an empty slice is a caller error to handle,
    not a task to invent.
    """
    task = _find_task(plan, task_id)
    if task is None:
        return ""

    lines = []
    section = task.get("section")
    if section:
        lines.append(f"## {section}")
    lines.append(f"- {_status_mark(task.get('status'))} {_task_prefix(task)}{task.get('title', '')}")
    # What earlier work already did, carried at the same point the plan carries it, so a
    # re-run sees the ledger rather than repeating work the log says is finished.
    for entry in task.get("behavioral_log") or []:
        lines.append(f"  - {BEHAVIORAL_LOG_PREFIX} {entry}")

    for sub_step in task.get("sub_steps") or []:
        lines.append(
            f"  - {_status_mark(sub_step.get('status'))} "
            f"{_task_prefix(sub_step)}{sub_step.get('title', '')}"
        )
        for entry in sub_step.get("behavioral_log") or []:
            lines.append(f"    - {BEHAVIORAL_LOG_PREFIX} {entry}")
        for detail in sub_step.get("details") or []:
            lines.append(f"    - {detail}")
        sub_files = [f for f in (sub_step.get("files") or []) if f]
        if sub_files:
            lines.append(f"    - {_deliverables_line(sub_files)}")

    for detail in task.get("details") or []:
        lines.append(f"  - {detail}")
    files = [f for f in (task.get("files") or []) if f]
    if files:
        lines.append(f"  - {_deliverables_line(files)}")

    return "\n".join(lines)


def roadmap_brief(plan: Mapping[str, Any], max_tasks: int = 40) -> str:
    """A compact whole-roadmap view for an administrative answer.

    The standing summary first, then one line per task carrying only what a decision needs:
    its status mark, its tag, its title, and where its deliverable went. Capped, because
    the Architect is being asked about the shape of the roadmap, not handed the document
    (and its derived ``plan.json``) to re-read.
    """
    lines = []
    header = state_summary_block(plan)
    if header:
        lines.append(header)

    steps = list(plan.get("steps") or [])
    if not steps:
        steps = [t for s in plan.get("sections") or [] for t in s.get("tasks") or []]

    for task in steps[:max_tasks]:
        line = f"- {_status_mark(task.get('status'))} {_task_prefix(task)}{task.get('title', '')}"
        files = [f for f in (task.get("files") or []) if f]
        if files:
            line += f" \u2014 {', '.join(files[:2])}"
        lines.append(line)
    if len(steps) > max_tasks:
        lines.append(f"- \u2026 (+{len(steps) - max_tasks} more tasks)")

    return "\n".join(lines)


def slice_task_context(plan: Mapping[str, Any], task_id: str) -> str:
    """The whole prompt block for one task: standing context, then the target slice.

    This is what replaces "read PLAN.md and plan.json first". Order is deliberate: the
    summary is the frame the task is read in.
    """
    parts = [part for part in (state_summary_block(plan), task_slice(plan, task_id)) if part]
    return "\n\n".join(parts)


def _find_task(plan: Mapping[str, Any], task_id: str) -> Any:
    """The first task whose id or title is ``task_id``, or ``None``.

    ``steps`` is the flattened view of the same task objects, so it is consulted first;
    falling back to ``sections`` keeps the lookup working for a hand-built plan that only
    carries the nested shape.
    """
    for task in plan.get("steps") or []:
        if task.get("id") == task_id or task.get("title") == task_id:
            return task
    for section in plan.get("sections") or []:
        for task in section.get("tasks") or []:
            if task.get("id") == task_id or task.get("title") == task_id:
                return task
    return None


def _status_mark(status: Any) -> str:
    """The plan's own checkbox syntax for a status, so a slice line reads like the plan."""
    if status == "completed":
        return "[x]"
    if status == "in_progress":
        return "[-]"
    if status == "failed":
        return "[!]"
    return "[ ]"


def _task_prefix(task: Mapping[str, Any]) -> str:
    """The ``[TAG] `` a plan line carries, or nothing when the task has no tag."""
    tag = task.get("tag")
    return f"[{tag}] " if tag else ""


def _deliverables_line(files: Sequence[str]) -> str:
    """Renders declared files the way the plan reads them back ("Files: `a`, `b`")."""
    return "Files: " + ", ".join(f"`{f}`" for f in files)
