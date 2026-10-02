"""Phases 20-21: the shadow workspace, the delta, and the merge boundary.

An agent that is allowed to write into the user's live working tree is an agent that can delete
it. Twenty phases of containment -- no network, a bounded container, a read-only install -- say
nothing about the file system if the container is handed the user's uncommitted work as a
read-write volume. This module is the file-system half of that boundary.

The model is deliberately small and path-shaped:

* :func:`create_staging` copies the host workspace into a *shadow* directory under the state
  root, keyed by the intent that is about to run. The copy is what the container mounts and what
  every execution write targets (``tools.workspace.get_execution_dir``). Phase 21 stops it
  copying dead weight: the file set is what *git* says belongs to the project, so a virtualenv, a
  ``node_modules`` tree and a build output never enter the shadow.
* :func:`compute_diff` is the delta between the shadow and the host: added, modified and deleted
  files, plus a capped unified patch. It is content-addressed, not git-based, so it works in a
  workspace that is not a repository.
* :func:`merge_staging` applies that delta to the host, once, and only when the caller has
  verified the plan is finished. :func:`purge_staging` throws the shadow away instead, and
  :func:`purge_orphaned_stagings` throws away the ones no run can reach any more (Phase 22). The
  apply happens under :func:`merge_lock`, a per-project write lock, so two merges cannot interleave.

Everything is a pure function over paths. There is no global here, no process and no daemon: the
module is a filesystem transaction, and the run lifecycle (``app.py``) is what decides when to
begin and end one.
"""

from __future__ import annotations

import contextlib
import dataclasses
import difflib
import hashlib
import json
import os
import shutil
import subprocess
import uuid
from typing import Any, Dict, FrozenSet, Iterable, Iterator, List, Optional, Set, Tuple

from tools import atomic_io
from tools.workspace import (
    GIT_TIMEOUT_SECONDS,
    IDENTITY_FILE,
    IGNORE_DIRS,
    get_active_plan_filename,
    get_plan_dir,
    state_dir,
)

# Where the shadows live, under the per-project state directory. Segregated from the user's tree
# by the same rule as every other piece of machine state (Phase 11): the engine never litters.
STAGING_SUBDIR = "staging"

# The per-project merge lock, beside the shadows it protects. A file, not a global: it is held
# across processes, so a second engine instance cannot merge the same tree concurrently.
MERGE_LOCK_FILE = "merge.lock"

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


class StagingLocked(StagingError):
    """Another merge holds this project's write lock.

    A *conflict*, not a fault: the operation is well-formed and the answer is "no, not while
    another merge is applying its delta". ``api.gateway`` maps it to a 409.
    """


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


def _posix_relative(path: str, root: str) -> str:
    """``path`` relative to ``root``, with forward slashes whatever the platform separator is."""
    return os.path.relpath(path, root).replace(os.sep, "/")


