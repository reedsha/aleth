"""Absolute sandbox isolation: every command runs in a Docker container.

This module is the execution perimeter. It replaces the in-process containment that
``tools/sandbox.py`` used to provide (a Windows job object / POSIX rlimits), which confined a
process tree but could not deny the network and could not deny the filesystem outside the
workspace. A container can do both, so the payload is no longer executed on the host at all.

WHAT THIS ENFORCES
------------------
Each command runs under ``docker run`` with these invariants (see :func:`build_command`):

* ``--network=none`` -- no egress and no ingress; the payload cannot reach the network;
* ``--user <uid>:<gid>`` -- the container process runs as the **host** user, so files written
  through the workspace bind mount keep the caller's ownership instead of becoming root-owned;
* the workspace is the **only** host path mounted, as an OCI bind mount
  (``--mount type=bind,source=<root>,target=/workspace``), and it is the container's working
  directory -- so a command's cwd is the workspace root and nothing outside it exists to reach;
* ``--memory``/``--memory-swap``, ``--cpus`` and ``--pids-limit`` cap memory, CPU and the
  process count, so a fork bomb or an OOM cannot take the host down. The budget is **bounded by
  construction**: 512 MB and one CPU by default, a larger but still fixed profile for a node that
  declared the ``heavy`` capability, and a hard ceiling (:data:`MAX_MEMORY_MB`,
  :data:`MAX_CPUS`) that no caller can exceed -- see :func:`resource_limits`;
* ``--cap-drop=ALL`` and ``--security-opt no-new-privileges`` strip Linux capabilities and
  forbid privilege escalation;
* the container is named and ``--rm``, so the **process tree is owned by the container**: it
  dies with the container. On timeout the container is force-removed by name, because killing
  the ``docker`` client alone would leave it running.

The child's environment is **not** the host's: nothing is passed with ``-e``/``--env-file``, so
the ``.env`` credentials cannot be read by generated code.

RUN WHERE THE DAEMON RUNS
-------------------------
The perimeter speaks to the daemon over the ``docker`` client and treats the workspace path as a
path *in the daemon's own filesystem*. That is only true when the orchestrator and the daemon
share a namespace -- one kernel, one filesystem, one signal space. So:

* the client must be on ``PATH``. If it is not, :func:`docker_bin` raises ``EnvironmentError``:
  a missing client is a fatal configuration error, not something to work around. This module is
  not a path-translation utility and does not proxy commands through another OS;
* the process-tree guarantee above depends on it. Proxying ``docker run`` through an
  interpreter from a different OS (``wsl.exe``, a VM shim) breaks it: killing the *proxy* leaves
  the container running in the daemon's namespace, orphaned and unreachable by the timeout path.

If the daemon lives in WSL, the orchestrator runs in WSL. Nothing here bridges the two.

Configuration (read at call time, so tests can point them at a double):

* ``ALETH_SANDBOX_IMAGE`` -- the image the payload runs in (default
  ``aleth-sandbox:latest``).
* ``ALETH_DOCKER_BIN`` -- the runtime command line, when the client is not ``docker``
  (e.g. ``podman``, or an absolute path). Quote a path that contains spaces.
* ``ALETH_SANDBOX_UID`` / ``ALETH_SANDBOX_GID`` -- the identity to run as, used only
  on a platform with no POSIX uid/gid of its own (Windows), where it defaults to ``1000``.
"""

from __future__ import annotations

import atexit
import contextlib
import dataclasses
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tools import env_sanitizer, process_control, stream_drain

IMAGE_ENV = "ALETH_SANDBOX_IMAGE"
DOCKER_BIN_ENV = "ALETH_DOCKER_BIN"
UID_ENV = "ALETH_SANDBOX_UID"
GID_ENV = "ALETH_SANDBOX_GID"

DEFAULT_IMAGE = "aleth-sandbox:latest"
# The image contract lives beside the repo it serves. It describes the sandbox image only.
DOCKERFILE = Path(__file__).resolve().parent.parent / "docker" / "sandbox.Dockerfile"

# How long a base-image pull may take. Bounded for the same reason every other wait here is: a
# registry that stops answering must not hold the engine. A pull is a *host* operation -- it is how
# a runtime image the engine does not build becomes available -- and it happens once, then caches.
PULL_TIMEOUT_SECONDS = 600

DEFAULT_MEMORY_MB = 512
DEFAULT_CPUS = 1.0
DEFAULT_MAX_PROCESSES = 64
# The hard ceiling. No profile, and no caller, may exceed it: ``_clamp_limits`` is applied inside
# ``build_command`` itself, so even a direct caller cannot ask for an unbounded container.
MAX_MEMORY_MB = 8192
MAX_CPUS = 4.0

# The profile ladder, every rung bounded. A node's ``required_capabilities`` may name ``heavy``
# (see ``orchestration.mcp_session``), and that is the *only* way a command gets a larger budget:
# it travels to the exec server's own command line like the network capability, never through a
# tool argument, so a model cannot widen its own hardware. ``tiny`` is not a capability -- it is a
# budget the tests and cheap probes use.
RESOURCE_PROFILES: Dict[str, Tuple[int, float]] = {
    "tiny": (64, 0.5),
    "default": (DEFAULT_MEMORY_MB, DEFAULT_CPUS),
    "heavy": (4096, 2.0),
}

