"""Dual-sync plan state (plan.json machine state <-> active markdown plan).

Owns the "which representation is authoritative right now" decision: if the
markdown plan was edited on disk by a human (or an agent), re-hydrate plan.json
from it via the AST parser; otherwise read plan.json directly. Saving always
writes both representations and recomputes progress metrics.
"""

import json
import os
import time
from typing import Any, Dict, List, Optional

from tools.plan_parser import compile_plan_json_to_markdown, parse_markdown_to_plan_dict
from tools.workspace import get_active_plan_filename, get_plan_json_path, get_project_dir


def _empty_plan(title: str) -> Dict[str, Any]:
    """A valid, empty plan skeleton."""
    return {
        "version": "1.0",
        "plan_file": get_active_plan_filename(),
        "title": title,
        "updated_at": time.time(),
        "sections": [],
        "steps": [],
        "metrics": {
            "total_tasks": 0,
            "completed_tasks": 0,
            "in_progress_tasks": 0,
            "pending_tasks": 0,
            "progress_percent": 0
        }
    }


def _read_plan_json(plan_json_path: str) -> Optional[Dict[str, Any]]:
    """Read plan.json, or return ``None`` when it is missing or unreadable."""
    try:
        with open(plan_json_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[DualSync] Error reading plan.json: {e}")
        return None


def _hydrate_from_markdown(plan_md_path: str, plan_json_path: str) -> Optional[Dict[str, Any]]:
    """Rebuild plan.json from the markdown plan via the AST parser (zero LLM tokens)."""
    try:
        with open(plan_md_path, "r", encoding="utf-8") as f:
            content = f.read()
        plan_dict = parse_markdown_to_plan_dict(content, get_active_plan_filename())
        with open(plan_json_path, "w", encoding="utf-8") as f:
            json.dump(plan_dict, f, indent=2)
        return plan_dict
    except Exception as e:
        print(f"[DualSync] Error hydrating plan.json from markdown: {e}")
        return None


def load_plan_state(force_sync: bool = False) -> Dict[str, Any]:
    """
    Dual-Sync Engine:
    Loads machine state from plan.json.
    Detects external disk edits to PLAN.md: If PLAN.md is newer than plan.json,
    re-hydrates plan.json using markdown-it-py AST parser.
    """
    base_dir = get_project_dir()
    plan_md_path = os.path.join(base_dir, get_active_plan_filename())
    plan_json_path = get_plan_json_path()

    md_exists = os.path.exists(plan_md_path)
    json_exists = os.path.exists(plan_json_path)

    # Case 1: Neither exists yet -> return empty skeleton
    if not md_exists and not json_exists:
        return _empty_plan("New Project Plan")

    # Case 2: Only MD exists, or MD is newer than JSON, or force_sync requested, or plan.json is for different file
    # A plan.json that cannot be read at all is treated as "belongs to a different
    # file", which hands the markdown plan the chance to rebuild it below. Doing it
    # this way keeps a single read of plan.json and makes that recovery explicit
    # rather than a side effect of the comparison.
    cached_json = _read_plan_json(plan_json_path) if json_exists else None
    different_file = (
        cached_json is None
        or cached_json.get("plan_file", "").lower() != get_active_plan_filename().lower()
    )

    if md_exists and (not json_exists or force_sync or different_file or os.path.getmtime(plan_md_path) > os.path.getmtime(plan_json_path)):
        plan_dict = _hydrate_from_markdown(plan_md_path, plan_json_path)
        if plan_dict is not None:
            return plan_dict

    # Case 3: Load directly from plan.json
    if cached_json is not None:
        return cached_json

    return _empty_plan("Project Plan")


def save_plan_state(plan_dict: Dict[str, Any]) -> Dict[str, Any]:
    """
    Persists updated machine state to plan.json and instantly compiles back to PLAN.md.
    Recalculates all progress metrics automatically.
    """
    base_dir = get_project_dir()
    plan_md_path = os.path.join(base_dir, get_active_plan_filename())
    plan_json_path = get_plan_json_path()

    # Flatten steps from sections to ensure consistency
    all_steps = []
    sections = plan_dict.get("sections", [])
    for sec in sections:
        for t in sec.get("tasks", []):
            t["section"] = sec.get("title", "General")
            all_steps.append(t)

    total = len(all_steps)
    completed = len([t for t in all_steps if t.get("status") == "completed"])
    in_progress = len([t for t in all_steps if t.get("status") == "in_progress"])
    pending = total - completed - in_progress
    pct = round((completed / total) * 100) if total > 0 else 0

    plan_dict["steps"] = all_steps
    plan_dict["metrics"] = {
        "total_tasks": total,
        "completed_tasks": completed,
        "in_progress_tasks": in_progress,
        "pending_tasks": pending,
        "progress_percent": pct
    }
    plan_dict["updated_at"] = time.time()
    plan_dict["plan_file"] = get_active_plan_filename()

    # 1. Write plan.json
    try:
        with open(plan_json_path, "w", encoding="utf-8") as f:
            json.dump(plan_dict, f, indent=2)
    except Exception as e:
        print(f"[DualSync] Failed writing plan.json: {e}")

    # 2. Compile and write PLAN.md
    try:
        compiled_md = compile_plan_json_to_markdown(plan_dict)
        with open(plan_md_path, "w", encoding="utf-8") as f:
            f.write(compiled_md)
    except Exception as e:
        print(f"[DualSync] Failed compiling to PLAN.md: {e}")

    return plan_dict


def sync_plan_on_disk() -> Dict[str, Any]:
    """Checks for disk changes and performs bidirectional re-hydration if necessary."""
    return load_plan_state()


def update_plan_task_status(task_id: str, new_status: str, detail_note: Optional[str] = None, files: Optional[List[str]] = None) -> Dict[str, Any]:
    """Updates a specific task's status in plan.json and syncs to PLAN.md."""
    plan_dict = load_plan_state()
    for sec in plan_dict.get("sections", []):
        for task in sec.get("tasks", []):
            if task.get("id") == task_id or task.get("title") == task_id:
                task["status"] = new_status
                if detail_note:
                    task.setdefault("details", []).append(detail_note)
                if files:
                    task.setdefault("files", []).extend(files)
                return save_plan_state(plan_dict)
    return plan_dict
