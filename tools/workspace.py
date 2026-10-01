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

import hashlib
import os
import re
import subprocess
import uuid
from typing import Optional

# Default workspace directory. Resolved from the environment, because the workspace is a path
# *in the filesystem the Docker daemon sees*: a container bind-mounts it, and a container write
# through a Windows drive (9p DrvFs) destroys the host's permissions, so where the daemon runs on
# Linux this must be a Linux path.
#
# Anchored to an absolute location rather than the process working directory: the app can be
# launched from a shortcut, an IDE run configuration or a plain ``python app.py``, and a
# cwd-relative path would then silently point at a *different*, freshly-created empty directory --
# leaving the UI with no plan to render and every action control locked.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WORKSPACE_ENV = "ALETH_WORKSPACE_DIR"
DEFAULT_WORKSPACE_DIR = "~/workspaces/my_project"


def _resolve_project_dir() -> str:
    """The workspace directory: ``ALETH_WORKSPACE_DIR``, else the default.

    ``~`` is expanded because the default is written the way a person writes a path. A relative
    value (configured or default) is resolved against the repository root rather than the
    process cwd, for the reason above.
    """
    configured = (os.environ.get(WORKSPACE_ENV) or "").strip()
    candidate = os.path.expanduser(configured or DEFAULT_WORKSPACE_DIR)
    if not os.path.isabs(candidate):
        candidate = os.path.join(PROJECT_ROOT, candidate)
    return os.path.abspath(candidate)


PROJECT_DIR = _resolve_project_dir()
os.makedirs(PROJECT_DIR, exist_ok=True)

# Where the *plan* lives: the active markdown plan and the SQLite state store beside it.
#
# Deliberately separate from ``PROJECT_DIR``. The roadmap is the user's own document and
# belongs in the repository, where it is under version control; ``PROJECT_DIR`` is the
# sandbox that generated code, deliverables and backups land in, and it is gitignored. A
# plan kept in the gitignored sandbox has no history, and one kept in the repository is a
# file the user can edit, diff and revert like any other.
#
# Mutable so the test suite can point it at a throwaway directory: the suite must never
# read or write the real roadmap.
PLAN_DIR = PROJECT_ROOT

# Where *execution* writes land, as opposed to where the user's project is read from.
#
# ``PROJECT_DIR`` is the live working tree: the plan, the file explorer, the preview, the source
# spans and the agent listing all read it, and they must keep reading the user's real files while
# a run is in flight. An *agent's* writes are a different question. Phase 20 puts them in a shadow
# copy of the tree so a hallucinating Coder cannot overwrite or delete the user's uncommitted work;
# this pointer is what selects that copy. It is ``None`` -- and every write path resolves to
# ``PROJECT_DIR`` -- except for the duration of one staged run.
#
# A process-global, like the pointers above, and for the same reason: the alternative is threading
# a root through a dozen call sites that already reach this module. Runs are serialized by the
# server-side run lock, so there is never a second staged run to collide with.
EXECUTION_DIR: Optional[str] = None

# Where the *machine* state lives, keyed by the project it describes. Deliberately outside the
# user's repository.
#
# The plan pair is not a pair. ``PLAN.md`` is human-authored and git-tracked -- it belongs in the
# repository, where it can be reviewed, diffed and reverted. The SQLite store is volatile machine
# state that happens to describe the same project. Keeping them in one directory put a binary file
# in the user's working tree, guaranteed a merge conflict on every branch, and made a read-only
# install impossible. So the store lives here instead, and the directory is derived from the
# plan's *location* rather than its name: two checkouts keep separate state, and moving a project
# moves its state with it.
STATE_ENV = "ALETH_STATE_DIR"
DEFAULT_STATE_ROOT = "~/.aleth/state"
PROJECT_ID_LENGTH = 16

# The manifest a project can carry so its state follows it anywhere. Travels with the project, so
# it is the one identity that survives a move, a clone to another path, or a container mount.
IDENTITY_FILE = ".aleth_id"
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
GIT_TIMEOUT_SECONDS = 10


def state_root() -> str:
    """The directory the per-project state directories live under."""
    configured = (os.environ.get(STATE_ENV) or "").strip()
    return os.path.abspath(os.path.expanduser(configured or DEFAULT_STATE_ROOT))


def _identity_from_file(plan_dir: str) -> str:
    """Tier 1: an explicit ``.aleth_id`` beside the plan.

    The strongest answer, because it is the project's own declaration. A token that is not
    filesystem-safe is refused rather than sanitised -- a mangled id would silently become a
    *different* project's state directory.
    """
    try:
        with open(os.path.join(plan_dir, IDENTITY_FILE), "r", encoding="utf-8") as handle:
            token = handle.readline().strip()
    except OSError:
        return ""
    return token if _SAFE_ID_RE.match(token) else ""


