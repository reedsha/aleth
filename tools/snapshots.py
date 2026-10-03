"""Phase 35: temporal snapshots of the shadow workspace, so a run can be rewound.

A shadow workspace bounds the *blast radius* of a hallucinating agent: it can damage a scratch copy
and nothing else. It does not, on its own, let anyone **undo** that damage. An agent that corrupts a
module at step 12 and is interrupted at step 15 leaves a shadow that is simply wrong, and the
operator's only recourse is to reject the whole run.

This module is the rewind. A shadow carries its own **localized git repository** -- created inside
the shadow, never touching the user's history or their git identity -- and every step that changes a
file is committed and tagged with the step number. :func:`revert_to_step` restores the tree exactly.

Why git and not a pile of directory copies: git already has a content-addressed object store, cheap
incremental commits, and an exact restore (``reset --hard`` plus ``clean``). Re-implementing that
with copies would multiply the disk cost by the step count -- the I/O bomb Phase 21 removed.

Everything here is best-effort by contract. A snapshot that cannot be taken must never stop the run
that is producing the work, so a failed commit returns ``""`` rather than raising; only a *revert*,
which a person asked for, raises when it cannot be carried out.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Dict, List, Optional

from tools.workspace import GIT_TIMEOUT_SECONDS

# The tag namespace. A snapshot is a tag on a commit, so a step resolves to a tree without walking
# the log, and ``git tag -l 'aleth-step-*'`` is the whole index.
STEP_TAG_PREFIX = "aleth-step-"

# The state the shadow was handed in, before the agent touched anything. A step like any other, so
# "revert to before this run changed a file" is expressible.
INITIAL_STEP = 0

# The commit identity, supplied by the environment rather than read from the machine's git config.
# The engine must not depend on -- or write to -- the operator's global git identity, and a CI
# runner has none at all. ``GIT_CONFIG_GLOBAL``/``_SYSTEM`` point at the null device so the host's
# config cannot leak in either.
_IDENTITY: Dict[str, str] = {
    "GIT_AUTHOR_NAME": "Aleth Engine",
    "GIT_AUTHOR_EMAIL": "engine@aleth.local",
    "GIT_COMMITTER_NAME": "Aleth Engine",
    "GIT_COMMITTER_EMAIL": "engine@aleth.local",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_OPTIONAL_LOCKS": "0",
}


class SnapshotError(RuntimeError):
    """A snapshot could not be taken or restored."""


def _env() -> Dict[str, str]:
    from tools import env_sanitizer

    return env_sanitizer.sanitized_environment(extra=_IDENTITY)


def _git(root: str, *args: str) -> Optional[subprocess.CompletedProcess]:
    """One git command in ``root``. ``None`` when git is missing or could not run at all."""
    try:
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True,
            timeout=GIT_TIMEOUT_SECONDS, encoding="utf-8", errors="replace", env=_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _run(root: str, *args: str) -> Optional[str]:
    """One git command's stdout, or ``None`` when it failed."""
    completed = _git(root, *args)
    if completed is None or completed.returncode != 0:
        return None
    return completed.stdout


def tag_for(step: int) -> str:
    """The tag a step's snapshot carries."""
    return f"{STEP_TAG_PREFIX}{int(step)}"


def is_repository(root: str) -> bool:
    """Whether ``root`` already carries a snapshot repository."""
    return bool(root) and os.path.isdir(os.path.join(root, ".git"))


def init_snapshots(root: str) -> bool:
    """Create the shadow's snapshot repository and commit its starting state.

    Idempotent: a repository that already exists is left alone (``False``). Returns ``True`` only
    when a repository was created. Never raises -- a shadow without snapshots is a run without a
    rewind, which is worse than nothing but is not a reason to refuse the run.
    """
    if not root or not os.path.isdir(root):
        return False
    if is_repository(root):
        return False
    if _run(root, "init", "-q") is None:
        return False
    _run(root, "add", "-A")
    # ``--allow-empty`` so the baseline exists even for an empty shadow: the step index must start
    # at 0 whether or not the workspace had files.
    _run(root, "commit", "-q", "--allow-empty", "-m", "aleth: the workspace as staged")
    _run(root, "tag", "-f", tag_for(INITIAL_STEP))
    return True


def has_changes(root: str) -> bool:
    """Whether the working tree differs from the last snapshot. Never raises."""
    if not is_repository(root):
        return False
    status = _run(root, "status", "--porcelain", "--untracked-files=all")
    return bool(status and status.strip())