def _git(host_root: str, *args: str) -> Optional[bytes]:
    """One read-only git query in ``host_root``; ``None`` when git could not answer.

    Run with the sanitized child environment (``tools.env_sanitizer``): git is a child process and
    needs none of the host's credentials to say which files belong to the project. A list argv,
    never a shell.
    """
    from tools import env_sanitizer

    try:
        completed = subprocess.run(
            ["git", *args], cwd=host_root, capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS, env=env_sanitizer.sanitized_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _git_workspace_files(host_root: str) -> Optional[FrozenSet[str]]:
    """The paths git considers part of the project, or ``None`` when it is not a repository.

    The ignore rules are **not** re-implemented here. A hand-rolled matcher would have to
    reproduce negation, ``**``, nested ``.gitignore`` files, ``.git/info/exclude`` and the global
    excludes file -- a whole grammar, re-derived incorrectly -- and an ``rsync`` subprocess would
    have to be handed those same rules anyway. Git already knows them, and git is already a
    runtime dependency (``tools.workspace.project_id`` hashes a remote through it). So the filter
    is pushed down to ``git ls-files``: tracked files plus untracked files that are not ignored,
    which is exactly the project minus its dead weight.

    ``None`` means "not a git work tree, or git could not answer", and the caller falls back to a
    plain walk pruned by :data:`~tools.workspace.IGNORE_DIRS`.
    """
    inside = _git(host_root, "rev-parse", "--is-inside-work-tree")
    if inside is None or inside.strip() != b"true":
        return None
    listing = _git(host_root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    if listing is None:
        return None
    included: Set[str] = set()
    for chunk in listing.split(b"\0"):
        if not chunk:
            continue
        relative = os.fsdecode(chunk).replace("\\", "/").strip("/")
        if relative and not relative.startswith("../"):
            included.add(relative)
    return frozenset(included)


def _included_directories(files: FrozenSet[str]) -> FrozenSet[str]:
    """Every ancestor directory of an included file, so a walk can prune a whole foreign subtree."""
    directories: Set[str] = set()
    for relative in files:
        directory = os.path.dirname(relative)
        while directory:
            directories.add(directory)
            parent = os.path.dirname(directory)
            if parent == directory:
                break
            directory = parent
    return frozenset(directories)


def _copy_ignore(host_root: str, included: Optional[FrozenSet[str]]):
    """The ``shutil.copytree`` filter: prune the ignored, the engine's own, and the non-project."""
    managed = _engine_managed_files(host_root)
    keep_dirs = _included_directories(included) if included is not None else None

    def ignore(directory: str, names: List[str]) -> List[str]:
        at_top = os.path.abspath(directory) == os.path.abspath(host_root)
        dropped = []
        for name in names:
            if name in IGNORE_DIRS or name.startswith(".tmp") or name.startswith(".drive"):
                dropped.append(name)
                continue
            if at_top and name in managed:
                dropped.append(name)
                continue
            allowed, kept = included, keep_dirs
            if allowed is None or kept is None:
                continue
            absolute = os.path.join(directory, name)
            relative = _posix_relative(absolute, host_root)
            # A symlink to a directory is listed by git as a file, so it is judged as one.
            if os.path.islink(absolute) or not os.path.isdir(absolute):
                if relative not in allowed:
                    dropped.append(name)
            elif relative not in kept:
                dropped.append(name)
        return dropped

    return ignore


def _iter_files(
    root: str, host_root: str, *, included: Optional[FrozenSet[str]] = None
) -> Iterator[Tuple[str, str]]:
    """Yield ``(posix relative path, absolute path)`` for every project file under ``root``.

    ``included`` restricts the yield to the paths git considers part of the project. It is passed
    for the *host* side of a diff, so a gitignored file -- a build artefact, a virtualenv, the
    state database -- is never mistaken for something the agent deleted. The shadow is walked in
    full: it holds only the copied project plus whatever the agent added, and hiding an addition
    the agent made is the one thing a review surface must not do.
    """
    managed = _engine_managed_files(host_root)
    keep_dirs = _included_directories(included) if included is not None else None
    for current, dirs, files in os.walk(root):
        at_top = os.path.abspath(current) == os.path.abspath(root)
        kept = []
        for name in dirs:
            if name in IGNORE_DIRS or name.startswith(".tmp") or name.startswith(".drive"):
                continue
            if at_top and name in managed:
                continue
            if keep_dirs is not None and _posix_relative(os.path.join(current, name), root) not in keep_dirs:
                continue
            kept.append(name)
        dirs[:] = kept
        for name in files:
            if name == STAGING_MANIFEST or (at_top and name in managed):
                continue
            absolute = os.path.join(current, name)
            if os.path.islink(absolute):
                continue
            relative = _posix_relative(absolute, root)
            if included is not None and relative not in included:
                continue
            yield relative, absolute


def _digest(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _snapshot(root: str, host_root: str, *, included: Optional[FrozenSet[str]] = None) -> Dict[str, str]:
    """Every project file under ``root`` as ``relative path -> sha256``."""
    snapshot: Dict[str, str] = {}
    for relative, absolute in _iter_files(root, host_root, included=included):
        try:
            snapshot[relative] = _digest(absolute)
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

    The copy is the project as git sees it (``_git_workspace_files``), so the shadow carries the
    source and not a virtualenv, a dependency tree or a build output -- the dead weight that made
    a naive ``copytree`` an I/O bomb. Where the host is not a repository the walk falls back to
    :data:`~tools.workspace.IGNORE_DIRS`.
    """
    host = os.path.abspath(host_root)
    if not os.path.isdir(host):
        raise StagingError(f"the workspace {host!r} does not exist; nothing can be staged")
    resolved_id = str(staging_id or uuid.uuid4().hex[:STAGING_ID_LENGTH])
    path = os.path.join(staging_base(), resolved_id)
    if os.path.exists(path):
        shutil.rmtree(path, ignore_errors=True)
    try:
        shutil.copytree(host, path, ignore=_copy_ignore(host, _git_workspace_files(host)), symlinks=True)
    except OSError as error:
        raise StagingError(f"could not stage {host!r}: {error}") from error

    workspace = StagingWorkspace(
        staging_id=resolved_id,
        intent_id=str(intent_id or ""),
        plan_id=str(plan_id or ""),
        host_root=host,
        path=path,
    )
    atomic_io.write_text_atomic(
        os.path.join(path, STAGING_MANIFEST), json.dumps(workspace.to_manifest(), indent=2)
    )
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
    # Ordered by creation time, so a scan resolves to the newest match: a run can leave more than
    # one shadow behind, and the review view wants the latest, not an arbitrary directory name.
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
) -> Optional[StagingWorkspace]:
    """Resolve a shadow by its own id, or by the intent that created it. Never by plan.

    The intent is the execution identity (Phase 21), and it is the *only* key that maps one to
    one onto a shadow. A plan is a blueprint that can be executed more than once, so a shadow
    keyed to a plan would let one run read -- and merge -- another run's changes. A shadow whose
    intent is not the one being asked about is, deliberately, not found: an orphaned shadow is
    dead, and the review surface must not guess which run a person meant.
    """
    if staging_id:
        return load_staging(staging_id)
    if not intent_id:
        return None
    matches = [item for item in list_stagings() if item.intent_id == str(intent_id)]
    return matches[-1] if matches else None


def compute_diff(workspace: StagingWorkspace) -> Dict[str, Any]:
    """The delta between the shadow and the host: added, modified, deleted, and a patch."""
    included = _git_workspace_files(workspace.host_root)
    host = _snapshot(workspace.host_root, workspace.host_root, included=included)
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
    atomic_io.copy_file_atomic(os.path.join(staged_root, relative), destination)


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
    """Apply the shadow's delta to the host, once. Returns the counts that landed.

    Call under :func:`merge_lock`: this reads the host, computes the delta and writes it back, and
    that sequence is only atomic against another merge if the lock is held around it.
    """
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


@contextlib.contextmanager
def merge_lock():
    """Hold this project's merge lock: the write lock on the host working tree.

    A merge is a read-modify-write of the user's files, so two merges interleaved would each apply
    a delta computed against a tree the other has already changed. The lock is a file under the
    project's state directory, so it is per project and it outlives the process that took it. It
    is acquired **non-blocking**: a merge that finds it held is a conflict to report (a 409), not
    a request to queue behind an operation the user never asked to wait for. Raises
    :class:`StagingLocked`.
    """
    from filelock import FileLock, Timeout

    lock = FileLock(os.path.join(state_dir(), MERGE_LOCK_FILE))
    try:
        lock.acquire(timeout=0)
    except Timeout as error:
        raise StagingLocked("another merge holds the write lock on this project") from error
    try:
        yield lock
    finally:
        lock.release()


def purge_staging(staging_id: str) -> bool:
    """Delete a shadow. ``True`` when one was removed, ``False`` when there was nothing to remove."""
    path = os.path.join(staging_base(), str(staging_id))
    if not os.path.isdir(path):
        return False
    shutil.rmtree(path, ignore_errors=True)
    return True


def _staging_directories() -> List[str]:
    """Every directory under the staging base. Nothing else is ever written there.

    The merge lock lives at ``<state_dir>/merge.lock``, a level up, and a manifest's temporary
    sibling is created *inside* a shadow -- so an entry here is a shadow, and one whose manifest
    cannot be read is the residue of a copy that was killed before it could write one.
    """
    base = staging_base()
    try:
        names = os.listdir(base)
    except OSError:
        return []
    return sorted(name for name in names if os.path.isdir(os.path.join(base, name)))


def purge_orphaned_stagings(keep_intent_ids: Iterable[str]) -> List[str]:
    """Delete every shadow whose intent is not in ``keep_intent_ids``. Returns their ids.

    Which shadows are still meaningful is a *ledger* question, so the caller answers it (Phase 21
    keys a shadow to the execution that made it, never to a plan):

    * ``queued`` or ``running`` -- the run is using this shadow right now;
    * ``completed`` -- the delta is finished and waiting for a person to review and merge it.

    Anything else is dead. An intent that ``failed`` or was ``stopped`` can never be merged -- the
    gate refuses a plan that is not finished -- and one the ledger does not mention at all is what
    a reset, a deleted database or a killed copy leaves behind. Both are unreachable by every
    endpoint in ``api.operations``, so keeping them is a copy of the user's repository that only
    grows.

    Removal is idempotent: a directory that is already gone is not an error, it is the answer.
    """
    keep = {str(intent_id) for intent_id in keep_intent_ids if str(intent_id)}
    purged: List[str] = []
    for name in _staging_directories():
        workspace = load_staging(name)
        if workspace is not None and workspace.intent_id and workspace.intent_id in keep:
            continue
        if purge_staging(name):
            purged.append(name)
    return purged


__all__ = [
    "MAX_DIFF_CHARS",
    "MERGE_LOCK_FILE",
    "STAGING_MANIFEST",
    "STAGING_SUBDIR",
    "StagingError",
    "StagingLocked",
    "StagingWorkspace",
    "compute_diff",
    "create_staging",
    "find_staging",
    "list_stagings",
    "load_staging",
    "merge_lock",
    "merge_staging",
    "purge_orphaned_stagings",
    "purge_staging",
    "staging_base",
]