# Docker reports 128 + SIGKILL (9) for a container the kernel's OOM killer ended. It is also what
# ``docker rm -f`` produces, so it is only read as an OOM when the run did not time out.
OOM_EXIT_CODE = 137

# Windows has no POSIX identity of its own. The engine maps the host user to a uid inside the
# Linux VM, and 1000 is the conventional unprivileged default; the value is overridable for a
# host whose mapping differs. The point that matters -- the container is not root -- holds.
DEFAULT_UID = "1000"
DEFAULT_GID = "1000"

WORKSPACE_MOUNT = "/workspace"

# Every container this app creates carries these, so a leftover can be *found* rather than
# guessed at. ``owner_pid`` is what makes an orphan identifiable: the container is orphaned when
# the process that created it no longer exists, and nothing else records that fact.
LABEL_MANAGED = "aleth.managed"
LABEL_EXECUTION = "aleth.execution_id"
LABEL_OWNER = "aleth.owner_pid"
# The owner's *start time* (``/proc/<pid>/stat`` field 22). A pid alone is not an identity: the
# kernel reuses pids, so a long-lived host can hand a dead worker's number to an unrelated
# process -- and a sweep that only asked "does this pid exist?" would then see a live stranger,
# call the owner alive, and leave the orphan running forever.
LABEL_OWNER_START = "aleth.owner_start"
MANAGED_FILTER = f"label={LABEL_MANAGED}=true"

# The container's network. ``none`` is the baseline and the default for everything the *model*
# can cause; ``bridge`` exists for exactly one caller -- the deterministic setup phase, which runs
# before the model is ever handed a shell and is therefore not an exfiltration path (Phase 29).
# The model-facing surface (``tools/mcp_exec_server``) never passes it, and a test pins that.
SANDBOX_NETWORK = "none"
SETUP_NETWORK = "bridge"
SANDBOX_NETWORKS = (SANDBOX_NETWORK, SETUP_NETWORK)

# Containers this process has started and not yet finished. The signal handler in the exec
# server removes these; ``run_isolated`` adds and removes them around each command.
_ACTIVE: Dict[str, str] = {}
_ACTIVE_LOCK = threading.Lock()
# How long to wait for a wedged container to be force-removed. Bounded, so "the command timed
# out" can never become "the orchestrator hung".
KILL_TIMEOUT_SECONDS = 15
# A cold image build (base layers plus pip) is minutes, not seconds. Bounded so a wedged build
# cannot hang a caller forever, and generous enough not to fail a legitimate one.
BUILD_TIMEOUT_SECONDS = 900
# Probing the daemon is fast when it is up and must not stall when it is not.
PROBE_TIMEOUT_SECONDS = 45

# The exit statuses ``docker run`` uses for a client-side failure. We have observed 125 on a
# daemon that is down and 127 on Windows (a missing named pipe), so the marker text -- not the
# status alone -- is what classifies a run as "the runtime is unreachable".
DOCKER_CLIENT_ERROR_CODES = frozenset({125, 126, 127})
_DAEMON_DOWN_MARKERS = (
    "error during connect",
    "cannot connect to the docker daemon",
    "is the docker daemon running",
    "docker daemon is not running",
    "connection refused",
    "the system cannot find the file specified",
)

_MISSING_RUNTIME = (
    "the docker client is not on PATH. The orchestrator must run where the Docker daemon does "
    "-- one namespace, one filesystem, one signal space -- so install the client and start the "
    "daemon in this environment, or set ALETH_DOCKER_BIN to its path. The sandbox never "
    "falls back to the host, and it never proxies commands across an OS boundary."
)

# A positive image inspect is cached for the process: the inspect is a spawn, and the image does
# not usually vanish mid-run. A negative answer is not cached, so a build that follows is seen.
_VERIFIED_IMAGES: set = set()


class SandboxError(RuntimeError):
    """Container isolation could not be established. The run is refused, never retried natively."""


@dataclasses.dataclass(frozen=True)
class Capabilities:
    """What this perimeter actually enforces, stated plainly."""

    platform: str
    containment: bool
    network_denied: bool
    uid_mapping: bool
    image: str
    image_present: bool
    notes: str

    def describe(self) -> str:
        parts = [
            f"platform={self.platform}",
            "runtime=docker",
            f"containment={'yes' if self.containment else 'no'}",
            f"network_denied={'yes' if self.network_denied else 'NO'}",
            f"uid_mapping={'yes' if self.uid_mapping else 'NO'}",
            f"image={self.image}",
            f"image_present={'yes' if self.image_present else 'no'}",
        ]
        return "; ".join(parts) + f" ({self.notes})"


