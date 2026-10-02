"""An MCP exec server over stdio: two tools, caged in the workspace.

**Containment.** ``cwd`` is forced to the resolved workspace root, and no argument may resolve
outside it: every non-punctuation run of the command line is resolved against the root and
refused if it lands anywhere else. ``..``, an absolute path, a Windows backslash traversal and
a drive letter all fail the same check. A command that names a file it may not reach is
refused, not run and filtered afterwards.

Execution goes through ``tools.docker_sandbox`` -- the container perimeter -- rather than a bare
``subprocess`` call: every command runs inside a Docker container with the workspace as its only
bind mount, no network, capped CPU/memory/pids, and the process tree owned by the container. The
output is ``[Exit Code: N]`` followed by the truncated text, the same shape the legacy runner
produced.

There is no native fallback: when the container runtime cannot be reached the command is
**refused**, never executed on the host.

**Two tools, because the Architect's boundary is narrower than the Coder's.**

* ``execute_command`` -- the Coder's full shell: anything inside the workspace.
* ``execute_restricted_command`` -- the Architect's verification shell. It applies the
  allow-list (no mutation commands, no inline interpreter code, no command chaining, and only
  the approved verification shapes) *before* running anything.

That second tool is why this server exists rather than the policy being dropped with the
module it used to live in: deleting a security boundary to satisfy a refactor would be a
regression, so the policy moved here -- next to the containment it belongs with -- instead of
disappearing.

Run standalone for a smoke test:

    python tools/mcp_exec_server.py --root <workspace>
"""

from __future__ import annotations

import os
import re
import signal
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tools import docker_sandbox, mcp_stdio, result_budget

SERVER_NAME = "aleth-exec"
SERVER_VERSION = "1.0.0"

DEFAULT_TIMEOUT_SECONDS = 30
# The model-facing cap lives in ``tools.result_budget`` (Phase 26), beside the file-read cap it
# shares its number with: one budget for everything a tool hands the model, stated in bytes.

# Every whitespace-free run of the command line that is not shell punctuation. The scan is
# deliberately NOT ``shlex``: in POSIX mode shlex consumes a backslash as an escape, so
# ``..\\..\\secret.txt`` arrives as ``....secret.txt`` and a Windows traversal walks straight
# through a token check. Separators are normalised and every run is resolved instead, so the
# spelling and the quoting cannot hide a path.
_PATH_RUN_RE = re.compile(r"[^\s'\"`;|&()<>]+")

# --- the Architect's restricted shell policy -------------------------------------------
# Matched against *tokens*, not as substrings: a substring test for "reg" also rejected
# `python -m py_compile regression_test.py`, so legitimate verification of a workspace file
# could not be run.
RESTRICTED_DISALLOWED_COMMANDS = {
    "rm", "rmdir", "rd", "del", "erase", "format", "reg", "regedit",
    "shutdown", "mkfs", "diskpart", "fdisk", "chmod", "chown",
}
RESTRICTED_DISALLOWED_SUBCOMMANDS = {
    ("pip", "install"), ("pip", "uninstall"),
    ("npm", "install"), ("npm", "uninstall"), ("npm", "add"), ("npm", "remove"),
    ("yarn", "add"), ("yarn", "remove"),
}
# Destructive argument sequences, matched on the whole command: these appear inside quoted
# strings, where token inspection alone would not see them.
RESTRICTED_DISALLOWED_SEQUENCES = ("rm -rf", "rm -fr", "rm -r /", "del /f", "del /q", "del /s")
# Sequential separators. The allow-list judges the *first* token, but the shell runs every
# clause, so ``pytest ; pip install x`` clears a first-token check and then does what the
# check forbids.
RESTRICTED_DISALLOWED_SEPARATORS = (";", "&&", "||", "`", "$(")
# Python is handled separately: as an allowed prefix it let ``python -c "..."`` -- arbitrary
# code with no file to inspect -- through the "restricted" shell.
RESTRICTED_INTERPRETERS = {"python", "python3", "py"}
RESTRICTED_INLINE_FLAGS = {"-c", "-i", "-"}
RESTRICTED_ALLOWED_PYTHON_MODULES = {"pytest", "py_compile", "compileall", "unittest"}
RESTRICTED_ALLOWED_PREFIXES = [
    "pytest", "dir", "ls", "cat", "type", "echo",
    "git status", "git log", "git diff", "curl",
]


