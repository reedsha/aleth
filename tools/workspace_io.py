"""The frontend's file ground truth: the listing, the preview and the ``.env`` reader.

These are **not** LLM tools, and they are **not** the run's file access. They are reads of the
*user's project* -- what the explorer renders, what the preview shows, which ``.env`` names are
configured -- so they resolve against ``get_project_dir()`` and they must keep seeing the live tree
while a run is in flight.

The run's own file access lives in ``tools.execution_io``, bound to the shadow. The two halves were
one module until Phase 23, and sharing a resolver between callers with opposed security contracts is
what let the engine's deliverable writer resolve against the live project directory while every
execution path wrote into the shadow. The split is the fix: this module has no way to reach the
execution root, and that one has no way to reach this root.

**Containment is enforced here too**, by resolving both sides with ``Path.resolve()`` and comparing
them. A resolved-path comparison cannot be talked around: ``..``, a Windows backslash traversal, an
absolute path and a symlink pointing out of the tree all fail the one check.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from tools.git_status import workspace_vcs_status
from tools.workspace import PROJECT_ROOT, get_project_dir, walk_workspace

# The interface file the Coder agent writes into the workspace. The live preview renders this
# file, so its name is a contract between the agent's output and the preview pane.
PREVIEW_FILENAME = "ui_view.html"

# A ceiling on what is handed to the webview in one go. A generated interface far past this is
# truncated rather than allowed to stall the frame; the preview says so when it happens.
MAX_PREVIEW_CHARS = 1_500_000

# The app's own configuration file, loaded with ``load_dotenv()`` when the app starts.
ENV_FILENAME = ".env"

# A ceiling on how many names are reported. This is a developer's ``.env``, not the whole
# process environment; a runaway file must not turn the sidebar into a wall of text.
MAX_ENVIRONMENT_VARIABLES = 60


class PathDenied(ValueError):
    """A path resolved outside the workspace. Never a soft failure."""


def _resolve(filename: str) -> Path:
    """Resolve ``filename`` inside the workspace, or refuse.

    ``Path.resolve()`` on both sides is the whole guard: it normalises ``..``, expands a
    symlink to its target and makes an absolute argument absolute, so the containment test
    that follows cannot be fooled by the shape of the string.
    """
    root = Path(get_project_dir()).resolve()
    candidate = (root / str(filename or "")).resolve()
    if candidate != root and root not in candidate.parents:
        raise PathDenied(f"Path traversal denied: {filename!r} is outside the workspace.")
    return candidate


def list_workspace_files() -> list[dict]:
    """Every file in the workspace, excluding internal/temporary/cache folders.

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
        for name in files:
            if name.startswith(".tmp") or name.lower() in {".ds_store", "thumbs.db"}:
                continue
            full_path = os.path.join(root, name)
            rel_path = os.path.relpath(full_path, base_dir).replace("\\", "/")
            try:
                size = os.path.getsize(full_path)
            except OSError:
                size = 0
            files_list.append({
                "name": name,
                "path": rel_path,
                "size": size,
                "vcs": vcs.get(rel_path, ""),
            })
    files_list.sort(key=lambda entry: entry["path"])
    return files_list


def read_preview_source(filename: str = PREVIEW_FILENAME) -> dict:
    """Reads a workspace file as text for the live preview. Read-only.

    The preview renders through the bridge rather than pointing an iframe at a ``file://``
    URL: WebView2 does not reliably complete a local document load, and the frame then sits
    blank. Handing over the text sidesteps the document load entirely.

    A file that is simply absent is an ordinary answer (``found: False``), not an error -- the
    workspace legitimately holds no interface until a task builds one, and the preview has a
    message for exactly that case.
    """
    requested = str(filename or "").strip()
    # basename keeps the read inside the workspace even if a caller passes a path.
    safe = os.path.basename(requested)
    if not safe:
        return {"success": False, "found": False, "filename": "", "content": "",
                "truncated": False, "error": "No preview filename was given."}
    try:
        filepath = _resolve(safe)
        if not filepath.is_file():
            return {"success": True, "found": False, "filename": safe, "content": "",
                    "truncated": False, "error": ""}
        with open(filepath, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read(MAX_PREVIEW_CHARS + 1)
        truncated = len(content) > MAX_PREVIEW_CHARS
        return {"success": True, "found": True, "filename": safe,
                "content": content[:MAX_PREVIEW_CHARS], "truncated": truncated, "error": ""}
    except Exception as error:
        return {"success": False, "found": False, "filename": safe, "content": "",
                "truncated": False, "error": str(error)}


def read_environment_variables(env_path: Optional[str] = None) -> dict:
    """Names of the variables the app loads from ``.env``, plus whether each resolves.

    Never their values. ``OPENAI_API_KEY`` is one of these, so masking is done *here*, on the
    Python side: nothing sensitive crosses the bridge into the webview, where a devtools
    session could otherwise read it back out. The panel only needs to say which names are
    configured and whether the environment actually supplies them, so a name, a set flag and a
    character count are the whole payload.

    An absent file is an ordinary answer (``found: False``): a checkout without a ``.env`` is a
    legitimate state, not a failure to shout about.
    """
    path = env_path or os.path.join(PROJECT_ROOT, ENV_FILENAME)
    if not os.path.isfile(path):
        return {"success": True, "found": False, "filename": ENV_FILENAME,
                "variables": [], "error": ""}
    try:
        names: list[str] = []
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
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
    except Exception as error:
        return {"success": False, "found": False, "filename": ENV_FILENAME,
                "variables": [], "error": str(error)}
