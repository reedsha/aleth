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


def state_root() -> str:
    """The directory the per-project state directories live under."""
    configured = (os.environ.get(STATE_ENV) or "").strip()
    return os.path.abspath(os.path.expanduser(configured or DEFAULT_STATE_ROOT))


def project_id(plan_dir: str = None) -> str:
    """A stable id for the project a plan directory describes.

    ``sha256`` of the absolute plan directory, truncated: deterministic, filesystem-safe, and a
    function of *where the plan is*, so state follows the project rather than a name two projects
    could share.
    """
    resolved = os.path.abspath(plan_dir or get_plan_dir())
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