def restricted_denial(command: str) -> Optional[str]:
    """The refusal text for a command the Architect's shell must not run, or ``None``."""
    cmd_clean = command.strip().lower()
    try:
        import shlex

        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    first = os.path.basename(tokens[0]).lower() if tokens else ""

    # A Python interpreter is transparent for `-m <module>`: `python -m pip install x` must be
    # judged as `pip install x`, not waved through on the `python` prefix.
    effective = tokens
    if first in RESTRICTED_INTERPRETERS and len(tokens) >= 2 and tokens[1] == "-m":
        effective = tokens[2:]
    eff_first = os.path.basename(effective[0]).lower() if effective else ""

    denied_reason = None
    if eff_first in RESTRICTED_DISALLOWED_COMMANDS:
        denied_reason = eff_first
    elif len(effective) >= 2 and (eff_first, effective[1].lower()) in RESTRICTED_DISALLOWED_SUBCOMMANDS:
        denied_reason = f"{eff_first} {effective[1].lower()}"
    elif any(seq in cmd_clean for seq in RESTRICTED_DISALLOWED_SEQUENCES):
        denied_reason = next(seq for seq in RESTRICTED_DISALLOWED_SEQUENCES if seq in cmd_clean)

    if denied_reason:
        return (
            f"Permission Denied: Command '{command}' involves environment mutation ({denied_reason}). "
            "The Lead Architect has restricted shell access (test & verify only). "
            "Delegate dependency installation or system changes to a Coder Sub-Agent."
        )

    chained = next((sep for sep in RESTRICTED_DISALLOWED_SEPARATORS if sep in command), None)
    if chained:
        return (
            f"Permission Denied: Command '{command}' chains multiple shell commands ({chained!r}). "
            "The Architect's restricted shell runs a single verification action at a time."
        )

    if first in RESTRICTED_INTERPRETERS and any(
        tok.split("=", 1)[0] in RESTRICTED_INLINE_FLAGS for tok in tokens[1:]
    ):
        return (
            f"Permission Denied: Command '{command}' executes inline code. "
            "The Architect may run a workspace verification *script* (python <script.py>), "
            "never a string expression."
        )

    is_allowed = any(cmd_clean.startswith(prefix) for prefix in RESTRICTED_ALLOWED_PREFIXES)
    if not is_allowed and first in RESTRICTED_INTERPRETERS:
        if effective is not tokens and eff_first in RESTRICTED_ALLOWED_PYTHON_MODULES:
            is_allowed = True
        else:
            script_args = [tok for tok in tokens[1:] if not tok.startswith("-")]
            is_allowed = bool(script_args) and script_args[0].lower().endswith(".py")
    if not is_allowed and ("test" in cmd_clean or cmd_clean.endswith(".py") or "check" in cmd_clean):
        is_allowed = True

    if not is_allowed:
        return (
            f"Permission Denied: Command '{command}' is not in the Architect's approved verification whitelist. "
            "Approved commands: pytest, python -m py_compile <file>, python <script.py>, ls, dir, cat, git diff/status."
        )
    return None


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "execute_command",
        "description": (
            "Run a shell command with the working directory forced to the workspace root. "
            "Any argument that resolves outside the workspace is refused."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command line to run."},
                "timeout_seconds": {
                    "type": "integer",
                    "description": f"Hard limit, default {DEFAULT_TIMEOUT_SECONDS}.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "execute_restricted_command",
        "description": (
            "Run a verification command as the Architect: pytest, a compile check, a "
            "workspace script, or an inspection. Mutations, inline interpreter code and "
            "command chaining are refused before anything runs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The verification command."},
                "timeout_seconds": {
                    "type": "integer",
                    "description": f"Hard limit, default {DEFAULT_TIMEOUT_SECONDS}.",
                },
            },
            "required": ["command"],
        },
    },
]


class CommandEscaped(Exception):
    """An argument resolved outside the workspace root. Never a soft failure."""


