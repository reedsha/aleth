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
  process count, so a fork bomb or an OOM cannot take the host down;
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

import contextlib
import dataclasses
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

from tools import env_sanitizer, process_control, stream_drain

IMAGE_ENV = "ALETH_SANDBOX_IMAGE"
DOCKER_BIN_ENV = "ALETH_DOCKER_BIN"
UID_ENV = "ALETH_SANDBOX_UID"
GID_ENV = "ALETH_SANDBOX_GID"

DEFAULT_IMAGE = "aleth-sandbox:latest"
# The image contract lives beside the repo it serves. It describes the sandbox image only.
DOCKERFILE = Path(__file__).resolve().parent.parent / "docker" / "sandbox.Dockerfile"

DEFAULT_MEMORY_MB = 2048
DEFAULT_MAX_PROCESSES = 64
DEFAULT_CPUS = 1.0
# Windows has no POSIX identity of its own. The engine maps the host user to a uid inside the
# Linux VM, and 1000 is the conventional unprivileged default; the value is overridable for a
# host whose mapping differs. The point that matters -- the container is not root -- holds.
DEFAULT_UID = "1000"
DEFAULT_GID = "1000"

WORKSPACE_MOUNT = "/workspace"
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
    """The outcome of an isolated run, in the shape the exec server formats."""

    returncode: int
    stdout: str
    stderr: str
    sandboxed: bool
    timed_out: bool = False
    notes: str = ""


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
        raise SandboxError(
            f"the sandbox image {target!r} is not present, and {DOCKERFILE.name} does not build it; "
            f"build {target!r} or unset {IMAGE_ENV}"
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


def build_command(
    command: str,
    *,
    root: str,
    name: str,
    memory_mb: int = DEFAULT_MEMORY_MB,
    max_processes: int = DEFAULT_MAX_PROCESSES,
    image_name: Optional[str] = None,
) -> List[str]:
    """The ``docker run`` argv for one command.

    Pure, so the whole isolation contract can be asserted without a daemon. ``root`` is the
    workspace: it is the only host path mounted, and the container's working directory. It is
    used as written -- the path is a path *in the daemon's filesystem*, because the orchestrator
    runs there too.
    """
    uid, gid = host_identity()
    resolved = str(Path(root).resolve())
    return [
        *docker_bin(), "run", "--rm",
        "--name", name,
        "--network=none",
        "--user", f"{uid}:{gid}",
        "--workdir", WORKSPACE_MOUNT,
        "--mount", f"type=bind,source={resolved},target={WORKSPACE_MOUNT}",
        "--memory", f"{int(memory_mb)}m",
        "--memory-swap", f"{int(memory_mb)}m",
        "--cpus", str(DEFAULT_CPUS),
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
    max_processes: int = DEFAULT_MAX_PROCESSES,
) -> IsolatedResult:
    """Run ``command`` in a container rooted at ``cwd``, or raise :class:`SandboxError`.

    ``cwd`` is the workspace root: it becomes the container's bind mount and working directory.
    The command is never executed on the host -- when the runtime or the image cannot be reached
    this raises rather than falling back.
    """
    root = Path(cwd).resolve()
    if not root.is_dir():
        raise SandboxError(f"the workspace root {str(root)!r} is not a directory")

    try:
        target = ensure_image()
        name = f"aleth-exec-{uuid.uuid4().hex[:12]}"
        argv = build_command(
            command, root=str(root), name=name,
            memory_mb=memory_mb, max_processes=max_processes, image_name=target,
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
        return result

    out_thread.join(timeout=5)
    err_thread.join(timeout=5)
    process_control.close_pipes(process)

    result = _receipts()
    if result.returncode in DOCKER_CLIENT_ERROR_CODES and _is_unreachable_runtime(
        result.stderr, result.stdout
    ):
        raise SandboxError(
            f"the Docker daemon could not be reached ({_diagnostic(result.stderr)})"
        )
    return result
