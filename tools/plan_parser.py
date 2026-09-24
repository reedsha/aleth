"""Markdown <-> plan-dict translation (markdown-it-py AST engine).

Pure translation logic: markdown text in, strict plan dictionary out (and back).
This module performs no file I/O and holds no state beyond reading the active
plan filename for its default argument, which makes it the easiest part of the
plan engine to test in isolation.
"""

import re
import time
from typing import Any, Dict, List

from markdown_it import MarkdownIt

from tools.workspace import get_active_plan_filename


# A task's deliverables declaration, e.g. "Files: a.py, b.py". Both directions of the
# translation share this one pattern on purpose: an asymmetric pair is exactly what
# caused every task's `files` to be silently discarded whenever plan.json was
# rehydrated from the markdown plan.
_DELIVERABLES_RE = re.compile(r"(?i)files?\s*(?:created|modified)?\s*:\s*([^,\n]+(?:,\s*[^,\n]+)*)")


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


def parse_markdown_to_plan_dict(content: str, filename: str = "PLAN.md", relaxed: bool = False) -> Dict[str, Any]:
    """
    Parses Markdown plan files using markdown-it-py token AST.
    Extracts headers, sections, tasks (with [x], [-], [ ] status and [UI] tag),
    nested bullet details, and referenced deliverables into a strict JSON structure.
    If relaxed=True, extracts standard bulleted and numbered list items as pending tasks.
    """
    md = MarkdownIt("gfm-like")
    tokens = md.parse(content)

    title = "Project Plan"
    sections = []
    current_section = {"id": "sec-1", "title": "General", "tasks": []}
    all_steps = []
    task_counter = 0
    section_counter = 0

    i = 0
    while i < len(tokens):
        token = tokens[i]

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
                    task_hdr = re.match(r"^\[(.*?)\]", heading_text)
                    if task_hdr:
                        task_counter += 1
                        t_title = task_hdr.group(1).strip()
                        is_ui = "[ui]" in t_title.lower()
                        clean_title = re.sub(r"\[ui\]", "", t_title, flags=re.IGNORECASE).strip()
                        task_obj = {
                            "id": f"task-{task_counter}",
                            "section": current_section["title"],
                            "title": clean_title,
                            "status": "completed",
                            "is_ui": is_ui,
                            "details": [],
                            "files": []
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

            if item_text:
                cb_match = re.match(r"^[*-]?\s*\[([ xX\-])\]\s*(.*)", item_text)
                if cb_match:
                    mark, raw_task_title = cb_match.groups()
                    task_counter += 1
                    status = "completed" if mark.lower() == "x" else ("in_progress" if mark == "-" else "pending")
                    is_ui = "[ui]" in raw_task_title.lower()
                    clean_task_title = re.sub(r"\[ui\]", "", raw_task_title, flags=re.IGNORECASE).strip()

                    task_obj = {
                        "id": f"task-{task_counter}",
                        "section": current_section["title"],
                        "title": clean_task_title,
                        "status": status,
                        "is_ui": is_ui,
                        "details": [],
                        "files": []
                    }
                    _absorb_detail_lines(task_obj, item_details)
                    current_section["tasks"].append(task_obj)
                    all_steps.append(task_obj)
                else:
                    if relaxed:
                        clean_item = re.sub(r"^(?:[*-]|\d+[.)])\s*", "", item_text).strip()
                        if clean_item and len(clean_item) > 2 and not clean_item.startswith("http"):
                            task_counter += 1
                            is_ui = "[ui]" in clean_item.lower()
                            clean_task_title = re.sub(r"\[ui\]", "", clean_item, flags=re.IGNORECASE).strip()
                            task_obj = {
                                "id": f"task-{task_counter}",
                                "section": current_section["title"],
                                "title": clean_task_title,
                                "status": "pending",
                                "is_ui": is_ui,
                                "details": item_details,
                                "files": []
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
