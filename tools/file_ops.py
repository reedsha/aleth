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
from tools.git_status import workspace_vcs_status
from tools.workspace import PROJECT_ROOT, get_active_plan_filename, get_project_dir, walk_workspace

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


def read_source(filename: str) -> str:
    """Reads a workspace file in full, with no token-optimised truncation.

    ``read_file`` middle-truncates a large non-plan file, which is right when the
    content is only being shown to a model. Paths that will write the content
    *back* -- a bug patch, say -- must not use that view: the omitted middle would
    be silently deleted when the (truncated) text is written to disk. Returns ""
    when the file is missing or unreadable, so a caller can tell there was nothing
    to read rather than writing an error string as file content.
    """
    filepath = os.path.join(get_project_dir(), filename)
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


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
    """Returns a list of files in the current workspace, excluding internal/temporary/cache folders.

    Each entry carries a ``vcs`` letter (``"M"``/``"U"``, empty when unchanged) for the
    sidebar tree. Git answers when the workspace is a work tree and the task snapshots do
    otherwise -- see ``tools.git_status``; either way the listing itself is unchanged.
    """
    files_list = []
    base_dir = get_project_dir()
    if not os.path.exists(base_dir):
        return []
    vcs = workspace_vcs_status(base_dir)
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
                "size": size,
                "vcs": vcs.get(rel_path, ""),
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


# The app's own configuration file, loaded with ``load_dotenv()`` when the app starts.
# Resolved against the app root rather than the process working directory, so the panel
# reports the same variables however the app was launched.
ENV_FILENAME = ".env"

# A ceiling on how many names are reported. This is a developer's ``.env``, not the whole
# process environment; a runaway file must not turn the sidebar into a wall of text.
MAX_ENVIRONMENT_VARIABLES = 60


def read_environment_variables(env_path: str = None) -> dict:
    """Names of the variables the app loads from ``.env``, plus whether each resolves.

    Never their values. ``OPENAI_API_KEY`` is one of these, so masking is done *here*,
    on the Python side: nothing sensitive crosses the bridge into the webview, where a
    devtools session could otherwise read it back out. The panel only needs to say which
    names are configured and whether the environment actually supplies them, so a name, a
    set flag and a character count are the whole payload.

    An absent file is an ordinary answer (``found: False``): a checkout without a ``.env``
    is a legitimate state, not a failure to shout about.
    """
    path = env_path or os.path.join(PROJECT_ROOT, ENV_FILENAME)
    if not os.path.isfile(path):
        return {"success": True, "found": False, "filename": ENV_FILENAME,
                "variables": [], "error": ""}
    try:
        names: list[str] = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name = line.split("=", 1)[0].strip()
                # ``export NAME=value`` is a valid dotenv line.
                if name.lower().startswith("export "):
                    name = name[len("export "):].strip()
                if not name or name in names:
                    continue
                names.append(name)
                if len(names) >= MAX_ENVIRONMENT_VARIABLES:
                    break

        variables = []
        for name in names:
            value = os.environ.get(name)
            variables.append({
                "name": name,
                "set": bool(value),
                "length": len(value) if value else 0,
            })
        return {"success": True, "found": True, "filename": ENV_FILENAME,
                "variables": variables, "error": ""}
    except Exception as e:
        return {"success": False, "found": False, "filename": ENV_FILENAME,
                "variables": [], "error": str(e)}


# Export a bundle for easy importing
all_file_tools = [read_file, write_file, append_to_file]