@dataclasses.dataclass
class IsolatedResult:
    """The outcome of an isolated run, in the shape the exec server formats.

    Carries the forensics as well as the output: the execution's identity, how long it took, and a
    SHA-256 of each **full** stream. The hashes are what make a ledger row verifiable -- the text
    below them is truncated to 1 MB by design (``tools.stream_drain``), so the hash is the only
    thing that speaks for the whole output.
    """

    returncode: int
    stdout: str
    stderr: str
    sandboxed: bool
    timed_out: bool = False
    # The kernel's OOM killer ended the container's cgroup. A separate fact from ``returncode``
    # because 137 alone is ambiguous (``docker rm -f`` produces it too), and the ledger has to be
    # able to say *why* a command died.
    oom_killed: bool = False
    notes: str = ""
    execution_id: str = ""
    duration_ms: int = 0
    stdout_sha256: str = ""
    stderr_sha256: str = ""
    stdout_bytes: int = 0
    stderr_bytes: int = 0


def image() -> str:
    """The image the payload runs in."""
    return (os.environ.get(IMAGE_ENV) or "").strip() or DEFAULT_IMAGE


def _split_command(value: str) -> List[str]:
    """A command line from an env var, split without mangling Windows paths.

    ``shlex`` in POSIX mode treats a backslash as an escape, which would turn
    ``C:\\Python\\python.exe`` into ``C:Pythonpython.exe``. On Windows it is therefore used in
    non-POSIX mode (where a backslash is literal) and the quotes it retains are stripped
    afterwards, so a quoted path that contains spaces still survives as one token.
    """
    if os.name != "nt":
        return shlex.split(value)
    tokens = shlex.split(value, posix=False)
    stripped: List[str] = []
    for token in tokens:
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1]
        stripped.append(token)
    return stripped


def docker_bin() -> List[str]:
    """The runtime command as an argv list: ``docker``, or ``ALETH_DOCKER_BIN``.

    Raises :class:`EnvironmentError` when the client cannot be found. That is deliberate and
    fatal: the perimeter reaches the daemon through this client, in this namespace, and a
    missing client means the orchestrator is running somewhere it cannot contain anything.
    """
    override = (os.environ.get(DOCKER_BIN_ENV) or "").strip()
    if override:
        return _split_command(override)
    if shutil.which("docker"):
        return ["docker"]
    raise EnvironmentError(_MISSING_RUNTIME)


def reset_caches() -> None:
    """Forget what has been cached about the runtime. For tests, and a machine whose image changed."""
    _VERIFIED_IMAGES.clear()


def host_identity() -> Tuple[str, str]:
    """The ``(uid, gid)`` the payload must run as -- the host's, so ownership is not mangled."""
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if callable(getuid) and callable(getgid):
        return str(getuid()), str(getgid())
    return (
        (os.environ.get(UID_ENV) or "").strip() or DEFAULT_UID,
        (os.environ.get(GID_ENV) or "").strip() or DEFAULT_GID,
    )


def available() -> bool:
    """Whether the Docker daemon can be reached. Never raises.

    A daemon that is down does **not** reliably report a non-zero status: on Windows the client
    was observed to exit 0 with the diagnostic on stderr and an empty stdout, so success requires
    a status of zero *and* an actual answer on stdout.
    """
    try:
        argv = [*docker_bin(), "info", "--format", "{{.ServerVersion}}"]
    except EnvironmentError:
        return False
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and bool((completed.stdout or "").strip())


def image_present(image_name: str) -> bool:
    """Whether the engine already has ``image_name``. Never raises."""
    if image_name in _VERIFIED_IMAGES:
        return True
    try:
        argv = [*docker_bin(), "image", "inspect", image_name]
    except EnvironmentError:
        return False
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if completed.returncode == 0:
        _VERIFIED_IMAGES.add(image_name)
        return True
    return False


def build_image(image_name: Optional[str] = None) -> bool:
    """Build the sandbox image from :data:`DOCKERFILE`. Returns whether it succeeded."""
    target = image_name or image()
    if not DOCKERFILE.is_file():
        return False
    try:
        argv = [
            *docker_bin(), "build", "-f", str(DOCKERFILE), "-t", target, str(DOCKERFILE.parent)
        ]
    except EnvironmentError:
        return False
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=BUILD_TIMEOUT_SECONDS,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if completed.returncode != 0:
        return False
    _VERIFIED_IMAGES.add(target)
    return True


def pull_image(image_name: str) -> bool:
    """Pull a base image from its registry. Returns whether it succeeded. Never raises.

    The counterpart of :func:`build_image` for a runtime the engine does not build: a JavaScript
    project's sandbox is ``node:20-alpine``, and that is pulled rather than built -- building *our*
    Dockerfile under someone else's tag would be a lie, and requiring a manual pull before every new
    runtime is the friction that makes a monolith look attractive.

    It is a host-level operation, not sandbox egress: the engine fetches a base image the same way a
    person would. Bounded, and reported, so it is never a silent network access.
    """
    try:
        argv = [*docker_bin(), "pull", image_name]
    except EnvironmentError:
        return False
    print(f"[sandbox] pulling {image_name} (one time, then cached)", file=sys.stderr)
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=PULL_TIMEOUT_SECONDS,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if completed.returncode != 0:
        return False
    _VERIFIED_IMAGES.add(image_name)
    return True