def commit_step(root: str, step: int, message: str = "") -> str:
    """Commit the current state and tag it as ``step``. Returns the sha, or ``""``.

    Silent and best-effort: ``""`` means nothing changed, or the commit could not be taken. The
    caller treats both the same way -- it carries on.
    """
    if not has_changes(root):
        return ""
    _run(root, "add", "-A")
    note = str(message or "").strip() or f"step {int(step)}"
    completed = _git(root, "commit", "-q", "-m", f"aleth: {note}")
    if completed is None or completed.returncode != 0:
        return ""
    _run(root, "tag", "-f", tag_for(step))
    return (_run(root, "rev-parse", "HEAD") or "").strip()


def steps(root: str) -> List[Dict[str, Any]]:
    """Every snapshot step, oldest first, as ``{step, sha, message}``. ``[]`` when there are none."""
    if not is_repository(root):
        return []
    listing = _run(root, "tag", "-l", f"{STEP_TAG_PREFIX}*")
    if not listing:
        return []
    found: List[Dict[str, Any]] = []
    for line in listing.splitlines():
        tag = line.strip()
        if not tag.startswith(STEP_TAG_PREFIX):
            continue
        raw = tag[len(STEP_TAG_PREFIX):]
        if not raw.isdigit():
            continue
        found.append({
            "step": int(raw),
            "sha": (_run(root, "rev-list", "-n", "1", tag) or "").strip(),
            "message": (_run(root, "log", "-1", "--format=%s", tag) or "").strip(),
        })
    found.sort(key=lambda entry: entry["step"])
    return found


def next_step(root: str) -> int:
    """The next step number for this shadow: one past the highest snapshot it carries.

    Monotonic for the life of the *shadow*, not the pass. The autonomous loop drives several
    planner passes under one intent and they share one shadow, so numbering per pass would collide
    -- pass two's step 1 would overwrite pass one's.
    """
    found = steps(root)
    return (found[-1]["step"] + 1) if found else 1


def latest_step_at_or_before(root: str, step: int) -> Optional[int]:
    """The highest snapshot step ``<= step``, or ``None``. What a revert resolves to."""
    target = int(step)
    candidate: Optional[int] = None
    for entry in steps(root):
        if entry["step"] <= target:
            candidate = entry["step"]
    return candidate


def truncate_after(root: str, step: int) -> List[int]:
    """Drop every snapshot *after* ``step``: the abandoned timeline. Returns the steps dropped.

    A ledger is a log of the **active causal timeline**. When a rewind abandons steps 3-5, their
    future does not exist any more -- and leaving their tags behind is what would make the resumed
    run collide with its own abandoned future: ``next_step`` reads the tags, so the agent would
    resume at step 6 while the ledger's rows for 3-5 were gone, and a later rewind to 4 would find a
    tree with no memory to go with it.

    Dropping the tags makes the commits they pointed at unreachable, so git collects them; the
    working tree was already restored by :func:`revert_to_step`. Linear history is the contract:
    if a person wants step 5, they should not rewind to step 2.
    """
    target = int(step)
    abandoned = [entry["step"] for entry in steps(root) if entry["step"] > target]
    for number in abandoned:
        _run(root, "tag", "-d", tag_for(number))
    return abandoned


def revert_to_step(root: str, step: int) -> Dict[str, Any]:
    """Restore the shadow to the state as of ``step``. Returns what happened.

    The tree is restored exactly: tracked files come back from the snapshot, files the agent added
    afterwards are removed by the reset, and files it created without ever committing them are
    cleaned. A step with no snapshot of its own resolves to the newest snapshot at or before it --
    *the state as of that step* -- because a step that changed nothing has no tree of its own.

    Raises :class:`SnapshotError` when there is no snapshot to return to, or the restore fails.
    """
    if not is_repository(root):
        raise SnapshotError("this workspace has no snapshots to revert to")
    resolved = latest_step_at_or_before(root, step)
    if resolved is None:
        raise SnapshotError(f"there is no snapshot at or before step {int(step)}")
    reset = _git(root, "reset", "--hard", "-q", tag_for(resolved))
    if reset is None or reset.returncode != 0:
        raise SnapshotError(f"the workspace could not be restored to step {resolved}")
    # ``-fdx``: remove untracked files *and* ignored ones. The shadow only ever contained files git
    # listed, so anything ignored inside it was written by the agent and belongs to the state being
    # undone. ``git clean`` never touches ``.git`` itself.
    _git(root, "clean", "-fdxq")
    # ...and the abandoned future goes with it. Strict truncation, on both sides of the rewind.
    abandoned = truncate_after(root, resolved)
    return {
        "reverted": True,
        "step": resolved,
        "requested_step": int(step),
        "abandoned_steps": abandoned,
    }


__all__ = [
    "INITIAL_STEP",
    "STEP_TAG_PREFIX",
    "SnapshotError",
    "commit_step",
    "has_changes",
    "init_snapshots",
    "is_repository",
    "latest_step_at_or_before",
    "next_step",
    "revert_to_step",
    "steps",
    "tag_for",
    "truncate_after",
]
