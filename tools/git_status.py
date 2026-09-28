"""Version-control status for the workspace file tree.

The left pane's tree used to print each file's size, which is not the number a developer
scans for. The signal worth that space is *what changed*, and this module supplies it in one
vocabulary no matter where the workspace lives:

* inside its **own** git work tree -- the porcelain letters, so a real project gets real
  answers, and a folder that merely sits inside a parent repository (the app's sandbox does)
  is not mistaken for one;
* anywhere else (the app's own gitignored sandbox, or a plain folder) -- the deliverable
  snapshots under ``.deepagents_backups/`` are read instead, so the badge still means "an
  agent touched this" rather than going permanently blank.

Only two letters ever leave here: ``M`` (changed) and ``U`` (new). The tree is a glance, not
a ``git status`` replacement, so every other porcelain state collapses into one of the two.

Read-only. No ``git`` binary, a non-repo directory or any error degrades to an empty mapping
rather than a crash, because a missing badge is cosmetic and a failed listing is not.
"""

import json
import os
import subprocess
from typing import Dict, Optional

from tools.workspace import BACKUP_SUBDIR, get_project_dir

# git runs as a subprocess; a pathological repository must not hang the file listing.
GIT_TIMEOUT_SECONDS = 4

# Porcelain XY -> the letter the tree shows. Anything absent falls through to "M": the path
# was reported by git, so something about it changed, and "changed" is the safe reading.
_PORCELAIN_LETTERS = {
    "??": "U",
    "A ": "U",
    "AM": "U",
    "AD": "U",
}


def _git(base_dir: str, *args: str) -> Optional[str]:
    """Run git in the workspace, returning stdout, or ``None`` on absence, error or non-repo."""
    try:
        res = subprocess.run(
            ["git", "-C", base_dir, *args],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout


def _git_status(base_dir: str) -> Optional[Dict[str, str]]:
    """Git's porcelain letters for ``base_dir``, or ``None`` when git does not apply.

    ``None`` and ``{}`` are different answers and both matter: ``None`` means "not this
    workspace's own repository, ask something else", while ``{}`` means "its repository has
    nothing changed".

    Only a workspace that *is* a work-tree root counts. A folder that merely sits inside
    someone else's repository -- which the app's own gitignored sandbox does, since it lives
    under the project repo -- has no status of its own: git would answer with the parent
    repo's changes, whose paths do not even name files in this workspace. Those workspaces
    take the snapshot path instead.
    """
    toplevel = _git(base_dir, "rev-parse", "--show-toplevel")
    if not toplevel or not toplevel.strip():
        return None
    if os.path.normcase(os.path.realpath(base_dir)) != os.path.normcase(os.path.realpath(toplevel.strip())):
        return None

    porcelain = _git(base_dir, "status", "--porcelain")
    if porcelain is None:
        return None

    found: Dict[str, str] = {}
    for line in porcelain.splitlines():
        # "XY path": two status columns, a space, then the path.
        if len(line) < 4:
            continue
        xy = line[:2]
        path = line[3:].strip().strip('"')
        # A rename or copy reads "old -> new"; the badge belongs on the path that exists now.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        found[path.replace("\\", "/")] = _PORCELAIN_LETTERS.get(xy, "M")
    return found


def _backup_status(base_dir: str) -> Dict[str, str]:
    """Letters derived from the task snapshots, for a workspace git cannot speak for.

    ``backup_file_for_task`` records, per task and file, whether the file was *modified*
    (keeping the prior copy) or *created*. That is the only "what changed" signal that means
    anything in the app's own sandbox.
    """
    backup_root = os.path.join(base_dir, BACKUP_SUBDIR)
    if not os.path.isdir(backup_root):
        return {}

    modified = set()
    created = set()
    for task_id in os.listdir(backup_root):
        meta_path = os.path.join(backup_root, task_id, "_meta.json")
        if not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        for filename, info in meta.items():
            info = info if isinstance(info, dict) else {}
            action = info.get("action") or "modified"
            rel = str(filename).replace("\\", "/")
            if action == "created":
                created.add(rel)
            else:
                modified.add(rel)

    # A file created and later modified reads as modified: the stronger of the two facts wins.
    return {rel: "M" for rel in modified} | {rel: "U" for rel in created - modified}


def workspace_vcs_status(base_dir: Optional[str] = None) -> Dict[str, str]:
    """Map of workspace-relative path -> a one-letter status (``"M"`` or ``"U"``).

    Git when the workspace is a work tree, the task snapshots otherwise. An empty map simply
    means "nothing to badge".
    """
    base = os.path.abspath(base_dir or get_project_dir())
    git = _git_status(base)
    if git is not None:
        return git
    return _backup_status(base)
