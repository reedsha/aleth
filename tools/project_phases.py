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

# How long a phase may take. The setup phase is the one that matters: it runs with **egress**, so
# an install that hangs -- a poisoned manifest, a circular dependency, a registry that stopped
# answering -- would hold the intent queue's only worker forever. 120 s is generous for a real
# install and far too short for a hang. A human may raise it in the declaration, up to the ceiling.
DEFAULT_SETUP_TIMEOUT_SECONDS = 120
MAX_SETUP_TIMEOUT_SECONDS = 600
DEFAULT_VERIFY_TIMEOUT_SECONDS = 900

# The runtime images a project is verified in, selected from its own manifests (Phase 30).
#
# ``PYTHON_IMAGE`` is the engine's *own* sandbox image: it is built from ``docker/sandbox.Dockerfile``
# and carries the Python toolchain (pytest and friends), which a bare ``python:3.12-slim`` does not.
# ``NODE_IMAGE`` is a stock base pulled from its registry -- node and npm are the whole toolchain a
# JavaScript project needs, and they are not this engine's business to rebuild.
#
# There is deliberately no image that carries both. A runtime the project does not use is weight
# every project pays for, and a monolith grows into a CVE surface nobody audits.
PYTHON_RUNTIME = "python"
NODE_RUNTIME = "node"
PYTHON_IMAGE = docker_sandbox.DEFAULT_IMAGE
NODE_IMAGE = "node:20-alpine"

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


def declared_image(project_dir: str) -> str:
    """The image a human declared for this project, or ``""``.

    The escape hatch for a **polyglot** project: a repository with both a ``package.json`` and a
    ``requirements.txt`` has no single runtime this engine can pick for it, so it must say.
    """
    data = _read_json(os.path.join(project_dir, DECLARATION_FILE))
    if not isinstance(data, dict):
        return ""
    return str(data.get("image") or "").strip()


def _runtimes(project_dir: str) -> Tuple[str, ...]:
    """Which runtimes this project's manifests declare, in a stable order."""
    found = []
    if os.path.isfile(os.path.join(project_dir, "package.json")):
        found.append(NODE_RUNTIME)
    for manifest in ("requirements.txt", "pyproject.toml", "setup.py", "pytest.ini", "tox.ini"):
        if os.path.isfile(os.path.join(project_dir, manifest)):
            found.append(PYTHON_RUNTIME)
            break
    return tuple(found)


def detect_runtime(project_dir: str) -> str:
    """The runtime this project is built in: ``python``, ``node``, or a declared name.

    Raises :class:`ValueError` for a **polyglot** project with no declaration. That is a refusal
    rather than a guess: picking one runtime for a repository that is genuinely both would verify
    half of it and call that a verdict.
    """
    found = _runtimes(project_dir)
    if len(found) > 1:
        raise ValueError(
            f"this project declares both {' and '.join(found)} runtimes; set \"image\" in "
            f"{DECLARATION_FILE} so the engine knows which one to verify it in"
        )
    if found:
        return found[0]
    return ""


def detect_image(project_dir: str) -> str:
    """The image a phase runs in: the declaration's, else the runtime's own.

    Empty when the project's runtime is unknown *and* nothing was declared -- the caller decides
    whether that is a refusal, and for the verification gate it is (there is nothing to check).
    """
    declared = declared_image(project_dir)
    if declared:
        return declared
    runtime = detect_runtime(project_dir)
    if runtime == NODE_RUNTIME:
        return NODE_IMAGE
    if runtime == PYTHON_RUNTIME:
        return PYTHON_IMAGE
    return ""


def _declared_timeout(project_dir: str, phase: str, fallback: int) -> int:
    """The declared timeout for ``phase``, **clamped** to the ceiling. Never unbounded."""
    data = _read_json(os.path.join(project_dir, DECLARATION_FILE))
    ceiling = MAX_SETUP_TIMEOUT_SECONDS if phase == "setup" else DEFAULT_VERIFY_TIMEOUT_SECONDS
    if not isinstance(data, dict):
        return min(fallback, ceiling)
    raw = data.get(f"{phase}_timeout_seconds")
    try:
        return max(1, min(int(raw), ceiling))
    except (TypeError, ValueError):
        return min(fallback, ceiling)


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


