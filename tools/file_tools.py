"""Compatibility facade for the workspace tooling package.

Historically every plan/file/audit helper lived in this one module. It has been
decomposed by concern into focused siblings:

    workspace.py    - where work happens (project dir + active plan pointer)
    plan_parser.py  - markdown <-> plan dict translation (pure)
    plan_state.py   - dual-sync plan.json <-> PLAN.md persistence
    file_ops.py     - read_file / write_file / append_to_file / listing
    recovery.py     - backups, codebase audit, sync resolution, rollback

This module re-exports the full previous public surface so existing imports such
as ``from tools.file_tools import load_plan_state`` keep working unchanged.
"""

from tools.file_ops import (
    ENV_FILENAME,
    MAX_ENVIRONMENT_VARIABLES,
    MAX_FILE_READ_CHARS,
    MAX_PREVIEW_CHARS,
    PREVIEW_FILENAME,
    all_file_tools,
    append_to_file,
    list_workspace_files,
    read_environment_variables,
    read_file,
    read_preview_source,
    read_source,
    write_file,
)
from tools.plan_parser import (
    check_plan_structure,
    compile_plan_json_to_markdown,
    parse_markdown_to_plan_dict,
    parse_plan_tree,
)
from tools.plan_state import (
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
    rollback_task_state,
    task_diff,
)
from tools.workspace import (
    BACKUP_SUBDIR,
    IGNORE_DIRS,
    PLAN_JSON_FILE,
    get_active_plan_filename,
    get_plan_dir,
    get_plan_json_path,
    get_project_dir,
    list_plan_files,
    set_active_plan_filename,
    set_plan_dir,
    set_project_dir,
    walk_workspace,
)

__all__ = [
    # workspace
    "get_project_dir",
    "set_project_dir",
    "get_active_plan_filename",
    "set_active_plan_filename",
    "get_plan_dir",
    "set_plan_dir",
    "PROJECT_DIR",
    "ACTIVE_PLAN_FILE",
    "get_plan_json_path",
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
    "sync_plan_on_disk",
    "update_plan_task_status",
    # file operations
    "read_file",
    "read_source",
    "write_file",
    "append_to_file",
    "list_workspace_files",
    "all_file_tools",
    "MAX_FILE_READ_CHARS",
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
    # constants
    "PLAN_JSON_FILE",
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