def run_workspace_command_result(
    command: str, *, root: str, timeout: int = DEFAULT_TIMEOUT_SECONDS,
    allow_network: bool = False, resource_profile: str = "default",
) -> Tuple[str, Optional[Any]]:
    """The formatted block **and** the isolated result, so a caller can record the receipt.

    :func:`run_workspace_command` is this function's first half; the pair exists so the exec
    server can keep the forensics (identity, duration, full-stream hashes) without changing the
    contract every existing caller and test depends on.

    A refusal returns ``None`` for the result: no container ran, so there is nothing to record.
    A timeout *does* carry a result -- an execution that had to be killed is telemetry too.

    ``resource_profile`` names the budget the container runs under (``tools.docker_sandbox``). It
    is set from the plan's capabilities, never from a tool argument, so a model cannot buy itself
    more memory or CPU.
    """
    from tools import docker_sandbox

    # Resolved to a *bounded* pair here, in the parent's own module: an unknown name is the
    # default profile, and the builder clamps whatever it is handed.
    memory_mb, cpus = docker_sandbox.resource_limits(resource_profile)
    try:
        result = docker_sandbox.run_isolated(
            command, cwd=str(Path(root).resolve()), timeout=int(timeout),
            memory_mb=memory_mb, cpus=cpus, allow_network=allow_network,
        )
    except docker_sandbox.SandboxError as error:
        return (
            f"Error: Command refused: container isolation is required but unavailable ({error}). "
            "Start the Docker daemon, or point ALETH_DOCKER_BIN at a reachable runtime. "
            "Commands are never run on the host.",
            None,
        )

    if result.timed_out:
        return f"Error: Command timed out after {timeout} seconds.", result

    if result.oom_killed:
        # Not a test failure and not a transient fault: the command asked for more memory than its
        # budget allows. Said plainly so neither the model nor the engine reads it as a flake to
        # retry -- the ledger records the same fact as ``OOM_KILLED``.
        return (
            f"[Exit Code: {result.returncode}]\n"
            f"[OOM-KILLED] the container exceeded its {memory_mb}MB memory limit and was killed "
            f"by the kernel's OOM killer; the command did not complete.",
            result,
        )

    parts = []
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(f"[STDERR]\n{stderr}")
    body = "\n".join(parts) or "(Command executed successfully with no output)"
    return f"[Exit Code: {result.returncode}]\n{result_budget.truncate_result(body)}", result


