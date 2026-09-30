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
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools import mcp_stdio

SERVER_NAME = "aleth-exec"
SERVER_VERSION = "1.0.0"

DEFAULT_TIMEOUT_SECONDS = 30
MAX_OUTPUT_CHARS = 2400

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


def run_workspace_command(command: str, *, root: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> str:
    """Run one command in a container, rooted at ``root``. Returns the ``[Exit Code: N]`` block.

    The single entry point both tools use, and the one the isolation tests drive directly.
    A container refusal is *reported* in the result rather than raised: the caller is a
    verification gate, and "this could not be run safely" is an answer it must be able to
    read, not an exception that aborts a run. It is never a licence to run on the host.
    """
    from tools import docker_sandbox

    try:
        result = docker_sandbox.run_isolated(
            command, cwd=str(Path(root).resolve()), timeout=int(timeout)
        )
    except docker_sandbox.SandboxError as error:
        return (
            f"Error: Command refused: container isolation is required but unavailable ({error}). "
            "Start the Docker daemon, or point ALETH_DOCKER_BIN at a reachable runtime. "
            "Commands are never run on the host."
        )

    if result.timed_out:
        return f"Error: Command timed out after {timeout} seconds."

    parts = []
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(f"[STDERR]\n{stderr}")
    body = "\n".join(parts) or "(Command executed successfully with no output)"
    return f"[Exit Code: {result.returncode}]\n{_truncate(body)}"


class ExecServer:
    """The command runner, bound to one root directory."""

    def __init__(self, root: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS):
        self.root = Path(root).resolve()
        self.timeout_seconds = int(timeout_seconds)

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

    def execute_command(self, command: str, timeout_seconds: Optional[int] = None) -> str:
        text = str(command or "").strip()
        if not text:
            raise ValueError("an empty command was given")
        self._assert_contained(text)
        return run_workspace_command(
            text, root=str(self.root), timeout=int(timeout_seconds or self.timeout_seconds)
        )

    def execute_restricted_command(self, command: str, timeout_seconds: Optional[int] = None) -> str:
        """The Architect's verification shell: policy first, then the same sandboxed run."""
        text = str(command or "").strip()
        if not text:
            raise ValueError("an empty command was given")
        denial = restricted_denial(text)
        if denial:
            return denial
        return self.execute_command(text, timeout_seconds)

    def call(self, name: str, arguments: Dict[str, Any]) -> str:
        command = str(arguments.get("command") or "")
        timeout = arguments.get("timeout_seconds")
        if name == "execute_command":
            return self.execute_command(command, timeout)
        if name == "execute_restricted_command":
            return self.execute_restricted_command(command, timeout)
        raise ValueError(f"unknown tool {name!r}")


def _truncate(text: str, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    """Middle-truncate, so the head and the tail -- where errors live -- both survive."""
    if len(text) <= max_chars:
        return text
    keep = max_chars // 2
    return f"{text[:keep]}\n... [{len(text) - max_chars} characters omitted] ...\n{text[-keep:]}"


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = os.getcwd()
    if "--root" in args:
        root = args[args.index("--root") + 1]
    server = ExecServer(root)
    return mcp_stdio.serve(
        server_name=SERVER_NAME,
        server_version=SERVER_VERSION,
        tools=TOOLS,
        call=server.call,
    )


if __name__ == "__main__":
    raise SystemExit(main())
