"""Markdown <-> plan-dict translation (markdown-it-py AST engine).

Pure translation logic: markdown text in, strict plan dictionary out (and back).
This module performs no file I/O and holds no state beyond reading the active
plan filename for its default argument, which makes it the easiest part of the
plan engine to test in isolation.

Four shapes of plan content are recognised, and only the first is a milestone:

    # Title                      the plan title
    ## 🌍 Global State Summary   standing facts, captured into `state_summary`
                                 instead of a task-less section
    - [ ] Task                   a milestone: gets a `task-N` id and a metric slot
      - [ ] Sub-step             folded into the parent task's `sub_steps`, never a
                                 task of its own, at any indent depth
      - detail bullet            a detail, or `files` when it declares deliverables
"""

import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from markdown_it import MarkdownIt

from tools.workspace import get_active_plan_filename


# A task's deliverables declaration, e.g. "Files: a.py, b.py". Both directions of the
# translation share this one pattern on purpose: an asymmetric pair is exactly what
# caused every task's `files` to be silently discarded whenever plan.json was
# rehydrated from the markdown plan.
_DELIVERABLES_RE = re.compile(r"(?i)files?\s*(?:created|modified)?\s*:\s*([^,\n]+(?:,\s*[^,\n]+)*)")

# A checkbox list item's mark and title. Shared by the task scan and the sub-step
# pre-pass so the two can never disagree about what counts as a checkbox.
_CHECKBOX_RE = re.compile(r"^[*-]?\s*\[([ xX\-])\]\s*(.*)")

# The Global State Summary heading. Matched on the words rather than the whole line so
# the decorative globe is optional and either heading level is accepted.
_STATE_SUMMARY_RE = re.compile(r"global\s+state\s+summary", re.IGNORECASE)
_STATE_SUMMARY_TITLE = "🌍 Global State Summary"

# A `[UI]` domain tag is a standalone token. The previous bare substring test could not
# tell a tagged task ("[UI] Dashboard view") from one that merely *mentions* the tag in
# prose or in backticks, which stripped the tag out of the middle of the title and left
# an orphaned empty code span behind.
_UI_TAG_RE = re.compile(r"(?:^|\s)\[ui\](?=\s|$)", re.IGNORECASE)


def parse_deliverables(text: str) -> List[str]:
    """File paths declared by a `Files: a.py, b.py` style detail line, else an empty list."""
    match = _DELIVERABLES_RE.search(text)
    if not match:
        return []
    return [f for f in (p.strip(" `\"'") for p in match.group(1).split(",")) if f]


def format_deliverables(files: List[str]) -> str:
    """Renders a files list as the detail line parse_deliverables() reads back."""
    return "Files: " + ", ".join(f"`{f}`" for f in files)


def _unique(files: List[str]) -> List[str]:
    """Order-preserving de-duplication.

    `files` is a set of deliverables, so the same path declared twice (for instance
    by a detail bullet that also survived as a compiled line) must not accumulate.
    """
    seen = set()
    unique = []
    for f in files:
        if f not in seen:
            seen.add(f)
            unique.append(f)
    return unique


def _absorb_detail_lines(task: Dict[str, Any], lines: List[str]) -> None:
    """Sorts detail bullets into a task's structured `files` or free-form `details`.

    A bullet that declares deliverables ("Files: a.py, b.py") belongs in `files`;
    leaving it in `details` as well would let the two views of one fact duplicate
    each other on the next compile/reparse round-trip.
    """
    for line in lines:
        declared = parse_deliverables(line)
        if declared:
            task["files"] = _unique(task["files"] + declared)
        else:
            task["details"].append(line)


def _status_from_mark(mark: str) -> str:
    """The one place a checkbox mark is translated into a task status."""
    return "completed" if mark.lower() == "x" else ("in_progress" if mark == "-" else "pending")


def _split_ui_tag(title: str) -> Tuple[str, bool]:
    """Splits a title into (clean title, is it [UI]-tagged)."""
    if not _UI_TAG_RE.search(title):
        return title.strip(), False
    return _UI_TAG_RE.sub("", title, count=1).strip(), True


