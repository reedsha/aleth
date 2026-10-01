"""Phase 20: the shadow workspace, the delta, and the merge boundary.

An agent that is allowed to write into the user's live working tree is an agent that can delete
it. Twenty phases of containment -- no network, a bounded container, a read-only install -- say
nothing about the file system if the container is handed the user's uncommitted work as a
read-write volume. This module is the file-system half of that boundary.

The model is deliberately small and path-shaped:

* :func:`create_staging` copies the host workspace into a *shadow* directory under the state
  root, keyed by the intent that is about to run. The copy is what the container mounts and what
  every execution write targets (``tools.workspace.get_execution_dir``).
* :func:`compute_diff` is the delta between the shadow and the host: added, modified and deleted
  files, plus a capped unified patch. It is content-addressed, not git-based, so it works in a
  workspace that is not a repository.
* :func:`merge_staging` applies that delta to the host, once, and only when the caller has
  verified the plan is finished. :func:`purge_staging` throws the shadow away instead.

Everything is a pure function over paths. There is no global here, no process and no daemon: the
module is a filesystem transaction, and the run lifecycle (``app.py``) is what decides when to
begin and end one.
"""

from __future__ import annotations

import dataclasses
import difflib
import hashlib
import json
import os
import shutil
import uuid
from typing import Any, Dict, FrozenSet, Iterator, List, Optional, Tuple

from tools.workspace import (
    IDENTITY_FILE,
    IGNORE_DIRS,
    get_active_plan_filename,
    get_plan_dir,
    state_dir,
)

# Where the shadows live, under the per-project state directory. Segregated from the user's tree
# by the same rule as every other piece of machine state (Phase 11): the engine never litters.
STAGING_SUBDIR = "staging"

# The manifest at the root of a shadow, so a restart can resolve a staging id -- or find the one
# belonging to an intent -- without an in-memory registry. Excluded from the copy and the delta.
STAGING_MANIFEST = ".aleth_staging.json"

STAGING_ID_LENGTH = 12

# The largest patch the diff will assemble, so a review view can never be handed a multi-megabyte
# blob. Consistent with ``tools.recovery.MAX_DIFF_SOURCE_CHARS``.
MAX_DIFF_CHARS = 200_000

# A file larger than this is reported by name and size instead of by content.
MAX_DIFF_SOURCE_BYTES = 2_000_000

_CHUNK = 1024 * 1024


class StagingError(RuntimeError):
    """A staging operation could not be carried out (a missing shadow, an unwritable host)."""


@dataclasses.dataclass(frozen=True)
class StagingWorkspace:
    """One shadow workspace and the run it belongs to."""

    staging_id: str
    intent_id: str
    plan_id: str
    host_root: str
    path: str

    def to_manifest(self) -> Dict[str, str]:
        return {
            "staging_id": self.staging_id,
            "intent_id": self.intent_id,
            "plan_id": self.plan_id,
            "host_root": self.host_root,
        }


def staging_base() -> str:
    """The directory every shadow for this project lives under: ``<state_dir>/staging``."""
    base = os.path.join(state_dir(), STAGING_SUBDIR)
    os.makedirs(base, exist_ok=True)
    return base


def _manifest_path(staging_id: str) -> str:
    return os.path.join(staging_base(), str(staging_id), STAGING_MANIFEST)


def _engine_managed_files(host_root: str) -> FrozenSet[str]:
    """Top-level names the engine owns and that must never enter the shadow or the delta.

    The plan pair is the case that matters. ``save_plan_state`` writes ``PLAN.md`` and
    ``plan.json`` directly to the plan directory -- which is the host workspace whenever a user
    selects their repository as the workspace -- so a shadow copy of them would age out of date
    the moment the engine saved the plan, and a merge would then write the stale plan back over
    the fresh one. ``.aleth_id`` is the project's own identity manifest, and the staging manifest
    is our own bookkeeping.
    """
    names = {STAGING_MANIFEST, IDENTITY_FILE}
    if os.path.abspath(host_root) == os.path.abspath(get_plan_dir()):
        names.add("plan.json")
        names.add(get_active_plan_filename())
    return frozenset(names)


def _copy_ignore(host_root: str):
    managed = _engine_managed_files(host_root)

    def ignore(directory: str, names: List[str]) -> List[str]:
        at_top = os.path.abspath(directory) == os.path.abspath(host_root)
        dropped = []
        for name in names:
            if name in IGNORE_DIRS or name.startswith(".tmp") or name.startswith(".drive"):
                dropped.append(name)
            elif at_top and name in managed:
                dropped.append(name)
        return dropped

    return ignore