def run_workspace_command(command: str, *, root: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> str:
    """Run one command in a container, rooted at ``root``. Returns the ``[Exit Code: N]`` block.

    The single entry point both tools use, and the one the isolation tests drive directly.
    A container refusal is *reported* in the result rather than raised: the caller is a
    verification gate, and "this could not be run safely" is an answer it must be able to
    read, not an exception that aborts a run. It is never a licence to run on the host.
    """
    return run_workspace_command_result(command, root=root, timeout=timeout)[0]


def _outcome_of(result: Any) -> str:
    """Why an execution ended, when it did not end on its own terms.

    ``137`` is SIGKILL, and inside a memory-capped container that is the kernel's OOM killer. The
    ledger records it so the engine can tell a memory leak apart from a genuine test failure
    instead of re-queuing the node until its retry budget runs out.
    """
    if getattr(result, "oom_killed", False):
        return "OOM_KILLED"
    if getattr(result, "timed_out", False):
        return "TIMEOUT"
    return ""


class ExecServer:
    """The command runner, bound to one root directory."""

    def __init__(
        self,
        root: str,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        db_path: Optional[str] = None,
        session_id: str = "",
        allow_network: bool = False,
        resource_profile: str = "default",
    ):
        self.root = Path(root).resolve()
        self.timeout_seconds = int(timeout_seconds)
        # Where the forensic receipt goes, and which run it belongs to. Empty means "no ledger":
        # a caller that did not ask for telemetry gets none, rather than a crash on a missing path.
        self.db_path = str(db_path) if db_path else ""
        self.session_id = str(session_id or "")
        # The network boundary. Off unless the plan declared the capability that needs it, which
        # the parent decided before this process existed -- nothing a tool call says can change it.
        self.allow_network = bool(allow_network)
        # The resource budget, decided the same way and for the same reason.
        self.resource_profile = str(resource_profile or "default")

    def _record_receipt(self, tool: str, result: Any) -> str:
        """Append the forensic receipt. Returns ``""`` on success, or the fault to report.

        The rule this implements: a receipt that cannot be written is a **critical fault**, not a
        warning. An execution whose evidence was lost must not read as a clean success, so the
        fault is surfaced both in the tool result the model sees and on stderr.
        """
        if not self.db_path:
            return ""
        try:
            from storage import telemetry

            telemetry.record(
                self.db_path,
                execution_id=result.execution_id,
                session_id=self.session_id,
                target_tool=tool,
                exit_code=int(result.returncode),
                duration_ms=int(result.duration_ms),
                stdout_hash=result.stdout_sha256,
                stderr_hash=result.stderr_sha256,
                outcome=_outcome_of(result),
            )
        except Exception as error:
            return f"{type(error).__name__}: {error}"
        return ""

    def _assert_contained(self, command: str) -> None:
        """Refuse the command if any argument resolves outside the root.

        Separators are normalised to ``/`` and every non-punctuation run is resolved against
        the root: a run that lands outside it is refused. Flags resolve harmlessly inside, an
        absolute path wins over the join and is refused, and ``..`` segments are resolved away
        before the containment test -- so no shape of string slips past.
        """
        normalised = command.replace("\\", "/")
        for run in _PATH_RUN_RE.findall(normalised):
            candidate = (self.root / run).resolve()
            if candidate != self.root and self.root not in candidate.parents:
                raise CommandEscaped(
                    f"Path traversal denied: argument {run!r} resolves outside the workspace root."
                )

    def execute_command(
        self, command: str, timeout_seconds: Optional[int] = None, *, tool: str = "execute_command"
    ) -> str:
        text = str(command or "").strip()
        if not text:
            raise ValueError("an empty command was given")
        self._assert_contained(text)
        output, result = run_workspace_command_result(
            text, root=str(self.root), timeout=int(timeout_seconds or self.timeout_seconds),
            allow_network=self.allow_network, resource_profile=self.resource_profile,
        )
        if result is not None:
            fault = self._record_receipt(tool, result)
            if fault:
                print(f"[exec-server] TELEMETRY FAULT ({tool}): {fault}", file=sys.stderr)
                output += (
                    f"\n[TELEMETRY FAULT] the execution receipt could not be recorded: {fault}"
                )
        return output

    def execute_restricted_command(self, command: str, timeout_seconds: Optional[int] = None) -> str:
        """The Architect's verification shell: policy first, then the same sandboxed run."""
        text = str(command or "").strip()
        if not text:
            raise ValueError("an empty command was given")
        denial = restricted_denial(text)
        if denial:
            return denial
        return self.execute_command(text, timeout_seconds, tool="execute_restricted_command")

    def call(self, name: str, arguments: Dict[str, Any]) -> str:
        command = str(arguments.get("command") or "")
        timeout = arguments.get("timeout_seconds")
        if name == "execute_command":
            return self.execute_command(command, timeout, tool=name)
        if name == "execute_restricted_command":
            return self.execute_restricted_command(command, timeout)
        raise ValueError(f"unknown tool {name!r}")


def _truncate(text: str) -> str:
    """Cap the command's output for the model (``tools.result_budget``).

    Kept as a name because every caller here means "the model-facing budget", and the *number* is
    shared with the filesystem server's file reads -- a model that cats a log and one that reads a
    file meet the same ceiling.
    """
    return result_budget.truncate_result(text)


def install_container_reaper() -> None:
    """Remove this server's in-flight containers when the server is asked to stop.

    The orphan this closes: this server launches a container per command, and the MCP client
    spawns *this* server. When the client goes away -- a run is stopped, a session is torn down --
    it signals this process first (SIGTERM, then SIGKILL after a grace period). A container whose
    server was killed keeps running, because the server was the only thing that knew its name.

    SIGTERM is therefore the one chance to clean up, and this is where it is taken. It is not a
    substitute for the timeout path inside ``run_isolated`` -- that covers a command that runs too
    long; this covers the *server* being killed underneath a command.

    The removal is requested **detached** (``purge_active_containers(detached=True)``), and that
    detail is the whole guarantee. The client sends SIGTERM, waits its grace period, then SIGKILLs
    this process's whole group -- and this handler's ``docker rm`` child used to be *in* that
    group, so a removal slower than the grace (under a parallel suite, common) was killed
    mid-flight and the container outlived the server it belonged to. In its own session the
    removal cannot be reached by that kill, so the container dies either way.

    A no-op where the platform cannot deliver these signals, and on a thread that is not the main
    one (``signal.signal`` refuses both): the guarantee is then the client's group kill, which is
    what the handler exists to complement.
    """

    def _reap(signum, _frame):
        try:
            removed = docker_sandbox.purge_active_containers(detached=True)
            print(
                f"[exec-server] signal {signum}: removed {len(removed)} in-flight container(s)",
                file=sys.stderr,
            )
        except Exception as error:  # never let cleanup replace the reason we are stopping
            print(f"[exec-server] container cleanup failed: {error}", file=sys.stderr)
        # Leave now: the client is tearing this process down, and a half-served request would
        # write a reply nobody is listening for.
        raise SystemExit(0)

    for name in ("SIGTERM", "SIGINT"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            signal.signal(number, _reap)
        except (ValueError, OSError):
            # Not the main thread, or the platform refuses: the group kill still covers us.
            pass


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = os.getcwd()
    if "--root" in args:
        root = args[args.index("--root") + 1]
    db_path = args[args.index("--db-path") + 1] if "--db-path" in args else None
    session_id = args[args.index("--session-id") + 1] if "--session-id" in args else ""
    resource_profile = (
        args[args.index("--resource-profile") + 1] if "--resource-profile" in args else "default"
    )
    server = ExecServer(
        root,
        db_path=db_path,
        session_id=session_id,
        # Flags on *this* process's command line, set by the parent from the plan's declaration.
        # They are not tool arguments: the model cannot reach them.
        allow_network="--allow-network" in args,
        resource_profile=resource_profile,
    )
    install_container_reaper()
    return mcp_stdio.serve(
        server_name=SERVER_NAME,
        server_version=SERVER_VERSION,
        tools=TOOLS,
        call=server.call,
    )


if __name__ == "__main__":
    raise SystemExit(main())
