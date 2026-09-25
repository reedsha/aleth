"""Generic workspace file tools exposed to the agents.

These are the ``@tool``-decorated primitives the Coder agents call directly
(``read_file`` / ``write_file`` / ``append_to_file``) plus the workspace listing
used by the UI file explorer and the audit engine.

Reads are token-optimised: large non-plan files are middle-truncated so they
cannot blow the model context window. Plan files and JSON state are exempt.
"""

import os

from langchain_core.tools import tool

from tools.plan_state import load_plan_state
from tools.workspace import get_active_plan_filename, get_project_dir, walk_workspace

MAX_FILE_READ_CHARS = 6000


@tool
def read_file(filename: str) -> str:
    """Reads the contents of a file in the project directory with context window token optimization."""
    filepath = os.path.join(get_project_dir(), filename)
    if not os.path.exists(filepath):
        return f"File {filename} does not exist yet."
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        # Token efficiency optimization for large non-plan files
        if len(content) > MAX_FILE_READ_CHARS and not filename.lower().endswith(".md") and not filename.lower().endswith(".json"):
            half = MAX_FILE_READ_CHARS // 2
            head = content[:half]
            tail = content[-half:]
            return (
                f"{head}\n\n... [Omitted {len(content) - MAX_FILE_READ_CHARS} characters "
                f"from {filename} to optimize context window] ...\n\n{tail}"
            )
        return content
    except Exception as e:
        return f"Error reading file {filename}: {str(e)}"


@tool
def write_file(filename: str, content: str) -> str:
    """Writes text to a file, creating it if it doesn't exist. Overwrites existing content."""
    filepath = os.path.join(get_project_dir(), filename)
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True) if os.path.dirname(filepath) else None
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        # If writing to active plan file, trigger auto-sync to plan.json
        if os.path.basename(filename).lower() == get_active_plan_filename().lower():
            load_plan_state(force_sync=True)
        return f"Successfully wrote {len(content)} characters to {filename}."
    except Exception as e:
        return f"Error writing file {filename}: {str(e)}"


@tool
def append_to_file(filename: str, content: str) -> str:
    """Adds text to the very end of an existing file without deleting what is already there."""
    filepath = os.path.join(get_project_dir(), filename)
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True) if os.path.dirname(filepath) else None
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(f"\n{content}\n")
        # If appending to active plan file, trigger auto-sync to plan.json
        if os.path.basename(filename).lower() == get_active_plan_filename().lower():
            load_plan_state(force_sync=True)
        return f"Successfully appended to {filename}."
    except Exception as e:
        return f"Error appending to file {filename}: {str(e)}"


def list_workspace_files() -> list[dict]:
    """Returns a list of files in the current workspace, excluding internal/temporary/cache folders."""
    files_list = []
    base_dir = get_project_dir()
    if not os.path.exists(base_dir):
        return []
    for root, _dirs, files in walk_workspace(base_dir):
        for f in files:
            if f.startswith(".tmp") or f.lower() in {".ds_store", "thumbs.db"}:
                continue
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, base_dir).replace("\\", "/")
            try:
                size = os.path.getsize(full_path)
            except OSError:
                size = 0
            files_list.append({
                "name": f,
                "path": rel_path,
                "size": size
            })
    files_list.sort(key=lambda x: x["path"])
    return files_list


# The interface file the Coder agent writes into the workspace. The live preview renders
# this file, so its name is a contract between the agent's output and the preview pane.
PREVIEW_FILENAME = "ui_view.html"

# A ceiling on what is handed to the webview in one go. A generated interface far past this
# is truncated rather than allowed to stall the frame; the preview says so when it happens.
MAX_PREVIEW_CHARS = 1_500_000


def read_preview_source(filename: str = PREVIEW_FILENAME) -> dict:
    """Reads a workspace file as text for the live preview. Read-only.

    The preview renders through the bridge rather than pointing an iframe at a ``file://``
    URL: WebView2 does not reliably complete a local document load, and the frame then sits
    blank. Handing over the text sidesteps the document load entirely.

    A file that is simply absent is an ordinary answer (``found: False``), not an error --
    the workspace legitimately holds no interface until a task builds one, and the preview
    has a message for exactly that case.
    """
    requested = str(filename or "").strip()
    # basename keeps the read inside the workspace even if a caller passes a path.
    safe = os.path.basename(requested)
    if not safe:
        return {"success": False, "found": False, "filename": "", "content": "",
                "truncated": False, "error": "No preview filename was given."}
    try:
        filepath = os.path.join(get_project_dir(), safe)
        if not os.path.isfile(filepath):
            return {"success": True, "found": False, "filename": safe, "content": "",
                    "truncated": False, "error": ""}
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(MAX_PREVIEW_CHARS + 1)
        truncated = len(content) > MAX_PREVIEW_CHARS
        return {"success": True, "found": True, "filename": safe,
                "content": content[:MAX_PREVIEW_CHARS], "truncated": truncated, "error": ""}
    except Exception as e:
        return {"success": False, "found": False, "filename": safe, "content": "",
                "truncated": False, "error": str(e)}


# Export a bundle for easy importing
all_file_tools = [read_file, write_file, append_to_file]