def _build_once(image_name: str) -> bool:
    """Build under a cross-process lock, re-checking after it is held.

    The lock is what keeps a pool of concurrent workers from starting the same cold build; the
    re-check is what makes every caller after the first a no-op rather than a second build.
    """
    lock_path = os.path.join(tempfile.gettempdir(), "aleth-sandbox-image.lock")
    try:
        import filelock

        lock: object = filelock.FileLock(lock_path, timeout=BUILD_TIMEOUT_SECONDS)
    except Exception:  # filelock is a declared dependency; a missing one must not block a build
        lock = contextlib.nullcontext()
    with lock:
        if image_present(image_name):
            return True
        print(
            f"[sandbox] building {image_name} from {DOCKERFILE} (one time, then cached)",
            file=sys.stderr,
        )
        return build_image(image_name)


def ensure_image(image_name: Optional[str] = None) -> str:
    """The image a payload runs in, building the sandbox image once when it is absent.

    A **custom** image that is absent is an error rather than a build: ``docker/sandbox.Dockerfile``
    describes the sandbox image and nothing else, so building it under someone else's tag would be
    a lie. The sandbox image itself is built from that file. If it cannot be built, this raises --
    the perimeter refuses the command instead of running it on the host.

    The build happens inside the caller's path, so a first-ever command pays for it. Build the
    image ahead of time (see the Dockerfile header) and this is a single cached inspect.
    """
    target = image_name or image()
    try:
        docker_bin()
    except EnvironmentError as error:
        raise SandboxError(str(error)) from error
    if image_present(target):
        return target
    if target != DEFAULT_IMAGE:
        # A runtime image the engine does not build: pull it, once, rather than refusing and making
        # every new language a manual chore (Phase 30).
        if pull_image(target):
            return target
        raise SandboxError(
            f"the sandbox image {target!r} is not present and could not be pulled; "
            f"run `docker pull {target}` or set {IMAGE_ENV} to an image you have"
        )
    if not DOCKERFILE.is_file():
        raise SandboxError(
            f"the sandbox image {target!r} is missing and {DOCKERFILE} does not exist; build it with "
            f"`docker build -f docker/sandbox.Dockerfile -t {DEFAULT_IMAGE} docker/`"
        )
    if not _build_once(target):
        raise SandboxError(
            f"the sandbox image {target!r} could not be built; run "
            f"`docker build -f docker/sandbox.Dockerfile -t {DEFAULT_IMAGE} docker/` and retry"
        )
    return target


def capabilities() -> Capabilities:
    """A static, honest statement of what this perimeter enforces."""
    reachable = available()
    present = image_present(image()) if reachable else False
    if not reachable:
        notes = "the Docker daemon is NOT reachable: every run is refused, never run natively"
    elif present:
        notes = f"container via {docker_bin()[0]!r}; network disabled (--network=none)"
    else:
        notes = f"{image()!r} is absent; it is built from {DOCKERFILE.name} on first use"
    return Capabilities(
        platform=sys.platform,
        containment=reachable,
        network_denied=True,
        uid_mapping=True,
        image=image(),
        image_present=present,
        notes=notes,
    )


def _clamp_limits(memory_mb: int, cpus: float) -> Tuple[int, float]:
    """Clamp a budget to the ceiling. The last line of defence, inside the argv builder."""
    return min(int(memory_mb), MAX_MEMORY_MB), min(float(cpus), MAX_CPUS)


def resource_limits(profile: str = "default") -> Tuple[int, float]:
    """The ``(memory_mb, cpus)`` a named profile grants, clamped to the ceiling.

    An unknown name resolves to the **default** profile rather than raising: the name has already
    been through the capability vocabulary (``orchestration.mcp_session``), which refuses anything
    it does not implement, so by here it is a known profile or the caller is asking for the
    baseline. What matters is that the answer is always bounded -- there is no name, and no
    argument, that yields an unbounded container.
    """
    key = str(profile or "").strip().lower()
    memory_mb, cpus = RESOURCE_PROFILES.get(key, RESOURCE_PROFILES["default"])
    return _clamp_limits(memory_mb, cpus)


