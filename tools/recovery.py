"""Deliverable safety net: backups, codebase/plan audit, sync resolution, rollback.

Before any task modifies or creates a deliverable, a snapshot is recorded under
``.deepagents_backups/<task_id>/`` together with ``_meta.json`` describing whether
the file was modified (and where its prior copy lives) or newly created.

The audit compares what plan.json claims against what actually exists on disk, and
offers two resolutions: trust the codebase, or force the codebase back in line
with the plan. Rollback consumes the snapshots to reverse a completed task.
"""

import json
import os
import re
import shutil
import time
from typing import Any, Dict, Optional

from tools.plan_parser import compile_plan_json_to_markdown
from tools.plan_state import load_plan_state, save_plan_state
from tools.workspace import (
    BACKUP_SUBDIR,
    PLAN_JSON_FILE,
    get_active_plan_filename,
    get_project_dir,
    walk_workspace,
)


def get_backup_dir() -> str:
    path = os.path.join(get_project_dir(), BACKUP_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def backup_file_for_task(task_id: str, filename: str) -> Optional[str]:
    """
    Creates a snapshot of an existing file before a task modifies it.
    If the file doesn't exist yet, records that it was newly created.
    """
    base_dir = get_project_dir()
    filepath = os.path.join(base_dir, filename)
    task_backup_dir = os.path.join(get_backup_dir(), task_id)
    os.makedirs(task_backup_dir, exist_ok=True)

    backup_path = os.path.join(task_backup_dir, os.path.basename(filename))
    meta_path = os.path.join(task_backup_dir, "_meta.json")

    meta = {}
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            pass

    if os.path.exists(filepath):
        shutil.copy2(filepath, backup_path)
        meta[filename] = {"action": "modified", "backup": backup_path, "timestamp": time.time()}
    else:
        meta[filename] = {"action": "created", "backup": None, "timestamp": time.time()}

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    return backup_path if os.path.exists(filepath) else None


def audit_codebase_plan_sync() -> Dict[str, Any]:
    """
    Audits discrepancies between the current workspace files and plan.json tasks.
    Returns:
    - completed_missing_files: tasks marked completed whose deliverables are missing on disk
    - pending_existing_files: tasks marked pending whose deliverables already exist
    - untracked_files: code files in workspace not linked to any task deliverables
    - in_sync: boolean True if 0 discrepancies
    - summary: human-readable summary string
    """
    base_dir = get_project_dir()
    plan_dict = load_plan_state()

    # 1. Gather all actual files on disk (excluding internal / cache / temp files)
    disk_files = set()
    for root, _dirs, files in walk_workspace(base_dir):
        for f in files:
            if f.startswith(".tmp") or f.lower() in {
                PLAN_JSON_FILE.lower(), get_active_plan_filename().lower(), ".ds_store", "thumbs.db"
            }:
                continue
            rel = os.path.relpath(os.path.join(root, f), base_dir).replace("\\", "/")
            disk_files.add(rel)

    # 2. Gather task files
    completed_missing = []
    pending_existing = []
    all_task_files = set()

    for sec in plan_dict.get("sections", []):
        for task in sec.get("tasks", []):
            task_files = task.get("files", [])
            # Also extract from details if files array is empty
            if not task_files:
                for d in task.get("details", []):
                    found = re.findall(r'`([^`]+\.[a-zA-Z0-9]+)`', d)
                    task_files.extend(found)
            task_files = [f.replace("\\", "/").strip() for f in task_files if f.strip()]
            for tf in task_files:
                all_task_files.add(tf)

            if task.get("status") == "completed":
                missing = [tf for tf in task_files if tf not in disk_files and not os.path.exists(os.path.join(base_dir, tf))]
                if missing:
                    completed_missing.append({
                        "task_id": task.get("id"),
                        "title": task.get("title"),
                        "section": sec.get("title"),
                        "missing_files": missing
                    })
            elif task.get("status") in ("pending", "in_progress"):
                if task_files:
                    existing = [tf for tf in task_files if tf in disk_files or os.path.exists(os.path.join(base_dir, tf))]
                    if len(existing) == len(task_files):
                        pending_existing.append({
                            "task_id": task.get("id"),
                            "title": task.get("title"),
                            "section": sec.get("title"),
                            "existing_files": existing
                        })

    # 3. Untracked files on disk
    untracked = [df for df in sorted(disk_files) if df not in all_task_files]

    in_sync = len(completed_missing) == 0 and len(pending_existing) == 0
    discrepancy_count = len(completed_missing) + len(pending_existing)

    if in_sync and not untracked:
        summary = "Codebase and plan are in 100% synchronization."
    elif in_sync and untracked:
        summary = f"Plan tasks are synchronized, with {len(untracked)} untracked file(s) in workspace."
    else:
        summary = f"Detected {discrepancy_count} discrepancy(ies): {len(completed_missing)} completed task(s) missing deliverables, {len(pending_existing)} pending task(s) already have deliverables."

    return {
        "in_sync": in_sync,
        "discrepancy_count": discrepancy_count,
        "completed_missing_files": completed_missing,
        "pending_existing_files": pending_existing,
        "untracked_files": untracked,
        "summary": summary
    }


def resolve_sync_plan_to_codebase() -> Dict[str, Any]:
    """
    Resolution Option A: Sync Plan to Codebase.
    - Marks pending tasks as completed if all their deliverables already exist on disk.
    - If untracked files exist, records them under an audit deliverables note.
    """
    audit = audit_codebase_plan_sync()
    plan_dict = load_plan_state()

    modified_tasks = []
    tasks_to_complete = {item["task_id"] for item in audit["pending_existing_files"]}

    for sec in plan_dict.get("sections", []):
        for task in sec.get("tasks", []):
            if task.get("id") in tasks_to_complete:
                task["status"] = "completed"
                task.setdefault("details", []).append("Audited: Deliverables verified on disk.")
                modified_tasks.append(task.get("title"))

    saved = save_plan_state(plan_dict)
    recompiled = compile_plan_json_to_markdown(saved)
    return {
        "success": True,
        "action": "plan_to_codebase",
        "modified_tasks": modified_tasks,
        "plan_json": saved,
        "content": recompiled,
        "tree": saved.get("steps", [])
    }


def resolve_sync_code_to_plan() -> Dict[str, Any]:
    """
    Resolution Option B: Force Code to Match Plan.
    - Resets completed tasks whose deliverables are missing on disk back to 'pending'.
    - Ready for agents to re-execute and construct the missing files.
    """
    audit = audit_codebase_plan_sync()
    plan_dict = load_plan_state()

    modified_tasks = []
    tasks_to_reset = {item["task_id"] for item in audit["completed_missing_files"]}

    for sec in plan_dict.get("sections", []):
        for task in sec.get("tasks", []):
            if task.get("id") in tasks_to_reset:
                task["status"] = "pending"
                task.setdefault("details", []).append("Audited: Reset to pending due to missing deliverables on disk.")
                modified_tasks.append(task.get("title"))

    saved = save_plan_state(plan_dict)
    recompiled = compile_plan_json_to_markdown(saved)
    return {
        "success": True,
        "action": "code_to_plan",
        "modified_tasks": modified_tasks,
        "plan_json": saved,
        "content": recompiled,
        "tree": saved.get("steps", [])
    }


def rollback_task_state(task_id: str) -> Dict[str, Any]:
    """
    Rolls back a task from completed to pending.
    Restores file backups if available, or archives newly created deliverables.
    Saves plan.json and updates PLAN.md.
    """
    base_dir = get_project_dir()
    plan_dict = load_plan_state()

    target_task = None
    target_sec = None
    for sec in plan_dict.get("sections", []):
        for task in sec.get("tasks", []):
            if task.get("id") == task_id or task.get("title") == task_id:
                target_task = task
                target_sec = sec
                break
        if target_task:
            break

    if not target_task:
        return {"success": False, "error": f"Task '{task_id}' not found in active plan."}

    old_status = target_task.get("status")
    target_task["status"] = "pending"
    target_task.setdefault("details", []).append(f"Rolled back from '{old_status}' to pending.")

    # Restore backups if present in .deepagents_backups/<task_id>
    restored_files = []
    task_backup_dir = os.path.join(get_backup_dir(), target_task.get("id", task_id))
    meta_path = os.path.join(task_backup_dir, "_meta.json")

    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            for fname, info in meta.items():
                target_file = os.path.join(base_dir, fname)
                if info.get("action") == "modified" and info.get("backup") and os.path.exists(info["backup"]):
                    shutil.copy2(info["backup"], target_file)
                    restored_files.append(f"{fname} (restored prior version)")
                elif info.get("action") == "created" and os.path.exists(target_file):
                    archive_path = target_file + ".rollback_bak"
                    shutil.move(target_file, archive_path)
                    restored_files.append(f"{fname} (archived to .rollback_bak)")
        except Exception as e:
            print(f"[Rollback] Backup restoration note: {e}")

    saved = save_plan_state(plan_dict)
    recompiled = compile_plan_json_to_markdown(saved)

    return {
        "success": True,
        "task_id": target_task.get("id"),
        "task_title": target_task.get("title"),
        "restored_files": restored_files,
        "plan_json": saved,
        "content": recompiled,
        "tree": saved.get("steps", [])
    }