def detect_syntax_command(project_dir: str) -> str:
    """The structural check a project without a suite still gets (Phase 30).

    ``verified=None`` is not a free pass: a project with no tests still must not hand a human
    unparseable code. The fallback is the runtime's own parser -- ``compileall`` for Python, and
    ``node --check`` over the sources for JavaScript (or the project's own ``build`` script, which is
    a stronger check when it exists).

    The JavaScript loop skips ``node_modules``: a dependency tree's syntax is not this project's
    verdict, and walking it would make the check slow enough to time out.
    """
    runtime = detect_runtime(project_dir)
    if runtime == PYTHON_RUNTIME:
        # ``-q``: only the failures are worth printing. It compiles every module in the tree.
        return "python -m compileall -q ."
    if runtime == NODE_RUNTIME:
        package = _read_json(os.path.join(project_dir, "package.json"))
        scripts = package.get("scripts") if isinstance(package, dict) else None
        if isinstance(scripts, dict) and str(scripts.get("build") or "").strip():
            return "npm run build --silent"
        return (
            "for f in $(find . -path ./node_modules -prune -o -type f "
            "\\( -name '*.js' -o -name '*.mjs' -o -name '*.cjs' \\) -print); do "
            "node --check \"$f\" || exit 1; done"
        )
    return ""


def detect_verify_command(project_dir: str) -> str:
    """The command that decides whether ``project_dir``'s work is fit to review, or ``""``.

    A declared command wins, then a declared *suite* (``package.json`` counts only when it declares a
    ``test`` script -- ``npm test`` with no script is an error, not a verdict; a Python project
    counts when it carries ``pytest.ini``, ``pyproject.toml`` or ``tox.ini``). With no suite, the
    **syntax fallback** applies: :func:`detect_syntax_command`. Only a project whose runtime cannot
    be determined at all yields ``""``, and the engine treats that as a refusal rather than a pass.
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
    return detect_syntax_command(project_dir)


def run_setup(project_dir: str, *, timeout: Optional[int] = None) -> Optional[Any]:
    """Install the project's dependencies **with egress**, before the model is involved.

    Returns the isolated result, or ``None`` when the project declares nothing to install. Raises
    :class:`~tools.docker_sandbox.SandboxError` when the perimeter cannot run it -- a setup phase
    that could not happen is not something to paper over, because the loop that follows will fail
    for a missing dependency and look like the model's fault.

    The timeout is **hard and short by default** (Phase 30): this is the one phase with egress, so a
    poisoned manifest or a circular dependency must not be able to hold the intent queue's only
    worker. A timeout kills the container, which takes its network namespace with it.
    """
    command = detect_setup_command(project_dir)
    if not command:
        return None
    return docker_sandbox.run_isolated(
        command,
        cwd=project_dir,
        timeout=int(timeout or _declared_timeout(project_dir, "setup", DEFAULT_SETUP_TIMEOUT_SECONDS)),
        network=docker_sandbox.SETUP_NETWORK,
        image=detect_image(project_dir) or None,
    )


def run_verification(project_dir: str, *, timeout: Optional[int] = None) -> Optional[Any]:
    """Run the project's suite -- or its syntax check -- **airgapped**, as the verdict on a run.

    Returns the isolated result, or ``None`` when *no* command could be determined: an unknown
    runtime with no declaration. The engine treats that as a refusal, not a pass -- there is no free
    merge for a project the engine cannot check (Phase 30).

    The network is the sealed default: verification executes the workspace's own code, which is the
    code the model just wrote, so it is the last place egress should be available.
    """
    command = detect_verify_command(project_dir)
    if not command:
        return None
    return docker_sandbox.run_isolated(
        command,
        cwd=project_dir,
        timeout=int(timeout or _declared_timeout(project_dir, "verify", DEFAULT_VERIFY_TIMEOUT_SECONDS)),
        network=docker_sandbox.SANDBOX_NETWORK,
        image=detect_image(project_dir) or None,
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
    "DEFAULT_SETUP_TIMEOUT_SECONDS",
    "MAX_SETUP_TIMEOUT_SECONDS",
    "NODE_IMAGE",
    "PYTHON_IMAGE",
    "SETUP_COMMANDS",
    "VERIFY_COMMANDS",
    "declared_image",
    "describe",
    "detect_image",
    "detect_runtime",
    "detect_setup_command",
    "detect_syntax_command",
    "detect_verify_command",
    "run_setup",
    "run_verification",
]