def build_command(
    command: str,
    *,
    root: str,
    name: str,
    memory_mb: int = DEFAULT_MEMORY_MB,
    cpus: float = DEFAULT_CPUS,
    max_processes: int = DEFAULT_MAX_PROCESSES,
    image_name: Optional[str] = None,
    execution_id: Optional[str] = None,
    network: str = SANDBOX_NETWORK,
) -> List[str]:
    """The ``docker run`` argv for one command.

    Pure, so the whole isolation contract can be asserted without a daemon. ``root`` is the
    workspace: it is the only host path mounted, and the container's working directory. It is
    used as written -- the path is a path *in the daemon's filesystem*, because the orchestrator
    runs there too.

    ``network`` defaults to :data:`SANDBOX_NETWORK` (``none``) and is **validated**: the only other
    value is :data:`SETUP_NETWORK`, which exists for the deterministic setup phase -- a command the
    orchestrator runs from the project's own manifest before the model is involved, so it is not a
    path an injected tool call can take. Everything the model can cause goes through
    ``tools/mcp_exec_server``, which never passes it. The plan-declared ``net`` capability that used
    to widen this was removed in Phase 28: an LLM that can reach the network can exfiltrate
    ``.env``, and "the plan declared it" is not a mitigation.

    The resource budget is bounded by the same rule, enforced here in the builder: ``memory_mb``
    and ``cpus`` are clamped to :data:`MAX_MEMORY_MB`/:data:`MAX_CPUS`, so nothing a model can say
    -- and no caller -- can ask for an unbounded container.

    The three labels make the container accountable: that it is ours, which execution it belongs
    to, and which process created it. That last one is the whole point -- see
    :func:`purge_orphaned_containers`.
    """
    if network not in SANDBOX_NETWORKS:
        raise ValueError(f"unknown network {network!r}; expected one of {SANDBOX_NETWORKS}")
    uid, gid = host_identity()
    resolved = str(Path(root).resolve())
    memory_mb, cpus = _clamp_limits(memory_mb, cpus)
    return [
        *docker_bin(), "run", "--rm",
        "--name", name,
        "--label", f"{LABEL_MANAGED}=true",
        "--label", f"{LABEL_EXECUTION}={execution_id or name}",
        "--label", f"{LABEL_OWNER}={os.getpid()}",
        "--label", f"{LABEL_OWNER_START}={_process_start_time(os.getpid()) or ''}",
        f"--network={network}",
        "--user", f"{uid}:{gid}",
        "--workdir", WORKSPACE_MOUNT,
        "--mount", f"type=bind,source={resolved},target={WORKSPACE_MOUNT}",
        "--memory", f"{int(memory_mb)}m",
        "--memory-swap", f"{int(memory_mb)}m",
        "--cpus", str(cpus),
        "--pids-limit", str(int(max_processes)),
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
        image_name or image(),
        "/bin/sh", "-c", command,
    ]


def _is_unreachable_runtime(stderr: str, stdout: str) -> bool:
    """Whether a failed run is the *runtime* failing, rather than the payload.

    Both halves matter: a payload that cannot reach a service would also print a connectivity
    line, so a daemon error is only believed when the client failed a client-side code and the
    payload produced no output of its own.
    """
    if (stdout or "").strip():
        return False
    text = (stderr or "").lower()
    return any(marker in text for marker in _DAEMON_DOWN_MARKERS)


def _diagnostic(stderr: str) -> str:
    """The line of the client's output that says *why* it failed, not a trailing hint.

    ``docker run`` puts the connectivity error first and a ``See 'docker run --help'.`` hint
    last, so the last line is the least useful one to report.
    """
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    for line in lines:
        lowered = line.lower()
        if any(marker in lowered for marker in _DAEMON_DOWN_MARKERS):
            return line
    return lines[0] if lines else "no diagnostic"


@dataclasses.dataclass(frozen=True)
class ManagedContainer:
    """A container this app made, and the identity of the process that made it."""

    id: str
    execution_id: str
    owner_pid: str
    owner_start: str


def _register_active(name: str, execution_id: str) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE[name] = execution_id


def _unregister_active(name: str) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.pop(name, None)


def active_containers() -> List[str]:
    """The names of the containers this process still has in flight."""
    with _ACTIVE_LOCK:
        return list(_ACTIVE)


def remove_container(container: str, *, detached: bool = False) -> bool:
    """Force-remove one container. Idempotent, and never raises.

    ``rm -f`` on an already-gone container succeeds, which is what makes every caller here safe
    to retry: the signal handler, the timeout path and the sweeper can all race each other.

    ``detached`` is for the one caller that is itself being killed -- the exec server's signal
    handler. The removal runs in **its own session**, so the client's grace-expiry SIGKILL, aimed
    at the server's process group, cannot reach it: the container dies even when the server does
    not live long enough to see it happen. Without this the two budgets disagree -- the handler's
    removal is bounded by :data:`KILL_TIMEOUT_SECONDS` while the grace is
    ``process_control.DEFAULT_GRACE_SECONDS`` -- and a slow removal under load was killed
    mid-flight, leaving the container orphaned by the very signal that was meant to reap it.

    Output goes to ``DEVNULL`` rather than a pipe in that mode: a detached child must not hold a
    pipe whose reader is about to die.
    """
    try:
        argv = [*docker_bin(), "rm", "-f", container]
    except EnvironmentError:
        return False
    if detached:
        options: Dict[str, Any] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        spawn: Dict[str, Any] = {"start_new_session": True}
    else:
        options = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"}
        spawn = {}
    try:
        completed = subprocess.run(argv, timeout=KILL_TIMEOUT_SECONDS, **options, **spawn)
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def purge_active_containers(*, detached: bool = False) -> List[str]:
    """Force-remove every container this process still has in flight.

    What the exec server's signal handler calls. Deliberately not a docker *query*: this runs on
    a signal, and the names are already known here -- a query would add a round trip to a path
    that has a grace period measured in seconds.

    ``detached`` is passed through to :func:`remove_container`, and the handler asks for it: the
    process this is cleaning up after is about to be SIGKILLed, and a removal that kill can
    interrupt is a removal that did not happen.
    """
    names = active_containers()
    removed = [name for name in names if remove_container(name, detached=detached)]
    for name in names:
        _unregister_active(name)
    return removed


