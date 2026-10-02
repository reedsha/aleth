"""Deliverable safety net: backups, codebase/plan audit, sync resolution, rollback.

Before any task modifies or creates a deliverable, a snapshot is recorded under
``.aleth_backups/<task_id>/`` together with ``_meta.json`` describing whether
the file was modified (and where its prior copy lives) or newly created.

The audit compares what the plan state claims against what actually exists on disk, and
offers two resolutions: trust the codebase, or force the codebase back in line
with the plan. Rollback consumes the snapshots to reverse a completed task.
"""

import difflib
import json
import os
import re
import shutil
import time
from typing import Any, Dict, List, Optional

from tools import atomic_io
from tools.plan_parser import compile_plan_json_to_markdown
from tools.plan_state import load_plan_state, save_plan_state
from storage.db import DB_FILENAME
from tools.payloads import (
    AuditReport,
    PlanMutationResult,
    PlanPayload,
    RollbackResult,
    TaskDiffResult,
    validated,
    validated_backup_meta,
)
from tools.workspace import (
    BACKUP_SUBDIR,
    IDENTITY_FILE,
    get_active_plan_filename,
    get_plan_markdown_path,
    get_project_dir,
    walk_workspace,
)


def get_backup_dir() -> str:
    path = os.path.join(get_project_dir(), BACKUP_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


# A single "before" slot for the whole plan: the state dictionary and the markdown exactly
# as they were immediately before the last agent-driven revision. Distinct from the
# per-task deliverable snapshots above -- this captures the roadmap document itself, so a
# revision can be undone.
PLAN_REVISION_SUBDIR = "plan_revision"
PLAN_REVISION_JSON = "plan_revision.json"


def _plan_revision_dir() -> str:
    path = os.path.join(get_backup_dir(), PLAN_REVISION_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def snapshot_plan_revision() -> bool:
    """Captures the plan's current state into the revision slot, before a change is written.

    Returns ``False`` when there is no plan to capture yet (nothing to revert to) -- a
    first-ever revision has no prior state. Nothing raises: a missing snapshot must never
    be able to fail the plan edit that is about to be made.
    """
    plan = load_plan_state()
    if not (plan.get("steps") or plan.get("sections")):
        return False
    dest = _plan_revision_dir()
    atomic_io.write_text_atomic(
        os.path.join(dest, PLAN_REVISION_JSON), json.dumps(plan, indent=2)
    )
    markdown_path = get_plan_markdown_path()
    if os.path.isfile(markdown_path):
        atomic_io.copy_file_atomic(markdown_path, os.path.join(dest, os.path.basename(markdown_path)))
    return True


def revert_plan_revision() -> Dict[str, Any]:
    """Restores the plan captured by the last :func:`snapshot_plan_revision`.

    The snapshot is a whole-plan "before", so this reverses the last revision rather than
    nudging one task's status. ``save_plan_state`` is used rather than a raw store write so
    the restored state goes through the same canonicalisation and metric recomputation as
    every other write, and the markdown is recompiled from it rather than trusted as-is.
    """
    snap_path = os.path.join(_plan_revision_dir(), PLAN_REVISION_JSON)
    if not os.path.isfile(snap_path):
        return validated(PlanMutationResult, {"success": False, "error": "There is no captured plan revision to restore."})
    try:
        with open(snap_path, "r", encoding="utf-8") as handle:
            plan_dict = validated(PlanPayload, json.load(handle))
    except Exception as error:
        return validated(PlanMutationResult, {"success": False, "error": f"Could not read the captured revision: {error}"})

    saved = save_plan_state(plan_dict)
    recompiled = compile_plan_json_to_markdown(saved)
    return validated(PlanMutationResult, {
        "success": True,
        "plan_json": saved,
        "content": recompiled,
        "tree": saved.get("steps", []),
    })


# A single source file larger than this is reported by name and size instead of by
# content, so a diff view can never be handed a multi-megabyte blob.
MAX_DIFF_SOURCE_CHARS = 200_000


def _read_snapshot_text(path: Optional[str]) -> Optional[str]:
    """Read a text file, or return None when absent, oversized or not decodable."""
    if not path or not os.path.isfile(path):
        return None
    try:
        if os.path.getsize(path) > MAX_DIFF_SOURCE_CHARS:
            return None
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _unified_diff(filename: str, before: Optional[str], after: Optional[str]) -> List[str]:
    """Unified diff of a file's prior and current text, newline-free per line."""
    return list(difflib.unified_diff(
        (before or "").splitlines(),
        (after or "").splitlines(),
        fromfile="/dev/null" if before is None else f"a/{filename}",
        tofile="/dev/null" if after is None else f"b/{filename}",
        lineterm="",
    ))


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
        # A snapshot exists to be restored *from*, so a half-written one is worse than none:
        # the file it would replace is the user's, and the copy is all that stands behind it.
        atomic_io.copy_file_atomic(filepath, backup_path)
        meta[filename] = {"action": "modified", "backup": backup_path, "timestamp": time.time()}
    else:
        meta[filename] = {"action": "created", "backup": None, "timestamp": time.time()}

    validated_backup_meta(meta)
    atomic_io.write_text_atomic(meta_path, json.dumps(meta, indent=2))
    return backup_path if os.path.exists(filepath) else None


def task_diff(task_id: str) -> Dict[str, Any]:
    """Diff a task's recorded deliverables: snapshot against workspace.

    ``backup_file_for_task`` writes ``_meta.json`` beside the snapshots, recording for
    each file whether the task *modified* an existing file (keeping the prior copy) or
    *created* a new one. Reading that snapshot against the workspace file now yields the
    real before/after pair, so the change shown is the change the task actually made
    rather than a prose summary of it. Read-only: it never writes to the workspace.
    """
    task_key = str(task_id or "").strip()
    empty = {"files": [], "totals": {"files": 0, "added": 0, "removed": 0}}
    if not task_key:
        return validated(TaskDiffResult, {"success": False, "error": "No task id given.", "task_id": task_id, **empty})

    base_dir = get_project_dir()
    task_backup_dir = os.path.join(get_backup_dir(), task_key)
    meta_path = os.path.join(task_backup_dir, "_meta.json")
    if not os.path.isfile(meta_path):
        # Not every task writes files (analyze / recommend / update_plan never do).
        return validated(TaskDiffResult, {"success": True, "found": False, "task_id": task_key, **empty})

    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = validated_backup_meta(json.load(f))
    except Exception as e:
        return validated(TaskDiffResult, {"success": False, "error": f"Could not read backup metadata: {e}",
                "task_id": task_key, **empty})

    files: List[Dict[str, Any]] = []
    total_added = 0
    total_removed = 0

    for filename, info in meta.items():
        info = info if isinstance(info, dict) else {}
        action = info.get("action") or "modified"
        before = _read_snapshot_text(info.get("backup")) if action == "modified" else None
        after = _read_snapshot_text(os.path.join(base_dir, filename))

        diff_lines = _unified_diff(filename, before, after)
        added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
        total_added += added
        total_removed += removed

        files.append({
            "filename": filename,
            "action": action,
            "before": before,
            "after": after,
            "diff": diff_lines,
            "added": added,
            "removed": removed,
            # False when both sides are binary, oversized or gone, so the view can say
            # so instead of rendering an empty box with no explanation.
            "available": before is not None or after is not None,
        })

    files.sort(key=lambda item: item["filename"])
    return validated(TaskDiffResult, {
        "success": True,
        "found": True,
        "task_id": task_key,
        "files": files,
        "totals": {"files": len(files), "added": total_added, "removed": total_removed},
    })


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
                DB_FILENAME.lower(), IDENTITY_FILE.lower(), get_active_plan_filename().lower(),
                ".ds_store", "thumbs.db",
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

    return validated(AuditReport, {
        "in_sync": in_sync,
        "discrepancy_count": discrepancy_count,
        "completed_missing_files": completed_missing,
        "pending_existing_files": pending_existing,
        "untracked_files": untracked,
        "summary": summary
    })


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
    return validated(PlanMutationResult, {
        "success": True,
        "action": "plan_to_codebase",
        "modified_tasks": modified_tasks,
        "plan_json": saved,
        "content": recompiled,
        "tree": saved.get("steps", [])
    })


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
    return validated(PlanMutationResult, {
        "success": True,
        "action": "code_to_plan",
        "modified_tasks": modified_tasks,
        "plan_json": saved,
        "content": recompiled,
        "tree": saved.get("steps", [])
    })


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
        return validated(RollbackResult, {"success": False, "error": f"Task '{task_id}' not found in active plan."})

    old_status = target_task.get("status")
    target_task["status"] = "pending"
    target_task.setdefault("details", []).append(f"Rolled back from '{old_status}' to pending.")

    # Restore backups if present in .aleth_backups/<task_id>
    restored_files = []
    restore_errors = []
    task_backup_dir = os.path.join(get_backup_dir(), target_task.get("id", task_id))
    meta_path = os.path.join(task_backup_dir, "_meta.json")

    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = validated_backup_meta(json.load(f))
        except Exception as e:
            restore_errors.append(f"backup metadata unreadable: {e}")
            meta = {}
        for fname, info in meta.items():
            target_file = os.path.join(base_dir, fname)
            try:
                if info.get("action") == "modified" and info.get("backup") and os.path.exists(info["backup"]):
                    atomic_io.copy_file_atomic(info["backup"], target_file)
                    restored_files.append(f"{fname} (restored prior version)")
                elif info.get("action") == "created" and os.path.exists(target_file):
                    archive_path = target_file + ".rollback_bak"
                    shutil.move(target_file, archive_path)
                    restored_files.append(f"{fname} (archived to .rollback_bak)")
            except Exception as file_err:
                # A rollback that could not put a file back is not a success: the task's status
                # was reset but the workspace still holds the patched code (audit M7).
                restore_errors.append(f"{fname}: {file_err}")

    saved = save_plan_state(plan_dict)
    recompiled = compile_plan_json_to_markdown(saved)

    result = {
        "success": not restore_errors,
        "task_id": target_task.get("id"),
        "task_title": target_task.get("title"),
        "restored_files": restored_files,
        "restore_errors": restore_errors,
        "plan_json": saved,
        "content": recompiled,
        "tree": saved.get("steps", [])
    }
    if restore_errors:
        result["error"] = "Rollback could not restore: " + "; ".join(restore_errors)
    return validated(RollbackResult, result)