def _iter_files(root: str, host_root: str) -> Iterator[Tuple[str, str]]:
    """Yield ``(relative_path, absolute_path)`` for every file in ``root`` worth diffing."""
    managed = _engine_managed_files(host_root)
    for current, dirs, files in os.walk(root):
        dirs[:] = [
            name for name in dirs
            if name not in IGNORE_DIRS and not name.startswith(".tmp") and not name.startswith(".drive")
        ]
        at_top = os.path.abspath(current) == os.path.abspath(root)
        for name in files:
            if at_top and name in managed:
                continue
            if name == STAGING_MANIFEST:
                continue
            absolute = os.path.join(current, name)
            if os.path.islink(absolute):
                continue
            yield os.path.relpath(absolute, root), absolute


def _digest(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _snapshot(root: str, host_root: str) -> Dict[str, str]:
    """Every file under ``root`` as ``relative path -> sha256``."""
    snapshot: Dict[str, str] = {}
    for relative, absolute in _iter_files(root, host_root):
        try:
            snapshot[relative.replace(os.sep, "/")] = _digest(absolute)
        except OSError:
            continue
    return snapshot


def _read_text(path: str) -> Optional[str]:
    try:
        if os.path.getsize(path) > MAX_DIFF_SOURCE_BYTES:
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def _unified_patch(host_root: str, staged_root: str, added: List[str],
                   modified: List[str], deleted: List[str]) -> str:
    """A capped unified diff of the delta, for the review view. Non-text files are named only."""
    lines: List[str] = []
    budget = MAX_DIFF_CHARS

    def emit(filename: str, before: Optional[str], after: Optional[str]) -> None:
        nonlocal budget
        if before is None and after is None:
            lines.append(f"# {filename}: binary or oversized; compare by hash\n")
            return
        chunk = list(difflib.unified_diff(
            (before or "").splitlines(),
            (after or "").splitlines(),
            fromfile="/dev/null" if before is None else f"a/{filename}",
            tofile="/dev/null" if after is None else f"b/{filename}",
            lineterm="",
        ))
        for line in chunk:
            if budget <= 0:
                return
            lines.append(line)
            budget -= len(line) + 1

    for name in added:
        emit(name, None, _read_text(os.path.join(staged_root, name)))
    for name in modified:
        emit(name, _read_text(os.path.join(host_root, name)),
             _read_text(os.path.join(staged_root, name)))
    for name in deleted:
        emit(name, _read_text(os.path.join(host_root, name)), None)

    if budget <= 0:
        lines.append("... [TRUNCATED BY ALETH ENGINE] ...")
    return "\n".join(lines)


def create_staging(
    host_root: str,
    *,
    intent_id: str = "",
    plan_id: str = "",
    staging_id: Optional[str] = None,
) -> StagingWorkspace:
    """Copy ``host_root`` into a fresh shadow and record the manifest that names it.

    ``staging_id`` is generated when it is not supplied. An existing directory at that id is
    replaced: a shadow is a scratch copy of the present, and a second copy of the same run is a
    bug, not a merge candidate.
    """
    host = os.path.abspath(host_root)
    if not os.path.isdir(host):
        raise StagingError(f"the workspace {host!r} does not exist; nothing can be staged")
    resolved_id = str(staging_id or uuid.uuid4().hex[:STAGING_ID_LENGTH])
    path = os.path.join(staging_base(), resolved_id)
    if os.path.exists(path):
        shutil.rmtree(path, ignore_errors=True)
    try:
        shutil.copytree(host, path, ignore=_copy_ignore(host), symlinks=True)
    except OSError as error:
        raise StagingError(f"could not stage {host!r}: {error}") from error

    workspace = StagingWorkspace(
        staging_id=resolved_id,
        intent_id=str(intent_id or ""),
        plan_id=str(plan_id or ""),
        host_root=host,
        path=path,
    )
    with open(os.path.join(path, STAGING_MANIFEST), "w", encoding="utf-8") as handle:
        json.dump(workspace.to_manifest(), handle, indent=2)
    return workspace


def load_staging(staging_id: str) -> Optional[StagingWorkspace]:
    """The shadow recorded under ``staging_id``, or ``None`` when there is no such manifest."""
    try:
        with open(_manifest_path(staging_id), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    path = os.path.join(staging_base(), str(staging_id))
    return StagingWorkspace(
        staging_id=str(data.get("staging_id") or staging_id),
        intent_id=str(data.get("intent_id") or ""),
        plan_id=str(data.get("plan_id") or ""),
        host_root=str(data.get("host_root") or ""),
        path=path,
    )


def list_stagings() -> List[StagingWorkspace]:
    """Every shadow currently on disk, oldest first. Reads the manifests, not a registry."""
    base = staging_base()
    found: List[StagingWorkspace] = []
    try:
        entries = os.listdir(base)
    except OSError:
        return found
    # Ordered by creation time, so ``find_staging``'s "the last one" really is the newest: a plan
    # can carry more than one shadow (a manual approval, then an ``execute_plan``), and the review
    # view wants the latest, not an arbitrary directory name.
    def _created(name: str) -> float:
        try:
            return os.path.getmtime(os.path.join(base, name))
        except OSError:
            return 0.0

    for name in sorted(entries, key=_created):
        workspace = load_staging(name)
        if workspace is not None:
            found.append(workspace)
    return found


def find_staging(
    *,
    intent_id: Optional[str] = None,
    staging_id: Optional[str] = None,
    plan_id: Optional[str] = None,
) -> Optional[StagingWorkspace]:
    """Resolve a shadow by its own id, by the intent that created it, or by its plan.

    The intent and plan lookups are scans because the sets are tiny and it keeps the on-disk
    manifest the single source of truth rather than a second index that can disagree with it.
    The plan fallback is what lets the review view ask "is anything staged for the plan I am
    looking at?" without knowing the run's id.
    """
    if staging_id:
        return load_staging(staging_id)
    candidates = list_stagings()
    if intent_id:
        matches = [item for item in candidates if item.intent_id == str(intent_id)]
        if matches:
            return matches[-1]
    if plan_id:
        matches = [item for item in candidates if item.plan_id == str(plan_id)]
        if matches:
            return matches[-1]
    return None


def compute_diff(workspace: StagingWorkspace) -> Dict[str, Any]:
    """The delta between the shadow and the host: added, modified, deleted, and a patch."""
    host = _snapshot(workspace.host_root, workspace.host_root)
    staged = _snapshot(workspace.path, workspace.host_root)
    added = sorted(set(staged) - set(host))
    deleted = sorted(set(host) - set(staged))
    modified = sorted(
        name for name in (set(staged) & set(host)) if staged[name] != host[name]
    )
    return {
        "staging_id": workspace.staging_id,
        "intent_id": workspace.intent_id,
        "plan_id": workspace.plan_id,
        "host_root": workspace.host_root,
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "counts": {"added": len(added), "modified": len(modified), "deleted": len(deleted)},
        "clean": not (added or modified or deleted),
        "patch": _unified_patch(workspace.host_root, workspace.path, added, modified, deleted),
    }


def _apply_one(host_root: str, staged_root: str, relative: str, *, copy: bool) -> None:
    destination = os.path.join(host_root, relative)
    if not copy:
        try:
            os.remove(destination)
        except FileNotFoundError:
            pass
        _prune_empty(os.path.dirname(destination), stop=host_root)
        return
    os.makedirs(os.path.dirname(destination) or host_root, exist_ok=True)
    shutil.copy2(os.path.join(staged_root, relative), destination)


def _prune_empty(directory: str, *, stop: str) -> None:
    """Remove directories a delete emptied, without ever walking above the host root."""
    stop = os.path.abspath(stop)
    current = os.path.abspath(directory)
    while current != stop and current.startswith(stop + os.sep):
        try:
            if os.listdir(current):
                return
            os.rmdir(current)
        except OSError:
            return
        current = os.path.dirname(current)


def merge_staging(workspace: StagingWorkspace) -> Dict[str, Any]:
    """Apply the shadow's delta to the host, once. Returns the counts that landed."""
    delta = compute_diff(workspace)
    applied = {"added": 0, "modified": 0, "deleted": 0}
    for name in delta["added"]:
        _apply_one(workspace.host_root, workspace.path, name, copy=True)
        applied["added"] += 1
    for name in delta["modified"]:
        _apply_one(workspace.host_root, workspace.path, name, copy=True)
        applied["modified"] += 1
    for name in delta["deleted"]:
        _apply_one(workspace.host_root, workspace.path, name, copy=False)
        applied["deleted"] += 1
    return {
        "success": True,
        "staging_id": workspace.staging_id,
        "intent_id": workspace.intent_id,
        "host_root": workspace.host_root,
        "applied": applied,
    }


def purge_staging(staging_id: str) -> bool:
    """Delete a shadow. ``True`` when one was removed, ``False`` when there was nothing to remove."""
    path = os.path.join(staging_base(), str(staging_id))
    if not os.path.isdir(path):
        return False
    shutil.rmtree(path, ignore_errors=True)
    return True


__all__ = [
    "MAX_DIFF_CHARS",
    "STAGING_MANIFEST",
    "STAGING_SUBDIR",
    "StagingError",
    "StagingWorkspace",
    "compute_diff",
    "create_staging",
    "find_staging",
    "list_stagings",
    "load_staging",
    "merge_staging",
    "purge_staging",
    "staging_base",
]
