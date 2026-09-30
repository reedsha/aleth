r"""Absolute sandbox isolation: every command runs in a Docker container.

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
the ``.env`` credentials cannot be read by generated code. That is stronger than the old
perimeter, which had to rebuild the environment to strip secret-looking names.

THERE IS NO NATIVE FALLBACK
---------------------------
If the Docker daemon cannot be reached, or the ``docker`` client is missing, the run is
**refused** with :class:`SandboxError` -- never retried on the host. "Contained or refused" is
the whole contract; a silent unsandboxed run is the failure this module exists to prevent.

THE IMAGE CONTRACT
------------------
Payloads run in ``deepagents-sandbox:latest``, built from ``docker/sandbox.Dockerfile``: a
Python 3.12 base with the test runner and the fundamental test utilities, because the commands
the app runs are a workspace's own build and test commands. When that image is absent it is
built once, behind a cross-process lock, on first use (:func:`ensure_image`); a *custom* image
that is absent is an error rather than a build, because the repo's Dockerfile describes the
sandbox image and nothing else.

REACHING AN ENGINE THAT LIVES IN WSL
------------------------------------
A Windows host with no ``docker`` on ``PATH`` may run its engine inside WSL. The perimeter
detects that (:func:`docker_bin`) and translates the workspace path to the form the *engine*
can bind-mount, because the engine is Linux.

Two host layouts are possible, and only one of them is safe:

* **A workspace on the Linux filesystem, addressed from Windows as ``\\wsl$\<distro>\<path>``.**
  This is the supported layout: the share translates straight to a Linux path, the bind mount
  is an ordinary ext4 mount, and files written by a payload keep real ownership. The Windows
  app reads and writes the same directory through the share.
* **A workspace on a Windows drive (``C:``).** This is **refused** when the engine is bridged.
  A container writing through a ``/mnt/c`` (9p DrvFs) bind mount creates files with no usable
  Windows ACL -- they land with mode ``0000`` and Windows cannot read or even enumerate them.
  That is host permissions corruption, so the perimeter refuses rather than producing files the
  user cannot open (:class:`SandboxError`). Running the engine with Windows file sharing
  (Docker Desktop) is the other way to use a Windows-drive workspace.

Configuration (read at call time, so tests can point them at a double):

* ``DEEPAGENTS_SANDBOX_IMAGE`` -- the image the payload runs in (default
  ``deepagents-sandbox:latest``).
* ``DEEPAGENTS_DOCKER_BIN`` -- the runtime command line, when it is not ``docker``
  (e.g. ``podman``, or an absolute path). Quote a path that contains spaces.
* ``DEEPAGENTS_DOCKER_WSL_DISTRO`` -- the WSL distribution to bridge through, when more than one
  is installed and the wrong one is being picked.
* ``DEEPAGENTS_SANDBOX_UID`` / ``DEEPAGENTS_SANDBOX_GID`` -- the identity to run as, used only
  on a platform with no POSIX uid/gid of its own (Windows), where it defaults to ``1000``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

IMAGE_ENV = "DEEPAGENTS_SANDBOX_IMAGE"
DOCKER_BIN_ENV = "DEEPAGENTS_DOCKER_BIN"
WSL_DISTRO_ENV = "DEEPAGENTS_DOCKER_WSL_DISTRO"
UID_ENV = "DEEPAGENTS_SANDBOX_UID"
GID_ENV = "DEEPAGENTS_SANDBOX_GID"

DEFAULT_IMAGE = "deepagents-sandbox:latest"
# The image contract lives beside the repo it serves. It describes the sandbox image only.
DOCKERFILE = Path(__file__).resolve().parent.parent / "docker" / "sandbox.Dockerfile"

DEFAULT_MEMORY_MB = 2048
DEFAULT_MAX_PROCESSES = 64
DEFAULT_CPUS = 1.0
# Windows has no POSIX identity of its own. Docker maps the host user to a uid inside the Linux
# VM, and 1000 is the conventional unprivileged default; the value is overridable for a host
# whose mapping differs. The point that matters -- the container is not root -- holds.
DEFAULT_UID = "1000"
DEFAULT_GID = "1000"

WORKSPACE_MOUNT = "/workspace"
# How long to wait for a wedged container to be force-removed. Bounded, so "the command timed
# out" can never become "the orchestrator hung".
KILL_TIMEOUT_SECONDS = 15
# A cold image build (base layers plus pip) is minutes, not seconds. Bounded so a wedged build
# cannot hang a caller forever, and generous enough not to fail a legitimate one.
BUILD_TIMEOUT_SECONDS = 900
# Probing a WSL distribution for a live engine spawns a VM command; it is fast when the distro
# is up and must not stall when it is not.
WSL_PROBE_TIMEOUT = 45

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

# Per-process caches. The runtime probe and the image inspect are each a process spawn; a
# long-lived caller (the verdict runner in the orchestrator) must not pay them per command.
_DETECTED_BIN: Optional[List[str]] = None
_VERIFIED_IMAGES: set = set()
_WSL_PATH_CACHE: Dict[Tuple[str, str], str] = {}

_DRIVE_RE = re.compile(r"^([A-Za-z]):/(.*)$")
# ``\\wsl$\Ubuntu\home\u\x`` and its ``\\wsl.localhost\...`` spelling.
_WSL_UNC_RE = re.compile(
    r"^\\\\wsl(?:\$|\.localhost)\\(?P<distro>[^\\/]+)[\\/](?P<rest>.*)$", re.IGNORECASE
)
# A Windows drive seen from inside WSL. A container write there corrupts the host's ACLs.
_DRVFS_RE = re.compile(r"^/mnt/[A-Za-z](/|$)")


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


# --------------------------------------------------------------------------- the runtime


def _wsl_distro(argv: Sequence[str]) -> Optional[str]:
    """The distribution name when ``argv`` invokes docker *through* WSL, else ``None``.

    Recognising the bridge from the argv -- rather than from how it was chosen -- is what lets an
    explicit ``DEEPAGENTS_DOCKER_BIN="wsl -d Ubuntu docker"`` get the same path translation the
    auto-detected form does.
    """
    if not argv:
        return None
    if os.path.basename(str(argv[0])).lower() not in ("wsl", "wsl.exe"):
        return None
    for index, token in enumerate(argv[1:], start=1):
        if token in ("-d", "--distribution") and index + 1 < len(argv):
            return str(argv[index + 1])
    return None


def _wsl_distros(wsl: str) -> List[str]:
    """The installed WSL distribution names.

    ``wsl -l -q`` writes **UTF-16LE** on Windows, so the bytes are decoded rather than read as
    text; a naive read yields names with interleaved NULs and no distribution is ever found.
    """
    try:
        completed = subprocess.run([wsl, "-l", "-q"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    raw = completed.stdout or b""
    text = raw.decode("utf-16-le", errors="replace") if b"\x00" in raw else raw.decode("utf-8", "replace")
    return [line.strip() for line in text.replace("\x00", "").splitlines() if line.strip()]


def _detect_wsl_bridge() -> Optional[List[str]]:
    """A docker engine reached through a WSL distribution, when the host has no docker of its own."""
    wsl = shutil.which("wsl") or shutil.which("wsl.exe")
    if not wsl:
        return None
    explicit = (os.environ.get(WSL_DISTRO_ENV) or "").strip()
    names = [explicit] if explicit else _wsl_distros(wsl)
    for name in names:
        probe = [wsl, "-d", name, "--", "docker", "info", "--format", "{{.ServerVersion}}"]
        try:
            completed = subprocess.run(
                probe, capture_output=True, text=True, timeout=WSL_PROBE_TIMEOUT,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.SubprocessError):
            continue
        # A down daemon can exit 0 with the diagnostic on stderr, so an answer is required.
        if completed.returncode == 0 and (completed.stdout or "").strip():
            return [wsl, "-d", name, "--", "docker"]
    return None


def _detect_runtime() -> List[str]:
    if shutil.which("docker"):
        return ["docker"]
    return _detect_wsl_bridge() or ["docker"]


def docker_bin() -> List[str]:
    """The runtime command as an argv list.

    ``docker`` when it is on ``PATH``; otherwise an engine reached through WSL, because a Windows
    host that runs its engine in WSL is an ordinary setup and the perimeter has to reach it.
    ``DEEPAGENTS_DOCKER_BIN`` overrides the detection outright.
    """
    global _DETECTED_BIN
    override = (os.environ.get(DOCKER_BIN_ENV) or "").strip()
    if override:
        return _split_command(override)
    if _DETECTED_BIN is None:
        _DETECTED_BIN = _detect_runtime()
    return list(_DETECTED_BIN)


def reset_runtime() -> None:
    """Forget the detected runtime and everything cached about it. For tests, and a changed PATH."""
    global _DETECTED_BIN
    _DETECTED_BIN = None
    _VERIFIED_IMAGES.clear()
    _WSL_PATH_CACHE.clear()


# --------------------------------------------------------------------------- paths


def _conventional_wsl_path(path: str) -> str:
    """``C:/a/b`` -> ``/mnt/c/a/b``. The fallback when ``wslpath`` cannot be asked."""
    match = _DRIVE_RE.match(path)
    return f"/mnt/{match.group(1).lower()}/{match.group(2)}" if match else path


def _wsl_unc_to_linux(path: str) -> Optional[str]:
    r"""``\\wsl$\Ubuntu\home\u\x`` -> ``/home/u/x``. ``None`` when it is not a WSL share.

    This is the *safe* Windows-side spelling of a Linux-filesystem path, and it needs no
    ``wslpath`` call: the share's own syntax already carries the distribution and the path.
    """
    match = _WSL_UNC_RE.match(path)
    if match is None:
        return None
    rest = match.group("rest").replace("\\", "/").strip("/")
    return f"/{rest}" if rest else "/"


def _wsl_path(host_path: str, distro: str, wsl_exe: str) -> str:
    """``host_path`` in the form the WSL engine can bind-mount.

    ``wslpath`` is asked rather than string-munged, because it is the authority on the mapping
    (``/etc/wsl.conf`` can change it). A backslash path is normalised to forward slashes first:
    ``wslpath`` reads a backslash as a literal and answers with a mangled path.
    """
    key = (distro, host_path)
    cached = _WSL_PATH_CACHE.get(key)
    if cached:
        return cached
    normalised = host_path.replace("\\", "/")
    translated = ""
    try:
        completed = subprocess.run(
            [wsl_exe, "-d", distro, "--", "wslpath", "-a", normalised],
            capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
        )
        if completed.returncode == 0:
            translated = (completed.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        translated = ""
    result = translated or _conventional_wsl_path(normalised)
    _WSL_PATH_CACHE[key] = result
    return result


def container_mount_source(root: str) -> str:
    r"""The workspace as the *engine* must see it.

    A bridged (WSL) engine is Linux, so a Windows path is translated. A ``\\wsl$\<distro>\...``
    share is the supported spelling of a Linux-filesystem workspace and translates directly. A
    Windows drive would become ``/mnt/c/...`` -- a 9p DrvFs bind mount, through which a container
    creates files with no Windows ACL. That corrupts the host's permissions, so it is refused
    rather than silently performed.
    """
    resolved = str(Path(root).resolve())
    argv = docker_bin()
    distro = _wsl_distro(argv)
    if not distro:
        return resolved
    for candidate in (resolved, str(root)):
        linux = _wsl_unc_to_linux(candidate)
        if linux:
            return linux
    translated = _wsl_path(resolved, distro, argv[0])
    if _DRVFS_RE.match(translated):
        raise SandboxError(
            f"the workspace {resolved!r} is on a Windows drive, and a container writing through a "
            f"9p (DrvFs) bind mount produces files with no Windows ACL -- mode 0000, unreadable "
            f"by the host. Refusing rather than corrupting them. Put the workspace on the Linux "
            f"filesystem and address it from Windows through the WSL share of {distro!r} "
            f"(the '\\\\wsl$' form), or run the engine with Windows file sharing (Docker Desktop)."
        )
    return translated


# --------------------------------------------------------------------------- identity


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


# --------------------------------------------------------------------------- the image


def available() -> bool:
    """Whether the Docker daemon can be reached. Never raises.

    A daemon that is down does **not** reliably report a non-zero status: on Windows the client
    was observed to exit 0 with the diagnostic on stderr and an empty stdout, so success requires
    a status of zero *and* an actual answer on stdout.
    """
    argv = [*docker_bin(), "info", "--format", "{{.ServerVersion}}"]
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=WSL_PROBE_TIMEOUT,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and bool((completed.stdout or "").strip())


def image_present(image_name: str) -> bool:
    """Whether the engine already has ``image_name``. Never raises.

    A positive answer is cached for the process: the inspect is a spawn, and the image does not
    usually vanish mid-run. A negative answer is not cached, so a build that follows is visible.
    """
    if image_name in _VERIFIED_IMAGES:
        return True
    try:
        completed = subprocess.run(
            [*docker_bin(), "image", "inspect", image_name],
            capture_output=True, text=True, timeout=WSL_PROBE_TIMEOUT,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if completed.returncode == 0:
        _VERIFIED_IMAGES.add(image_name)
        return True
    return False


def build_image(image_name: Optional[str] = None) -> bool:
    """Build the sandbox image from :data:`DOCKERFILE`. Returns whether it is present afterwards."""
    target = image_name or image()
    if not DOCKERFILE.is_file():
        return False
    dockerfile_arg = str(DOCKERFILE)
    context_arg = str(DOCKERFILE.parent)
    argv = docker_bin()
    distro = _wsl_distro(argv)
    if distro:
        # The build runs inside the distribution, so both paths must be in its namespace.
        dockerfile_arg = _wsl_path(dockerfile_arg, distro, argv[0])
        context_arg = _wsl_path(context_arg, distro, argv[0])
    try:
        completed = subprocess.run(
            [*argv, "build", "-f", dockerfile_arg, "-t", target, context_arg],
            capture_output=True, text=True, timeout=BUILD_TIMEOUT_SECONDS,
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
    lock_path = os.path.join(tempfile.gettempdir(), "deepagents-sandbox-image.lock")
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


# --------------------------------------------------------------------------- the command


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
    workspace: it is the only host path mounted, and the container's working directory.
    """
    uid, gid = host_identity()
    return [
        *docker_bin(), "run", "--rm",
        "--name", name,
        "--network=none",
        "--user", f"{uid}:{gid}",
        "--workdir", WORKSPACE_MOUNT,
        "--mount", f"type=bind,source={container_mount_source(root)},target={WORKSPACE_MOUNT}",
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
        subprocess.run(
            [*docker_bin(), "rm", "-f", name],
            capture_output=True, text=True, timeout=KILL_TIMEOUT_SECONDS,
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

    target = ensure_image()
    name = f"deepagents-exec-{uuid.uuid4().hex[:12]}"
    argv = build_command(
        command, root=str(root), name=name,
        memory_mb=memory_mb, max_processes=max_processes, image_name=target,
    )
    try:
        process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace",
        )
    except OSError as error:
        raise SandboxError(
            f"the container runtime {docker_bin()[0]!r} could not be started: {error}"
        ) from error

    try:
        stdout, stderr = process.communicate(timeout=int(timeout))
    except subprocess.TimeoutExpired:
        # The container owns its process tree, so killing the client is not enough: the
        # container is force-removed by name and only then is the client reaped.
        _force_remove(name)
        process.kill()
        stdout, stderr = process.communicate()
        return IsolatedResult(
            -1, stdout or "", stderr or "", sandboxed=True, timed_out=True,
            notes="the container was force-removed after the timeout",
        )

    returncode = process.returncode if process.returncode is not None else -1
    stdout = stdout or ""
    stderr = stderr or ""
    if returncode in DOCKER_CLIENT_ERROR_CODES and _is_unreachable_runtime(stderr, stdout):
        raise SandboxError(f"the Docker daemon could not be reached ({_diagnostic(stderr)})")
    return IsolatedResult(returncode, stdout, stderr, sandboxed=True)