def _git(cwd: str, *args: str) -> str:
    """A read-only git query. Returns "" for anything that did not answer.

    Run with the sanitized child environment (``tools.env_sanitizer``): git is a child process, and
    this needs none of the host's credentials to answer a question about a remote URL.
    """
    from tools import env_sanitizer

    try:
        completed = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            timeout=GIT_TIMEOUT_SECONDS, encoding="utf-8", errors="replace",
            env=env_sanitizer.sanitized_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _remote_repo_name(remote: str) -> str:
    """The repository's name as the **remote** spells it, without the ``.git`` suffix.

    Deliberately not the local directory name: ``~/work/thing`` and ``/opt/thing-v2`` are the same
    repository in two locations, and a location is the one thing this identity must not depend on.
    """
    tail = remote.rstrip("/").rsplit("/", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


# A git remote, in any of its spellings. Split into scheme, optional user, host and path, because
# the *only* parts that identify a project are the host and the path.
_GIT_URL_RE = re.compile(
    r"^(?:(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://)?"   # https://, ssh://, git:// -- optional
    r"(?:(?P<user>[^@/]+)@)?"                          # git@ -- optional
    r"(?P<host>[^:/]+)"                                # github.com
    r"[:/]"                                            # scp form (:), or a plain url (/)
    r"(?P<path>.+?)"                                   # org/repo
    r"(?:\.git)?/?$",                                  # the .git suffix and a trailing slash
    re.VERBOSE,
)


def canonical_git_remote(remote: str) -> str:
    """One project, one string -- whatever spelling the remote was cloned with.

    ``git@github.com:org/repo.git`` and ``https://github.com/org/repo.git`` are the *same project*,
    and hashing the raw remote would give them two different ids: Developer A's state would be
    orphaned the moment Developer B cloned the repository. So the spelling is reduced to what
    actually identifies the project -- host, organisation, repository:

    * the scheme is dropped (``https://``, ``ssh://``, ``git://``);
    * the user is dropped (``git@``);
    * the scp separator ``:`` becomes ``/``;
    * the ``.git`` suffix and any trailing slash are dropped;
    * the host is lowercased, since hostnames are case-insensitive.

    An unrecognised spelling is lowercased and returned rather than refused: an exotic remote should
    still get a stable identity, just a less canonical one.
    """
    text = str(remote or "").strip()
    match = _GIT_URL_RE.match(text)
    if match is None:
        return text.lower()
    return f"{match.group('host').lower()}/{match.group('path').strip('/')}"


def _identity_from_git(plan_dir: str) -> str:
    """Tier 2: the repository's identity -- stable across moves, clones and mounts.

    ``sha256(canonical remote + repository name)`` describes *which project this is* rather than
    where it happens to sit, which is exactly what state must be keyed on. The remote is
    canonicalised first (see :func:`canonical_git_remote`), so two developers who cloned the same
    repository over different protocols share one identity.
    """
    toplevel = _git(plan_dir, "rev-parse", "--show-toplevel")
    if not toplevel:
        return ""
    remote = _git(plan_dir, "config", "--get", "remote.origin.url")
    if not remote:
        return ""
    canonical = canonical_git_remote(remote)
    material = f"{canonical}\n{_remote_repo_name(canonical)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:PROJECT_ID_LENGTH]


def _ignore_identity_file(plan_dir: str) -> None:
    """Keep the minted id out of ``git status``, if there is a ``.gitignore`` to add it to."""
    path = os.path.join(plan_dir, ".gitignore")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            existing = handle.read()
    except OSError:
        return
    if any(line.strip() == IDENTITY_FILE for line in existing.splitlines()):
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(("" if existing.endswith("\n") else "\n") + IDENTITY_FILE + "\n")
    except OSError:
        pass


def _mint_identity(plan_dir: str) -> str:
    """Tier 3: mint an id, record it beside the plan, and keep it out of git's way."""
    token = uuid.uuid4().hex
    try:
        with open(os.path.join(plan_dir, IDENTITY_FILE), "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
    except OSError:
        return ""
    _ignore_identity_file(plan_dir)
    return token


def project_id(plan_dir: str = None) -> str:
    """A stable id for the project a plan directory describes.

    Deliberately **not** derived from the filesystem path. A path changes when a project is moved,
    cloned somewhere else, or bind-mounted into a container -- and state keyed on it is orphaned
    every time. Resolution, best first:

    1. ``.aleth_id`` beside the plan: explicit, and it travels with the project;
    2. the git remote: ``sha256(remote url + repository name)``, which survives a move or a mount
       because it describes the project rather than its location;
    3. a minted UUID written to ``.aleth_id``, so the answer is stable from then on;
    4. only when 1-3 are impossible -- a writable-by-nobody directory that is not a repository --
       the absolute path. The least good answer, and the only one reported as such.
    """
    resolved = os.path.abspath(plan_dir or get_plan_dir())
    explicit = _identity_from_file(resolved)
    if explicit:
        return explicit
    bound = _identity_from_git(resolved)
    if bound:
        return bound
    minted = _mint_identity(resolved)
    if minted:
        return minted
    print(
        f"[State] no stable project id for {resolved!r}: it is not a repository, carries no "
        f"{IDENTITY_FILE}, and cannot be written to. Falling back to its path, which will not "
        "survive a move."
    )
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:PROJECT_ID_LENGTH]


def state_dir(plan_dir: str = None) -> str:
    """The machine-state directory for a plan: ``<state_root>/<project_id>``."""
    path = os.path.join(state_root(), project_id(plan_dir))
    os.makedirs(path, exist_ok=True)
    return path

# Dynamic Active Plan File
ACTIVE_PLAN_FILE = "PLAN.md"

# Snapshot folder used to back deliverables up before a task modifies them.
BACKUP_SUBDIR = ".aleth_backups"

# Directories that are never surfaced to agents, the file explorer, or the audit engine.
IGNORE_DIRS = {
    BACKUP_SUBDIR, ".git", "__pycache__", ".venv", "venv", "node_modules",
    ".pytest_cache", ".idea", ".vscode", ".gemini"
}


def get_project_dir() -> str:
    """Return the absolute path of the current project workspace."""
    global PROJECT_DIR
    return os.path.abspath(PROJECT_DIR)


def get_execution_dir() -> str:
    """Where execution writes land: the staging root while a run is staged, else the project.

    Every *write* the agent can cause -- the MCP filesystem server's root, the container's bind
    mount, the approved artifact's apply target, the test runner's working directory -- resolves
    through this. Every *read* of the user's own code keeps ``get_project_dir``.
    """
    global EXECUTION_DIR
    return os.path.abspath(EXECUTION_DIR or PROJECT_DIR)


def set_execution_dir(path: Optional[str]) -> Optional[str]:
    """Point execution at ``path`` for the duration of one staged run, or clear it with ``None``."""
    global EXECUTION_DIR
    EXECUTION_DIR = os.path.abspath(path) if path else None
    return EXECUTION_DIR


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


def get_plan_dir() -> str:
    """Return the absolute path of the directory the plan files live in."""
    global PLAN_DIR
    return os.path.abspath(PLAN_DIR)


def set_plan_dir(new_path: str) -> str:
    """Point the plan at a different directory and re-hydrate state from it.

    Called when the workspace changes, so the plan follows the project folder rather than
    staying pinned to whichever directory was active before, and by the test suite, which
    must work against a throwaway plan rather than the real roadmap. Imported lazily for
    the same circular-import reason as ``set_project_dir``.
    """
    global PLAN_DIR
    abs_path = os.path.abspath(new_path)
    os.makedirs(abs_path, exist_ok=True)
    PLAN_DIR = abs_path
    from tools.plan_state import load_plan_state
    load_plan_state(force_sync=True)
    return abs_path


def get_plan_markdown_path() -> str:
    """Absolute path of the active markdown plan."""
    return os.path.join(get_plan_dir(), get_active_plan_filename())


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


def list_plan_files() -> list[str]:
    """Finds the `.md` plan files in the plan directory.

    Candidacy is decided by *content*, not by filename. A document is offered when it
    parses as the plan AST (``tools.plan_parser.check_plan_structure``: a title, a section
    and at least one milestone). That keeps a README, a changelog or a planning note out
    of the switcher without a hardcoded list of names, and lets any real roadmap in
    whatever it is called -- so a tracker that *is* milestone-shaped is offered too.

    The active plan is always included, even when it does not parse: it is what the app is
    currently working on, and the Normalization Gate exists to fix its shape.

    The parser is imported lazily: ``tools.plan_parser`` imports this module, so a
    module-level import here would close an import cycle.
    """
    from tools.plan_parser import check_plan_structure

    base_dir = get_plan_dir()
    if not os.path.exists(base_dir):
        return []

    active = get_active_plan_filename().lower()
    md_files = []
    for f in os.listdir(base_dir):
        if not f.lower().endswith(".md") or not os.path.isfile(os.path.join(base_dir, f)):
            continue
        if f.lower() == active:
            md_files.append(f)
            continue
        try:
            with open(os.path.join(base_dir, f), "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            continue
        if check_plan_structure(content)["structured"]:
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