def _collect_state_summary(tokens: List[Any]) -> Tuple[Optional[Dict[str, Any]], Optional[int], Optional[int]]:
    """Captures the optional `## 🌍 Global State Summary` block.

    Returns ``(summary, start, end)`` where ``start``/``end`` are the token indices the
    block occupies, so the task scan can walk past it instead of turning its prose
    bullets into milestones. ``summary`` is ``None`` when the plan has no such heading.
    """
    for i, token in enumerate(tokens):
        if token.type != "heading_open" or token.tag not in ("h2", "h3"):
            continue
        if i + 1 >= len(tokens) or tokens[i + 1].type != "inline":
            continue
        heading_text = tokens[i + 1].content.strip()
        if not _STATE_SUMMARY_RE.search(heading_text):
            continue

        bullets: List[str] = []
        end = len(tokens)
        for j in range(i + 2, len(tokens)):
            if tokens[j].type == "heading_open":
                end = j
                break
            if tokens[j].type == "inline" and tokens[j].content.strip():
                bullets.append(tokens[j].content.strip())
        return {"title": heading_text, "bullets": bullets}, i, end
    return None, None, None


def _collect_sub_steps(tokens: List[Any]) -> Tuple[Dict[str, List[List[Dict[str, Any]]]], Set[int], Set[str]]:
    """Finds the checkbox items indented under a top-level checkbox task.

    Runs as a separate pass so the milestone scan keeps behaving exactly as it did:
    previously each parent absorbed its *first* nested checkbox into `details` and
    hardened every remaining one into a sibling milestone, which is why a 16-task plan
    compiled down to 21.

    Returns ``(by_task, nested_indices, nested_texts)``:

    * ``by_task`` maps a top-level item's raw text to a queue of its sub-step lists.
      Nested checkbox items are flattened in document order at any indent depth, so
      2-space and 4-space nesting both land in the same ``sub_steps`` array.
    * ``nested_indices`` are the token indices consumed as sub-steps, which the
      milestone scan must skip so they never take a ``task-N`` id or a metric slot.
    * ``nested_texts`` are the raw item texts those sub-steps own, so the parent does
      not also absorb them as duplicate detail lines.
    """
    stack: List[Dict[str, Any]] = []
    top_level: List[Dict[str, Any]] = []

    for index, token in enumerate(tokens):
        if token.type == "list_item_open":
            stack.append({"index": index, "text": None, "details": [], "children": []})
        elif token.type == "inline" and stack:
            frame = stack[-1]
            if frame["text"] is None:
                frame["text"] = token.content.strip()
            else:
                frame["details"].append(token.content.strip())
        elif token.type == "list_item_close" and stack:
            frame = stack.pop()
            if stack:
                stack[-1]["children"].append(frame)
            else:
                top_level.append(frame)

    nested_indices: Set[int] = set()
    nested_texts: Set[str] = set()
    by_task: Dict[str, List[List[Dict[str, Any]]]] = {}

    def collect(frame: Dict[str, Any], inside_step: bool, out: List[Dict[str, Any]]) -> None:
        match = _CHECKBOX_RE.match(frame["text"] or "")
        if match and inside_step:
            out.append(frame)
            nested_indices.add(frame["index"])
            if frame["text"]:
                nested_texts.add(frame["text"])
            nested_texts.update(frame["details"])
        for child in frame["children"]:
            collect(child, inside_step or bool(match), out)

    for frame in top_level:
        if not _CHECKBOX_RE.match(frame["text"] or ""):
            continue
        collected: List[Dict[str, Any]] = []
        for child in frame["children"]:
            collect(child, True, collected)
        if not collected:
            continue
        sub_steps = []
        for item in collected:
            mark, raw_title = _CHECKBOX_RE.match(item["text"]).groups()
            clean_title, is_ui = _split_ui_tag(raw_title)
            sub_step = {
                # Filled in by the caller, which is where the parent's id is known.
                "id": None,
                "title": clean_title,
                "status": _status_from_mark(mark),
                "is_ui": is_ui,
                "details": [],
                "files": [],
            }
            _absorb_detail_lines(sub_step, item["details"])
            sub_steps.append(sub_step)
        # A queue, not a single list: two tasks may legitimately share a title.
        by_task.setdefault(frame["text"] or "", []).append(sub_steps)

    return by_task, nested_indices, nested_texts


