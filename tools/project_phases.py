"""Phase 29: the deterministic phases around the model loop -- setup, then verification.

A code-writing agent needs two things an airgapped sandbox alone cannot give it: its project's
dependencies *installed*, and a verdict on whether what it wrote actually runs. Neither is a job for
the model.

**Setup runs before the loop, with egress, and the model is not involved.** A ``package.json`` or a
``requirements.txt`` cannot be installed in a container with no network, and an agent that cannot
install its dependencies cannot test its own work. So the orchestrator reads the project's manifests
and runs the install itself -- deterministically, from a fixed table, never from anything the model
wrote -- and only then does the loop start.

The egress lives for the life of that one container, and it is *not* the container the model ever
gets. There is no long-lived container to ``docker network disconnect``: this perimeter is ephemeral
by design (one container per command, ``--rm``), so the setup container's network namespace is
destroyed with it -- a stronger guarantee than detaching it, because there is no window in which a
later command could inherit it.

**Verification runs after the loop, with no egress at all.** A diff that does not compile is not a
review candidate, and asking a human to spot a syntax error in a patch is asking them to be a
compiler. The project's own suite decides; a failure fails the intent and leaves the shadow
*unmergeable*, so the review surface never offers a merge for work that does not run.

**Detection is a fixed table and an explicit override.** Nothing here reads a file the *model* wrote
to decide what to execute: the table names manifests the project already had, and the override is a
file a human authored.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional, Tuple

from tools import docker_sandbox

# The declaration a human writes when the table guesses wrong. Read from the *plan* directory, which
# is the user's repository -- so it travels with the project and is reviewable in a diff.
DECLARATION_FILE = ".aleth_phases.json"

# How long a phase may take. Generous: an install is minutes, and a first run has no cache.
DEFAULT_SETUP_TIMEOUT_SECONDS = 900
DEFAULT_VERIFY_TIMEOUT_SECONDS = 900

# ``(manifest, command)``, first match wins. The commands are the ecosystem's own conventional
# install/verify, and each one is run with the workspace as the working directory.
SETUP_COMMANDS: Tuple[Tuple[str, str], ...] = (
    ("package.json", "npm install --no-audit --no-fund"),
    ("requirements.txt", "pip install -r requirements.txt"),
    ("pyproject.toml", "pip install -e ."),
)

# Verification needs a *suite*, not just a manifest: ``package.json`` only counts when it declares a
# test script, and a Python project counts when it carries the usual test markers.
VERIFY_COMMANDS: Tuple[Tuple[str, str], ...] = (
    ("pytest.ini", "python -m pytest -q"),
    ("pyproject.toml", "python -m pytest -q"),
    ("tox.ini", "python -m pytest -q"),
)


def _read_json(path: str) -> Any:
    """The parsed JSON, or ``None`` when it is missing or malformed. Never raises."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _declared(project_dir: str, phase: str) -> str:
    """The command a human declared for ``phase``, or ``""``."""
    data = _read_json(os.path.join(project_dir, DECLARATION_FILE))
    if not isinstance(data, dict):
        return ""
    value = data.get(phase)
    return str(value).strip() if isinstance(value, str) else ""


def _declared_timeout(project_dir: str, phase: str, fallback: int) -> int:
    data = _read_json(os.path.join(project_dir, DECLARATION_FILE))
    if not isinstance(data, dict):
        return fallback
    raw = data.get(f"{phase}_timeout_seconds")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return fallback


def detect_setup_command(project_dir: str) -> str:
    """The install command for ``project_dir``, or ``""`` when the project needs none.

    The explicit declaration wins; otherwise the first manifest in :data:`SETUP_COMMANDS` that is
    present decides. A project with none of them has nothing to install, and saying so is better
    than inventing a command for it.
    """
    declared = _declared(project_dir, "setup")
    if declared:
        return declared
    for manifest, command in SETUP_COMMANDS:
        if os.path.isfile(os.path.join(project_dir, manifest)):
            return command
    return ""


def detect_verify_command(project_dir: str) -> str:
    """The command that decides whether ``project_dir``'s work is fit to review, or ``""``.

    ``package.json`` counts only when it declares a ``test`` script -- ``npm test`` with no script is
    an error, not a verdict -- and a Python project counts when it carries ``pytest.ini``,
    ``pyproject.toml`` or ``tox.ini``, which is where a suite is declared rather than merely present.
    """
    declared = _declared(project_dir, "verify")
    if declared:
        return declared
    package = _read_json(os.path.join(project_dir, "package.json"))
    if isinstance(package, dict):
        scripts = package.get("scripts")
        if isinstance(scripts, dict) and str(scripts.get("test") or "").strip():
            return "npm test --silent"
    for manifest, command in VERIFY_COMMANDS:
        if os.path.isfile(os.path.join(project_dir, manifest)):
            return command
    return ""


def run_setup(project_dir: str, *, timeout: Optional[int] = None) -> Optional[Any]:
    """Install the project's dependencies **with egress**, before the model is involved.

    Returns the isolated result, or ``None`` when the project declares nothing to install. Raises
    :class:`~tools.docker_sandbox.SandboxError` when the perimeter cannot run it -- a setup phase
    that could not happen is not something to paper over, because the loop that follows will fail
    for a missing dependency and look like the model's fault.
    """
    command = detect_setup_command(project_dir)
    if not command:
        return None
    return docker_sandbox.run_isolated(
        command,
        cwd=project_dir,
        timeout=int(timeout or _declared_timeout(project_dir, "setup", DEFAULT_SETUP_TIMEOUT_SECONDS)),
        network=docker_sandbox.SETUP_NETWORK,
    )


def run_verification(project_dir: str, *, timeout: Optional[int] = None) -> Optional[Any]:
    """Run the project's suite **airgapped**, as the verdict on a finished run.

    Returns the isolated result, or ``None`` when the project declares no suite. The network is the
    sealed default: verification executes the workspace's own code, which is the code the model just
    wrote, so it is the last place egress should be available.
    """
    command = detect_verify_command(project_dir)
    if not command:
        return None
    return docker_sandbox.run_isolated(
        command,
        cwd=project_dir,
        timeout=int(timeout or _declared_timeout(project_dir, "verify", DEFAULT_VERIFY_TIMEOUT_SECONDS)),
        network=docker_sandbox.SANDBOX_NETWORK,
    )


def describe(project_dir: str) -> str:
    """A one-line, value-free summary of what the two phases would run. For the log."""
    setup = detect_setup_command(project_dir)
    verify = detect_verify_command(project_dir)
    return (
        f"setup={'yes' if setup else 'none'}, verify={'yes' if verify else 'none'}"
    )


__all__ = [
    "DECLARATION_FILE",
    "SETUP_COMMANDS",
    "VERIFY_COMMANDS",
    "describe",
    "detect_setup_command",
    "detect_verify_command",
    "run_setup",
    "run_verification",
]
