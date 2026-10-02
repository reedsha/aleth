"""Compatibility facade for the workspace tooling package.

Historically every plan/file/audit helper lived in this one module. It has been
decomposed by concern into focused siblings:

    workspace.py    - where work happens (project dir + active plan pointer)
    plan_parser.py  - markdown <-> plan dict translation (pure)
    plan_state.py   - SQLite-backed plan state + the read-only markdown projection
    execution_io.py - the run's file I/O, bound to the execution root (the shadow)
    workspace_io.py - the frontend's file reads (listing/preview/env), bound to the project
    recovery.py     - backups, codebase audit, sync resolution, rollback

The two I/O modules are deliberately separate (Phase 23): their callers have opposed security
contracts, so ``execution_io`` cannot import ``get_project_dir`` and ``workspace_io`` cannot reach
the execution root. This façade re-exports both halves so existing imports keep working.

This module re-exports the full previous public surface so existing imports such
as ``from tools.file_tools import load_plan_state`` keep working unchanged.

It no longer re-exports a model tool catalog: the file and shell tools are MCP tools,
bound per run from a live server (``tools/mcp_tools.py``), not a static list of
``@tool``-decorated Python wrappers.
"""

from tools.plan_parser import (
    check_plan_structure,
    compile_plan_json_to_markdown,
    parse_markdown_to_plan_dict,
    parse_plan_tree,
)
from tools.plan_state import (
    PlanWriteError,
    load_plan_state,
    plan_structure_report,
    save_plan_state,
    sync_plan_on_disk,
    update_plan_task_status,
)
from tools.recovery import (
    audit_codebase_plan_sync,
    backup_file_for_task,
    get_backup_dir,
    resolve_sync_code_to_plan,
    resolve_sync_plan_to_codebase,
    revert_plan_revision,
    rollback_task_state,
    snapshot_plan_revision,
    task_diff,
)
from tools.workspace import (
    BACKUP_SUBDIR,
    IGNORE_DIRS,
    get_active_plan_filename,
    get_execution_dir,
    get_plan_dir,
    get_project_dir,
    list_plan_files,
    set_active_plan_filename,
    set_execution_dir,
    set_plan_dir,
    set_project_dir,
    walk_workspace,
)
from tools.execution_io import (
    overwrite_source,
    read_source,
)
from tools.workspace_io import (
    ENV_FILENAME,
    MAX_ENVIRONMENT_VARIABLES,
    MAX_PREVIEW_CHARS,
    PREVIEW_FILENAME,
    list_workspace_files,
    read_environment_variables,
    read_preview_source,
)

__all__ = [
    # workspace
    "get_project_dir",
    "set_project_dir",
    "get_execution_dir",
    "set_execution_dir",
    "get_active_plan_filename",
    "set_active_plan_filename",
    "get_plan_dir",
    "set_plan_dir",
    "PROJECT_DIR",
    "ACTIVE_PLAN_FILE",
    "list_plan_files",
    "walk_workspace",
    "IGNORE_DIRS",
    # parser / compiler
    "parse_markdown_to_plan_dict",
    "compile_plan_json_to_markdown",
    "parse_plan_tree",
    "check_plan_structure",
    # state persistence
    "load_plan_state",
    "plan_structure_report",
    "save_plan_state",
    "PlanWriteError",
    "sync_plan_on_disk",
    "update_plan_task_status",
    # file operations (engine-internal; the model's file tools are MCP tools now)
    "read_source",
    "overwrite_source",
    "list_workspace_files",
    "read_preview_source",
    "PREVIEW_FILENAME",
    "MAX_PREVIEW_CHARS",
    "read_environment_variables",
    "ENV_FILENAME",
    "MAX_ENVIRONMENT_VARIABLES",
    # recovery / audit
    "get_backup_dir",
    "backup_file_for_task",
    "audit_codebase_plan_sync",
    "resolve_sync_plan_to_codebase",
    "resolve_sync_code_to_plan",
    "rollback_task_state",
    "task_diff",
    "snapshot_plan_revision",
    "revert_plan_revision",
    # constants
    "BACKUP_SUBDIR",
]


def __getattr__(name):
    """Serve the *live* runtime-value globals.

    ``PROJECT_DIR`` and ``ACTIVE_PLAN_FILE`` are mutable module-level pointers in
    ``tools.workspace``. Re-exporting them as plain attributes would hand out a
    frozen copy the moment they are re-pointed, so they are proxied instead.
    """
    if name == "PROJECT_DIR":
        from tools.workspace import PROJECT_DIR
        return PROJECT_DIR
    if name == "ACTIVE_PLAN_FILE":
        from tools.workspace import ACTIVE_PLAN_FILE
        return ACTIVE_PLAN_FILE
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
