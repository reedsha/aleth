"""A stand-in for the ``docker`` CLI, so the isolation tests need no Docker daemon.

It is not a container and does not pretend to be one. It is a *double* that lets the real code
path be exercised on a machine with no daemon: ``docker_sandbox`` still builds the full argv,
still spawns a real child process, and still parses its output and exit status -- only the
runtime at the far end is emulated. It understands exactly three invocations:

* ``docker info ...`` -- reports a version, so ``available()`` is true;
* ``docker image inspect <name>`` -- reports the image present, unless ``FAKE_DOCKER_IMAGE_MISSING``;
* ``docker build ...`` -- reports success, unless ``FAKE_DOCKER_BUILD_FAIL``;
* ``docker run ... <image> /bin/sh -c <command>`` -- runs ``<command>`` in the bind mount's
  source directory (the workspace) and passes its stdout/stderr/exit status back, the way a
  container would, and appends the argv it received to ``$FAKE_DOCKER_LOG``;
* ``docker rm -f <name>`` -- appends to the log and exits 0.

Every invocation's argv is appended to ``$FAKE_DOCKER_LOG`` (one JSON line each) when that
variable names a file, which is how a test asserts the *contract* -- the flags the payload was
launched under -- rather than only its output.

Optional knobs, read from the environment:

* ``FAKE_DOCKER_LOG``         -- append one JSON argv line per invocation here;
* ``FAKE_DOCKER_SLEEP``       -- sleep this many seconds before answering (drives the timeout);
* ``FAKE_DOCKER_STDOUT``      -- print this and skip running the command at all;
* ``FAKE_DOCKER_STDERR``      -- write this to stderr;
* ``FAKE_DOCKER_EXIT``        -- exit with this status (with ``FAKE_DOCKER_STDOUT``);
* ``FAKE_DOCKER_DAEMON_DOWN`` -- answer as a client that cannot reach the daemon (exit 127);
* ``FAKE_DOCKER_INFO_EMPTY``  -- answer ``info`` with status 0 and no stdout, which is what a
  Windows client does when the daemon is down (the trap ``available()`` must not fall into);
* ``FAKE_DOCKER_IMAGE_MISSING`` -- answer ``image inspect`` with "no such image";
* ``FAKE_DOCKER_BUILD_FAIL``  -- fail ``docker build``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time


def _log(argv: list) -> None:
    path = os.environ.get("FAKE_DOCKER_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(argv) + "\n")
    except OSError:
        pass


def _mount_source(argv: list) -> str:
    """The bind mount's ``source=``, i.e. the workspace the emulated container is cwd'd into."""
    for index, token in enumerate(argv):
        if token == "--mount" and index + 1 < len(argv):
            for part in str(argv[index + 1]).split(","):
                if part.startswith("source="):
                    return part[len("source="):]
    return os.getcwd()


_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _split_assignments(command: str):
    """Leading ``NAME=value`` words, as a POSIX shell applies them, and the command that remains.

    A real container runs the payload under ``/bin/sh -c``, where ``FOO=1 cmd`` sets an
    environment variable for that command. The host shell on Windows does not understand that
    form, so the emulator splits it out itself -- otherwise a payload like
    ``PYTHONDONTWRITEBYTECODE=1 python -m pytest`` would simply fail here while working in a
    container, and the emulation would be lying about which commands are runnable.
    """
    tokens = command.split()
    assignments = {}
    index = 0
    while index < len(tokens) and _ASSIGNMENT_RE.match(tokens[index]):
        name, _, value = tokens[index].partition("=")
        assignments[name] = value
        index += 1
    return assignments, " ".join(tokens[index:])


def _container_env() -> dict:
    """The emulated container's environment.

    Only ``PATH`` is adjusted: ``python`` is resolved to the interpreter running the suite, so
    the emulated image provides the project's toolchain -- which is exactly what the real image
    must do (see ``ALETH_SANDBOX_IMAGE``). No host secret is passed on.
    """
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
    return env


def _info() -> int:
    if os.environ.get("FAKE_DOCKER_INFO_EMPTY"):
        return 0
    sys.stdout.write("25.0.0\n")
    return 0


def _image_inspect() -> int:
    if os.environ.get("FAKE_DOCKER_IMAGE_MISSING"):
        sys.stderr.write("Error response from daemon: No such image\n")
        return 1
    sys.stdout.write("[]\n")
    return 0


def _build() -> int:
    if os.environ.get("FAKE_DOCKER_BUILD_FAIL"):
        sys.stderr.write("The command '/bin/sh -c pip install ...' returned a non-zero code: 1\n")
        return 1
    sys.stdout.write("Successfully tagged aleth-sandbox:latest\n")
    return 0


def _run(argv: list) -> int:
    if os.environ.get("FAKE_DOCKER_DAEMON_DOWN"):
        sys.stderr.write(
            'docker: error during connect: this error may indicate that the docker daemon '
            'is not running.\n'
        )
        return 127

    sleep = os.environ.get("FAKE_DOCKER_SLEEP")
    if sleep:
        time.sleep(float(sleep))

    canned = os.environ.get("FAKE_DOCKER_STDOUT")
    if canned is not None:
        sys.stdout.write(canned)
        sys.stderr.write(os.environ.get("FAKE_DOCKER_STDERR") or "")
        return int(os.environ.get("FAKE_DOCKER_EXIT", "0"))

    # The emulated container: run the payload in the mounted workspace, exactly as the
    # container's ``/bin/sh -c <command>`` would, and hand back its streams and status.
    command = argv[-1] if argv else ""
    assignments, payload = _split_assignments(command)
    env = _container_env()
    env.update(assignments)
    completed = subprocess.run(
        payload, shell=True, cwd=_mount_source(argv), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )
    sys.stdout.write(completed.stdout or "")
    sys.stderr.write(completed.stderr or "")
    return completed.returncode


def main(argv: list) -> int:
    _log(argv)
    if not argv:
        return 2
    subcommand = argv[0]
    if subcommand == "info":
        return _info()
    if subcommand == "image":
        return _image_inspect()
    if subcommand == "build":
        return _build()
    if subcommand == "rm":
        return 0
    if subcommand == "run":
        return _run(argv)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
