"""Plan session I/O: scaffolding a roadmap and reading the active plan.

These are the two operations that bridge "the user asked for a plan" and "the
workspace holds a plan". They own filename normalisation and the fallback rules
for a missing plan file, which is exactly the sort of policy that deserves to be
read on its own rather than buried between workflow branches.

Both functions are free of registry state, so they can be exercised directly.
"""

import os
import time
from typing import Any, Dict

from tools.plan_parser import compile_plan_json_to_markdown
from tools.plan_state import load_plan_state, read_plan_markdown, save_plan_state
from tools.workspace import (
    get_active_plan_filename,
    get_plan_markdown_path,
    list_plan_files,
    set_active_plan_filename,
)


def scaffold_plan_file(filename: str, project_idea: str) -> Dict[str, Any]:
    """
    Creates a new structured .md plan file and plan.json machine state based on user's project idea,
    stores the file in the workspace, sets it as active, and returns parsed tree.
    """
    clean_name = os.path.basename(filename.strip())
    if not clean_name.endswith(".md"):
        clean_name += ".md"

    set_active_plan_filename(clean_name)
    idea_clean = project_idea.strip() or "Custom Software Project"
    title = idea_clean.splitlines()[0][:60]

    plan_data = {
        "version": "1.0",
        "plan_file": clean_name,
        "title": title,
        # A new plan opens with a Global State Summary so the context slicer has the
        # plan's core facts to anchor on from the very first milestone.
        "state_summary": {
            "title": "🌍 Global State Summary",
            "bullets": [
                f"**Architecture:** {title} scaffolded by the Lead Architect from the initial directive.",
                "**Current Core State:** Milestone roadmap in place; no task executed yet.",
                "**Target Upgrade:** Execute, verify and roll back milestones from the plan tracker.",
            ],
        },
        "updated_at": time.time(),
        "sections": [
            {
                "id": "sec-1",
                "title": "1. Architecture & Setup",
                "tasks": [
                    {
                        "id": "task-1",
                        "section": "1. Architecture & Setup",
                        "title": "Initial specification and requirements formulation",
                        "status": "completed",
                        "details": [
                            f'Lead Architect: Defined scope for "{idea_clean[:80]}"',
                            f"Dynamic Plan: `{clean_name}`"
                        ],
                        "files": []
                    },
                    {
                        "id": "task-2",
                        "section": "1. Architecture & Setup",
                        "title": "Project scaffolding and runtime dependencies",
                        "status": "pending",
                        "details": [],
                        "files": []
                    }
                ]
            },
            {
                "id": "sec-2",
                "title": "2. Core Implementation",
                "tasks": [
                    {
                        "id": "task-3",
                        "section": "2. Core Implementation",
                        "title": "Build core domain models and application logic",
                        "status": "pending",
                        "details": ["Assigned: `coder-deep` (complex algorithms & backend logic)"],
                        "files": []
                    },
                    {
                        "id": "task-4",
                        "section": "2. Core Implementation",
                        "title": "Implement service endpoints and controllers",
                        "status": "pending",
                        "details": [],
                        "files": []
                    },
                    {
                        "id": "task-5",
                        "section": "2. Core Implementation",
                        "title": "Implement data validation and error handling",
                        "status": "pending",
                        "details": [],
                        "files": []
                    }
                ]
            },
            {
                "id": "sec-3",
                "title": "3. Testing & Verification",
                "tasks": [
                    {
                        "id": "task-6",
                        "section": "3. Testing & Verification",
                        "title": "Construct automated test suite with pytest",
                        "status": "pending",
                        "details": ["Assigned: `coder-standard` (tests & documentation)"],
                        "files": []
                    },
                    {
                        "id": "task-7",
                        "section": "3. Testing & Verification",
                        "title": "Verify functionality via restricted shell execution",
                        "status": "pending",
                        "details": [],
                        "files": []
                    },
                    {
                        "id": "task-8",
                        "section": "3. Testing & Verification",
                        "title": "Deliverable audit and deployment readiness check",
                        "status": "pending",
                        "details": [],
                        "files": []
                    }
                ]
            }
        ]
    }
    saved = save_plan_state(plan_data)
    content = compile_plan_json_to_markdown(saved)

    return {
        "success": True,
        "filename": clean_name,
        "content": content,
        "tree": saved.get("steps", []),
        "plan_json": saved,
        "plans": list_plan_files()
    }


def read_current_plan() -> Dict[str, Any]:
    """Fetches the active plan state from plan.json and PLAN.md."""
    active_name = get_active_plan_filename()
    plan_state = load_plan_state()
    content = read_plan_markdown()

    # Only consider a plan truly missing if the .md file doesn't exist on disk.
    # Never revert the active plan just because parsing returned zero steps --
    # that would undo a freshly-made plan switch (race condition).
    file_on_disk = os.path.isfile(get_plan_markdown_path())
    exists = file_on_disk and "does not exist yet" not in content

    if not file_on_disk:
        # File genuinely missing -- fall back to the first available plan
        available = list_plan_files()
        if available:
            active_name = set_active_plan_filename(available[0])
            plan_state = load_plan_state(force_sync=True)
            content = read_plan_markdown()
            exists = True

    return {
        "filename": active_name,
        "exists": exists,
        "content": content if exists else "",
        "tree": plan_state.get("steps", []),
        "plan_json": plan_state,
        "plans": list_plan_files()
    }