def parse_markdown_to_plan_dict(content: str, filename: str = "PLAN.md", relaxed: bool = False) -> Dict[str, Any]:
    """
    Parses Markdown plan files using markdown-it-py token AST.
    Extracts headers, sections, tasks (with [x], [-], [ ] status and [UI] tag),
    the Global State Summary block, nested sub-steps, nested bullet details, and
    referenced deliverables into a strict JSON structure.
    If relaxed=True, extracts standard bulleted and numbered list items as pending tasks.
    """
    md = MarkdownIt("gfm-like")
    tokens = md.parse(content)

    state_summary, summary_start, summary_end = _collect_state_summary(tokens)
    sub_steps_by_task, sub_step_indices, sub_step_texts = _collect_sub_steps(tokens)

    title = "Project Plan"
    sections = []
    current_section = {"id": "sec-1", "title": "General", "tasks": []}
    all_steps = []
    task_counter = 0
    section_counter = 0

    i = 0
    while i < len(tokens):
        token = tokens[i]

        # 0. The Global State Summary is standing context, not a milestone list: it was
        #    captured above, so everything between its heading and the next heading is
        #    walked past rather than read as tasks.
        if summary_end is not None and summary_start < i < summary_end:
            i += 1
            continue

        # 1. Headers (h1 -> Plan Title, h2/h3 -> Section or Logged Task)
        if token.type == "heading_open":
            tag = token.tag
            if i + 1 < len(tokens) and tokens[i+1].type == "inline":
                heading_text = tokens[i+1].content.strip()
                if tag == "h1":
                    if ":" in heading_text:
                        title = heading_text.split(":", 1)[1].strip()
                    else:
                        title = heading_text
                elif tag in ("h2", "h3"):
                    if summary_start is not None and i == summary_start:
                        # Not a section: it holds no tasks and is already captured.
                        i += 2
                        continue
                    task_hdr = re.match(r"^\[(.*?)\]", heading_text)
                    if task_hdr:
                        task_counter += 1
                        t_title = task_hdr.group(1).strip()
                        clean_title, is_ui = _split_ui_tag(t_title)
                        task_obj = {
                            "id": f"task-{task_counter}",
                            "section": current_section["title"],
                            "title": clean_title,
                            "status": "completed",
                            "is_ui": is_ui,
                            "details": [],
                            "files": [],
                            "sub_steps": []
                        }
                        current_section["tasks"].append(task_obj)
                        all_steps.append(task_obj)
                    else:
                        if current_section["tasks"] or section_counter > 0:
                            sections.append(current_section)
                        section_counter += 1
                        current_section = {
                            "id": f"sec-{section_counter}",
                            "title": heading_text,
                            "tasks": []
                        }
            i += 2
            continue

        # 2. List items (tasks with checkboxes or sub-bullets)
        if token.type == "list_item_open":
            j = i + 1
            item_text = None
            item_details = []

            while j < len(tokens) and tokens[j].type != "list_item_close":
                if tokens[j].type == "inline":
                    txt = tokens[j].content.strip()
                    if item_text is None:
                        item_text = txt
                    else:
                        item_details.append(txt)
                j += 1

            if i in sub_step_indices:
                # Part of the card of an earlier task, not a milestone of its own.
                i = j + 1
                continue

            if item_text:
                cb_match = _CHECKBOX_RE.match(item_text)
                if cb_match:
                    mark, raw_task_title = cb_match.groups()
                    task_counter += 1
                    clean_task_title, is_ui = _split_ui_tag(raw_task_title)

                    task_obj = {
                        "id": f"task-{task_counter}",
                        "section": current_section["title"],
                        "title": clean_task_title,
                        "status": _status_from_mark(mark),
                        "is_ui": is_ui,
                        "details": [],
                        "files": [],
                        "sub_steps": []
                    }
                    queues = sub_steps_by_task.get(item_text)
                    if queues:
                        # Keyed by the item's own text so the sub-steps land on the task
                        # they were indented under; the milestone scan reaches the parent
                        # and its nested items in the same document order.
                        sub_steps = queues.pop(0)
                        for position, sub_step in enumerate(sub_steps, start=1):
                            sub_step["id"] = f"task-{task_counter}-sub-{position}"
                        task_obj["sub_steps"] = sub_steps
                    # Sub-steps own their own lines now, so they must not also arrive as
                    # detail bullets of the parent.
                    _absorb_detail_lines(
                        task_obj,
                        [d for d in item_details if d not in sub_step_texts],
                    )
                    current_section["tasks"].append(task_obj)
                    all_steps.append(task_obj)
                else:
                    if relaxed:
                        clean_item = re.sub(r"^(?:[*-]|\d+[.)])\s*", "", item_text).strip()
                        if clean_item and len(clean_item) > 2 and not clean_item.startswith("http"):
                            task_counter += 1
                            clean_task_title, is_ui = _split_ui_tag(clean_item)
                            task_obj = {
                                "id": f"task-{task_counter}",
                                "section": current_section["title"],
                                "title": clean_task_title,
                                "status": "pending",
                                "is_ui": is_ui,
                                "details": item_details,
                                "files": [],
                                "sub_steps": []
                            }
                            current_section["tasks"].append(task_obj)
                            all_steps.append(task_obj)
                    elif all_steps:
                        # Nested bullets past the first arrive here instead of the
                        # checkbox branch above (the item scan stops at the first
                        # list_item_close), so they must be sorted the same way or a
                        # task's deliverables would still be dropped on rehydration.
                        _absorb_detail_lines(all_steps[-1], [item_text] + item_details)
            i = j

        i += 1

    if current_section not in sections and current_section["tasks"]:
        sections.append(current_section)
    elif not sections and current_section:
        sections.append(current_section)

    if not all_steps and not relaxed and content.strip():
        # Fallback to relaxed parsing to capture numbered lists or un-checkboxed task lists
        return parse_markdown_to_plan_dict(content, filename=filename, relaxed=True)

    total = len(all_steps)
    completed = len([t for t in all_steps if t["status"] == "completed"])
    in_progress = len([t for t in all_steps if t["status"] == "in_progress"])
    pending = total - completed - in_progress
    pct = round((completed / total) * 100) if total > 0 else 0

    return {
        "version": "1.0",
        "plan_file": filename,
        "title": title,
        "state_summary": state_summary,
        "updated_at": time.time(),
        "sections": sections,
        "steps": all_steps,
        "metrics": {
            "total_tasks": total,
            "completed_tasks": completed,
            "in_progress_tasks": in_progress,
            "pending_tasks": pending,
            "progress_percent": pct
        }
    }