def _docker_lines(args: List[str], *, timeout: int = PROBE_TIMEOUT_SECONDS) -> List[str]:
    """Run a docker query and return its non-empty stdout lines. Never raises."""
    try:
        argv = [*docker_bin(), *args]
    except EnvironmentError:
        return []
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    return [line for line in (completed.stdout or "").splitlines() if line.strip()]


def managed_containers() -> List["ManagedContainer"]:
    """Every container this app manages, with the identity of the process that made it."""
    rows: List[ManagedContainer] = []
    lines = _docker_lines([
        "ps", "-a", "--filter", MANAGED_FILTER,
        "--format",
        '{{.ID}}\t{{.Label "' + LABEL_EXECUTION + '"}}\t{{.Label "' + LABEL_OWNER
        + '"}}\t{{.Label "' + LABEL_OWNER_START + '"}}',
    ])
    for line in lines:
        parts = [part.strip() for part in line.split("\t")]
        if len(parts) == 4:
            rows.append(ManagedContainer(*parts))
    return rows


def _stat_fields(pid: int) -> List[str]:
    """``/proc/<pid>/stat``'s fields after the parenthesised comm, or ``[]``.

    Split on the **last** ``)``: the comm may itself contain spaces and parentheses, so a plain
    split would misnumber every field after it. After the comm, ``state`` is field 3 -- so the
    returned list is indexed from field 3.
    """
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as handle:
            raw = handle.read()
    except OSError:
        return []
    _head, separator, rest = raw.rpartition(")")
    if not separator:
        return []
    return rest.split()


def _process_start_time(pid: int) -> Optional[str]:
    """The kernel's start time for ``pid``, or ``None`` when it does not exist.

    Field 22 -- stable for the life of a process, which is exactly what makes it an identity
    rather than a number.
    """
    fields = _stat_fields(pid)
    # After the comm, ``state`` is field 3, so ``starttime`` (field 22) is index 19.
    return fields[19] if len(fields) >= 20 else None


def _parent_pid(pid: int) -> str:
    """The parent of ``pid``, or ``""`` when it is gone or unreadable."""
    fields = _stat_fields(pid)
    # After the comm, ``state`` is field 3, so ``ppid`` (field 4) is index 1.
    return fields[1] if len(fields) >= 2 else ""


def _is_our_child(pid: str) -> bool:
    """Whether ``pid`` is this process or one of its descendants, right now.

    The engine never creates a container itself: ``run_isolated`` runs inside the exec server child
    (``tools/mcp_exec_server.py``), so every container the engine is responsible for carries *that*
    child's pid. A shutdown sweep scoped to ``os.getpid()`` alone would therefore remove nothing,
    and one scoped to the label alone is what murders a neighbour. Walking the parent chain is what
    tells the two apart.

    Bounded and fails safe: the walk stops at pid 1 or on a cycle, and a pid that is gone -- or a
    ``/proc`` that cannot be read -- answers ``False``. A container whose owner is gone is
    deliberately left alone here; the boot sweep is the one that can tell, and it is the only one
    that should.
    """
    if os.name != "posix":
        return False
    mine = os.getpid()
    current = str(pid)
    seen = set()
    while current.isdigit() and current not in seen:
        if int(current) == mine:
            return True
        seen.add(current)
        current = _parent_pid(int(current))
    return False


def _owner_is_gone(owner_pid: str, owner_start: str = "") -> bool:
    """Whether the process that created a container is no longer the process it was.

    Two ways to be gone, and the second is the one a naive check misses:

    * the pid does not exist at all -- the ordinary case;
    * the pid exists but belongs to a **different** process, because the kernel recycled the
      number after the owner died. Comparing the recorded start time against ``/proc`` catches
      that; asking only whether the pid is alive would see the stranger and call the owner well.

    POSIX only. Elsewhere this answers ``False``: a sweep that cannot tell must not remove a
    container that may still be in use. A container with no recorded start time is likewise left
    alone unless its pid is missing outright -- the check fails *towards* keeping the container.
    """
    if os.name != "posix" or not owner_pid.isdigit():
        return False
    current = _process_start_time(int(owner_pid))
    if current is None:
        return True
    if owner_start and current != owner_start:
        return True
    return False


