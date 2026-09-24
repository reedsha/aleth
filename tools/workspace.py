"""Workspace location and active-plan pointer.

This module owns the process-wide mutable state that describes *where* work
happens: the current project directory and the currently selected markdown plan
file. Every other module in ``tools`` reads that state through the accessors
below rather than importing the globals directly, so the pointers can be
re-pointed at runtime (workspace switching, plan switching) without stale copies.

It also centralises the directory-pruning rules used by both the workspace file
listing and the codebase audit, so both agree on exactly which folders are
invisible to the agents.
"""

import os

# Default workspace directory (resolved relative to the process working directory).
PROJECT_DIR = os.path.abspath("./my_project_workspace")
os.makedirs(PROJECT_DIR, exist_ok=True)

# Dynamic Active Plan File
ACTIVE_PLAN_FILE = "PLAN.md"
PLAN_JSON_FILE = "plan.json"

# Snapshot folder used to back deliverables up before a task modifies them.
BACKUP_SUBDIR = ".deepagents_backups"

# Directories that are never surfaced to agents, the file explorer, or the audit engine.
IGNORE_DIRS = {
    BACKUP_SUBDIR, ".git", "__pycache__", ".venv", "venv", "node_modules",
    ".pytest_cache", ".idea", ".vscode", ".gemini"
}


def get_project_dir() -> str:
    """Return the absolute path of the current project workspace."""
    global PROJECT_DIR
    return os.path.abspath(PROJECT_DIR)


def set_project_dir(new_path: str) -> str:
    """Set the project workspace directory dynamically."""
    global PROJECT_DIR
    abs_path = os.path.abspath(new_path)
    os.makedirs(abs_path, exist_ok=True)
    PROJECT_DIR = abs_path
    # Synchronize plan state for the new workspace.
    # Imported lazily on purpose: plan_state depends on this module, so importing
    # it at module scope here would create a circular import.
    from tools.plan_state import load_plan_state
    load_plan_state()
    return abs_path


def get_active_plan_filename() -> str:
    """Returns the filename of the currently selected .md plan file."""
    global ACTIVE_PLAN_FILE
    return ACTIVE_PLAN_FILE


def set_active_plan_filename(filename: str) -> str:
    """Dynamically sets the active .md plan file path in memory and re-hydrates state."""
    global ACTIVE_PLAN_FILE
    clean_name = os.path.basename(filename.strip())
    if not clean_name.endswith(".md"):
        clean_name += ".md"
    ACTIVE_PLAN_FILE = clean_name
    # Re-hydrate plan.json from the newly selected markdown file.
    # Lazily imported to avoid the circular import described above.
    from tools.plan_state import load_plan_state
    load_plan_state(force_sync=True)
    return ACTIVE_PLAN_FILE


def get_plan_json_path() -> str:
    """Returns the absolute path to plan.json in the current workspace."""
    return os.path.join(get_project_dir(), PLAN_JSON_FILE)


def list_plan_files() -> list[str]:
    """Finds all .md plan files in the current workspace directory."""
    base_dir = get_project_dir()
    if not os.path.exists(base_dir):
        return []
    md_files = []
    for f in os.listdir(base_dir):
        if f.lower().endswith(".md") and os.path.isfile(os.path.join(base_dir, f)):
            md_files.append(f)
    return sorted(md_files)


def walk_workspace(base_dir: str):
    """Yields ``(root, dirs, files)`` for ``base_dir`` with internal/temp folders pruned.

    Single source of truth for the pruning rules shared by
    ``list_workspace_files`` and ``audit_codebase_plan_sync``.
    """
    if not os.path.exists(base_dir):
        return
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [
            d for d in dirs
            if d not in IGNORE_DIRS and not d.startswith(".tmp") and not d.startswith(".drive")
        ]
        yield root, dirs, files