def compile_plan_json_to_markdown(data: Dict[str, Any]) -> str:
    """
    Compiles strict plan.json dictionary back into standardized Markdown format.
    Ensures human readability, GitHub-Flavored Markdown checkboxes, and preservation of details.
    """
    title = data.get("title", "Project Plan")
    lines = [f"# Project Plan: {title}", ""]

    # The Global State Summary is standing context rather than a milestone, so it is
    # re-emitted directly under the title; without this the block would be erased by the
    # first save and every later context slice would lose the plan's core facts.
    summary = data.get("state_summary") or {}
    if summary.get("bullets"):
        lines.append(f"## {summary.get('title') or _STATE_SUMMARY_TITLE}")
        for bullet in summary["bullets"]:
            lines.append(f"- {bullet}")
        lines.append("")
        lines.append("---")
        lines.append("")

    sections = data.get("sections", [])
    for sec in sections:
        sec_title = sec.get("title", "General")
        lines.append(f"## {sec_title}")

        for task in sec.get("tasks", []):
            st = task.get("status", "pending")
            mark = "x" if st == "completed" else ("-" if st == "in_progress" else " ")
            ui_prefix = "[UI] " if task.get("is_ui") else ""
            t_title = task.get("title", "")
            files = [f for f in task.get("files", []) if f]
            lines.append(f"- [{mark}] {ui_prefix}{t_title}")
            # Sub-steps are re-indented under their parent so the parser folds them back
            # into `sub_steps` instead of rebuilding them as extra milestones.
            for sub_step in task.get("sub_steps") or []:
                sub_status = sub_step.get("status", "pending")
                sub_mark = "x" if sub_status == "completed" else ("-" if sub_status == "in_progress" else " ")
                sub_ui_prefix = "[UI] " if sub_step.get("is_ui") else ""
                lines.append(f"  - [{sub_mark}] {sub_ui_prefix}{sub_step.get('title', '')}")
                for d in sub_step.get("details") or []:
                    lines.append(f"    - {d}")
                sub_files = [f for f in sub_step.get("files") or [] if f]
                if sub_files:
                    lines.append(f"    - {format_deliverables(sub_files)}")
            for d in task.get("details", []):
                lines.append(f"  - {d}")
            if files:
                # Without this line the markdown carries no record of a task's
                # deliverables, so load_plan_state() lost every `files` entry
                # whenever it rehydrated plan.json from the markdown plan.
                lines.append(f"  - {format_deliverables(files)}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def parse_plan_tree(content: str) -> list[dict]:
    """
    Backwards-compatible wrapper that returns the steps list parsed via AST.
    ZERO AI / LLM tokens used.
    """
    parsed = parse_markdown_to_plan_dict(content, get_active_plan_filename())
    return parsed.get("steps", [])
