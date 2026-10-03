"""Phase 40: the pre-flight enforcer.

A V1 product does not ask a user to debug their own environment. Every check here answers one
question the engine will otherwise discover the hard way -- *after* it has accepted a run and
started writing state:

* is the container runtime reachable? The whole execution perimeter is Docker; an unreachable
  daemon is a refusal, not a fallback, so discovering it mid-run wastes a plan.
* is ``git`` on the PATH? The shadow's filtering, the project identity and the Phase 35 snapshots
  all shell out to it.
* is the daemon's port free? A zombie from a previous boot squatting on it produces a bind failure
  whose message names nothing useful.

Each check carries its own **fix**, because "it failed" is not a report. :func:`enforce` prints the
whole checklist -- passes included, so a user can see what was verified -- and returns ``False`` the
moment anything is wrong: no partial execution, ever.
"""

from __future__ import annotations

import dataclasses
import shutil
import socket
from typing import List, Optional

from tools import docker_sandbox


@dataclasses.dataclass(frozen=True)
class Check:
    """One pre-flight question, its answer, and -- when it failed -- how to fix it."""

    name: str
    ok: bool
    detail: str = ""
    fix: str = ""


def check_docker() -> Check:
    """The container runtime must be reachable: the execution perimeter is not optional."""
    try:
        if docker_sandbox.available():
            return Check("container runtime", True, "docker is reachable")
    except Exception as error:
        return Check(
            "container runtime", False, f"{type(error).__name__}: {error}",
            "start Docker (Docker Desktop, or `sudo systemctl start docker`) and retry",
        )
    return Check(
        "container runtime", False, "the docker daemon could not be reached",
        "start Docker (Docker Desktop, or `sudo systemctl start docker`) and retry. "
        "The engine runs every command in a container and refuses to fall back to the host.",
    )


def check_git() -> Check:
    """``git`` must be on the PATH: the shadow, the project identity and the snapshots use it."""
    found = shutil.which("git")
    if found:
        return Check("git", True, found)
    return Check(
        "git", False, "git was not found on the PATH",
        "install git (`apt-get install git`, `brew install git`, or https://git-scm.com/downloads) "
        "and reopen the shell so the PATH picks it up",
    )


def check_process_lock() -> Check:
    """The authoritative liveness question (Phase 41): is another engine running *this project*?

    A port probe cannot tell an Aleth daemon from an unrelated program, and it cannot tell a ghost
    from a live one -- so the lock is what decides. A stale lock is not a failure: the boot reaps it.
    """
    from tools import process_lock
    from tools.workspace import state_dir

    try:
        state = process_lock.check(state_dir())
    except Exception as error:
        return Check(
            "process lock", False, f"{type(error).__name__}: {error}",
            "check the permissions on the state directory and retry",
        )
    if state.acquired:
        return Check("process lock", True, state.detail)
    return Check(
        "process lock", False, state.detail,
        f"another Aleth engine is already running for this project. Stop it, or remove "
        f"{state.path} if you are certain it is stale.",
    )


def check_port(port: int) -> Check:
    """The daemon's port must be free -- a squatter produces a bind error naming nothing.

    Kept beside the process lock rather than replaced by it: the lock answers "another *engine*",
    and this answers "something else", which is the one case the lock cannot see.
    """
    wanted = int(port)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", wanted))
    except OSError as error:
        return Check(
            "api port", False, f"127.0.0.1:{wanted} is not free ({error})",
            f"find what holds it (`lsof -i :{wanted}` on macOS/Linux, "
            f"`netstat -ano | findstr :{wanted}` on Windows) and stop it, or run the engine on "
            f"another port with ALETH_API_PORT=<port>",
        )
    finally:
        probe.close()
    return Check("api port", True, f"127.0.0.1:{wanted} is free")


def check_keyring() -> Check:
    """Informational: without a keyring the engine still boots, it just cannot store credentials."""
    from tools import secrets

    if secrets.available():
        return Check("credential store", True, "an OS keyring backend is available")
    return Check(
        "credential store", True,
        "no OS keyring backend; credentials must come from the environment "
        "(install `keyring` to store them securely)",
    )


def run_checks(port: Optional[int] = None) -> List[Check]:
    """Every pre-flight check, in the order a user would want to fix them."""
    if port is None:
        from api.server import default_port

        port = default_port()
    return [check_docker(), check_git(), check_process_lock(), check_port(port), check_keyring()]


def failures(checks: List[Check]) -> List[Check]:
    """The checks that did not pass."""
    return [check for check in checks if not check.ok]


def report(checks: List[Check]) -> str:
    """The whole checklist as terminal-friendly text, with a fix line under each failure."""
    lines: List[str] = ["Aleth pre-flight:"]
    for check in checks:
        mark = "ok  " if check.ok else "FAIL"
        lines.append(f"  [{mark}] {check.name}: {check.detail}")
        if not check.ok and check.fix:
            lines.append(f"         fix: {check.fix}")
    broken = failures(checks)
    if broken:
        lines.append("")
        lines.append(
            f"{len(broken)} check(s) failed. Nothing was started -- fix the above and run again."
        )
    else:
        lines.append("  all checks passed.")
    return "\n".join(lines)


def enforce(port: Optional[int] = None, *, emit=print) -> bool:
    """Run the checklist, print it, and answer whether the engine may start.

    ``emit`` is the sink, so a test can capture the report without a terminal.
    """
    checks = run_checks(port)
    emit(report(checks))
    return not failures(checks)


__all__ = [
    "Check",
    "check_docker",
    "check_git",
    "check_keyring",
    "check_port",
    "check_process_lock",
    "enforce",
    "failures",
    "report",
    "run_checks",
]