def purge_orphaned_containers(session_id: Optional[str] = None) -> List[str]:
    """Force-remove managed containers left behind by a process that is gone.

    Two modes, and the difference is who is asking:

    * ``session_id`` given -- an owning process tidying up after itself. It matches the
      ``owner_pid`` label, which *is* the session: the exec server's containers are the ones its
      own pid created.
    * ``session_id=None`` -- the boot/teardown sweep. A container is orphaned exactly when the
      process that created it no longer exists, because that process is the only one that would
      ever have removed it.
    """
    removed: List[str] = []
    for container in managed_containers():
        if session_id is not None:
            if container.owner_pid != str(session_id):
                continue
        elif not _owner_is_gone(container.owner_pid, container.owner_start):
            continue
        if remove_container(container.id):
            removed.append(container.id)
    return removed


def sweep_orphaned_containers(stage: str) -> int:
    """Sweep, and **say what it found**. Returns how many containers were destroyed.

    The logging is the point as much as the removal: a silent collector hides a systemic crash.
    A host whose engine is OOM-killed on every run leaks a container per run, and the only
    signal is this line at the next boot. Zero is reported too -- that it ran is information.

    Never raises. A boot that cannot reach the daemon is still a boot, and the perimeter refuses
    commands on its own terms.
    """
    try:
        removed = purge_orphaned_containers()
    except Exception as error:
        print(
            f"[Sandbox] {stage} sweep skipped: {type(error).__name__}: {error}",
            flush=True,
        )
        return 0
    if removed:
        print(
            f"[Sandbox] {stage} sweep removed {len(removed)} orphaned container(s): "
            + ", ".join(removed),
            # Flushed on purpose: stdout is block-buffered when it is a pipe, and a boot report
            # that is lost because the process died before the buffer filled is not a report.
            flush=True,
        )
    else:
        print(f"[Sandbox] {stage} sweep: no orphaned containers", flush=True)
    return len(removed)


def purge_managed_containers() -> List[str]:
    """Force-remove every container **this process is responsible for**. Returns what it removed.

    Responsible for, not merely labelled: a host can run several engines at once -- a CI matrix, a
    background processor, two projects on one box -- and a sweep that matched the ``aleth.managed``
    label alone would murder a neighbour's in-flight containers the moment this one exited. So each
    candidate's owner is checked against **this process and its descendants**
    (:func:`_is_our_child`), which is what "the engine cleans its own garbage" actually means.

    Descendants, not ``os.getpid()`` alone, and the distinction is not a nicety: the engine never
    creates a container itself. ``run_isolated`` runs inside the exec server child
    (``tools/mcp_exec_server.py``), so the containers this engine is responsible for carry that
    child's pid. A sweep that only matched its own pid would remove nothing at all.

    It asks the daemon rather than trusting this process's bookkeeping, because the bookkeeping may
    be exactly what is broken -- and because a container is a child of *dockerd*, not of this
    process, so process-group scoping never reached it.

    A container whose owner is already gone is left alone: this engine cannot prove it made it, and
    the boot sweep (:func:`purge_orphaned_containers`) is the one that can tell.
    """
    removed: List[str] = []
    for container in managed_containers():
        if not _is_our_child(container.owner_pid):
            continue
        # Detached, like the exec server's signal path: this process may be killed a moment later,
        # and a removal the kill can interrupt is a removal that did not happen.
        if remove_container(container.id, detached=True):
            removed.append(container.id)
    return removed


def install_shutdown_sweep(stage: str = "shutdown") -> None:
    """Run the engine's container sweep on every way out: ``atexit``, SIGINT and SIGTERM.

    The exec server has its own reaper for the containers *it* holds (see
    ``mcp_exec_server.install_container_reaper``); this is the engine's, and it covers what that
    one cannot: a container whose exec server died first, and a shutdown where no server was
    running at all.

    Registered on all three exits on purpose. ``atexit`` covers a normal return and a ``sys.exit``
    -- including the bootloader's. SIGINT and SIGTERM cover a kill from a terminal, a supervisor or
    a service manager. The handler sweeps, restores the default disposition and re-delivers the
    signal, so the process still dies with the status the sender expects rather than lingering as
    a Python process that swallowed a terminate.

    A SIGKILL is precisely the case the *boot* sweep exists for: nothing can run, so the next boot
    is the only thing that will ever collect that container.
    """

    def _sweep(*_args) -> None:
        removed = purge_managed_containers()
        if removed:
            print(
                f"[Sandbox] {stage} sweep removed {len(removed)} managed container(s): "
                + ", ".join(removed),
                # Flushed on purpose: a shutdown report lost to an unwritten buffer is not a report.
                flush=True,
            )

    atexit.register(_sweep)

    def _handler(signum, _frame):
        _sweep()
        try:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        except Exception:  # a platform that cannot re-deliver still has to leave
            raise SystemExit(128 + signum)

    for name in ("SIGINT", "SIGTERM"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            signal.signal(number, _handler)
        except (ValueError, OSError):
            # Not the main thread: ``atexit`` still covers the graceful paths.
            pass


def _force_remove(name: str) -> None:
    """Destroy a container by name. Best effort: cleanup must never mask the timeout."""
    try:
        argv = [*docker_bin(), "rm", "-f", name]
    except EnvironmentError:
        return
    try:
        subprocess.run(
            argv, capture_output=True, text=True, timeout=KILL_TIMEOUT_SECONDS,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        pass


def run_isolated(
    command: str,
    *,
    cwd: str,
    timeout: int = 30,
    memory_mb: int = DEFAULT_MEMORY_MB,
    cpus: float = DEFAULT_CPUS,
    max_processes: int = DEFAULT_MAX_PROCESSES,
    network: str = SANDBOX_NETWORK,
    image: Optional[str] = None,
) -> IsolatedResult:
    """Run ``command`` in a container rooted at ``cwd``, or raise :class:`SandboxError`.

    ``cwd`` is the workspace root: it becomes the container's bind mount and working directory.
    The command is never executed on the host -- when the runtime or the image cannot be reached
    this raises rather than falling back.

    The network is sealed by default (see :func:`build_command`). ``network`` exists for the
    deterministic setup phase and nothing else; the model-facing surface never sets it.

    ``image`` is the **runtime** the command runs in (Phase 30): resolved per project from its own
    manifests by ``tools.project_phases.detect_runtime``. It defaults to the engine's own sandbox
    image, which is the Python toolchain; a JavaScript project gets ``node:20-alpine`` instead, so no
    single image has to carry every language.
    """
    root = Path(cwd).resolve()
    if not root.is_dir():
        raise SandboxError(f"the workspace root {str(root)!r} is not a directory")

    try:
        target = ensure_image(image)
        execution_id = uuid.uuid4().hex
        name = f"aleth-exec-{execution_id[:12]}"
        argv = build_command(
            command, root=str(root), name=name, memory_mb=memory_mb, cpus=cpus,
            max_processes=max_processes, image_name=target, execution_id=execution_id,
            network=network,
        )
    except EnvironmentError as error:
        # The client is missing: a fatal configuration error, reported at the tool boundary as
        # the refusal it is.
        raise SandboxError(str(error)) from error

    try:
        process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env_sanitizer.sanitized_environment(),
            # Its own process group, so the timeout path can end the whole tree.
            **process_control.spawn_kwargs(),
        )
    except OSError as error:
        raise SandboxError(
            f"the container runtime {docker_bin()[0]!r} could not be started: {error}"
        ) from error

    # Registered from the moment the container exists: the signal handler can only remove what it
    # knows about, and the window between ``run`` starting and returning is exactly the window in
    # which a killed server orphans a container.
    _register_active(name, execution_id)

    started = time.monotonic()

    # Drain both streams **while the client runs**, into a bounded window. Reading after the
    # process exits would deadlock on a full pipe, and ``communicate()`` would retain every byte
    # a chatty command wrote (see ``tools.stream_drain``).
    out_buffer, err_buffer, out_thread, err_thread = stream_drain.drain_pair(
        process.stdout, process.stderr
    )

    def _receipts() -> IsolatedResult:
        return IsolatedResult(
            process.returncode if process.returncode is not None else -1,
            stream_drain.decode(out_buffer.render()),
            stream_drain.decode(err_buffer.render()),
            sandboxed=True,
            execution_id=execution_id,
            duration_ms=int((time.monotonic() - started) * 1000),
            stdout_sha256=out_buffer.sha256,
            stderr_sha256=err_buffer.sha256,
            stdout_bytes=out_buffer.total_bytes,
            stderr_bytes=err_buffer.total_bytes,
        )

    try:
        process.wait(timeout=int(timeout))
    except subprocess.TimeoutExpired:
        # Two things must die, and they are different things: the client's process group, and the
        # container the client launched. Killing the client alone leaves the container running.
        process_control.kill_group(process)
        _force_remove(name)
        try:
            process.wait(timeout=process_control.DEFAULT_GRACE_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - a client that ignores SIGKILL
            pass
        out_thread.join(timeout=5)
        err_thread.join(timeout=5)
        process_control.close_pipes(process)
        result = _receipts()
        result.returncode = -1
        result.timed_out = True
        result.notes = "the container was force-removed after the timeout"
        _unregister_active(name)
        return result

    out_thread.join(timeout=5)
    err_thread.join(timeout=5)
    process_control.close_pipes(process)

    result = _receipts()
    _unregister_active(name)
    if result.returncode == OOM_EXIT_CODE:
        # The kernel's OOM killer ended the container's cgroup: the payload asked for more memory
        # than its budget allowed. Marked here rather than left to the caller, because the exit
        # status alone is ambiguous -- ``docker rm -f`` also yields 137 -- and the ledger must say
        # *why* a command died, not merely that it did.
        result.oom_killed = True
        result.notes = (
            f"exit {OOM_EXIT_CODE} (SIGKILL): the container was killed, which for a container "
            f"capped at {int(memory_mb)}MB is the kernel's OOM killer"
        )
    if result.returncode in DOCKER_CLIENT_ERROR_CODES and _is_unreachable_runtime(
        result.stderr, result.stdout
    ):
        raise SandboxError(
            f"the Docker daemon could not be reached ({_diagnostic(result.stderr)})"
        )
    return result
