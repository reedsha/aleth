import os
import re
import subprocess
import shlex
import sys
from typing import Optional
from langchain_core.tools import tool
from tools.file_tools import get_project_dir

# Token Efficiency Limit: Max output characters before truncating middle
MAX_SHELL_OUTPUT_CHARS = 2400

# Disallowed commands for the restricted Architect shell. These are matched against
# the *command tokens*, not as substrings: a substring test for "reg" also rejected
# `python -m py_compile regression_test.py`, and "format" rejected `format_utils.py`,
# so legitimate verification of a workspace file could not be run.
RESTRICTED_DISALLOWED_COMMANDS = {
    "rm", "rmdir", "rd", "del", "erase", "format", "reg", "regedit",
    "shutdown", "mkfs", "diskpart", "fdisk", "chmod", "chown",
}

# Destructive sub-commands, matched as (command, sub-command).
RESTRICTED_DISALLOWED_SUBCOMMANDS = {
    ("pip", "install"), ("pip", "uninstall"),
    ("npm", "install"), ("npm", "uninstall"), ("npm", "add"), ("npm", "remove"),
    ("yarn", "add"), ("yarn", "remove"),
}

# Destructive argument sequences, matched on the whole command: these appear inside
# quoted strings (e.g. `python -c "... rm -rf ..."`), where token inspection alone
# would not see them. The strings are specific enough not to collide with a path.
RESTRICTED_DISALLOWED_SEQUENCES = ("rm -rf", "rm -fr", "rm -r /", "del /f", "del /q", "del /s")

# Allowed command purposes for Architect: testing, running code verification, inspecting logs/files
RESTRICTED_ALLOWED_PREFIXES = [
    "pytest",
    "python",
    "py",
    "dir",
    "ls",
    "cat",
    "type",
    "echo",
    "git status",
    "git log",
    "git diff",
    "curl",
]

def truncate_output_for_token_efficiency(output: str, max_chars: int = MAX_SHELL_OUTPUT_CHARS) -> str:
    """
    Summarizes/truncates large command outputs to prevent context window bloat and excessive token consumption.
    Preserves critical initial command output and tail exit/trace info.
    """
    if len(output) <= max_chars:
        return output
    half = max_chars // 2
    head = output[:half]
    tail = output[-half:]
    omitted = len(output) - max_chars
    return f"{head}\n\n... [Omitted {omitted} characters to preserve context window & optimize tokens] ...\n\n{tail}"

def run_command_in_workspace(command: str, timeout: int = 30) -> str:
    """Executes a command within the current project workspace directory with token-efficient truncation."""
    cwd = get_project_dir()
    try:
        res = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace"
        )
        stdout = res.stdout.strip()
        stderr = res.stderr.strip()
        
        output_parts = []
        if stdout:
            output_parts.append(stdout)
        if stderr:
            output_parts.append(f"[STDERR]\n{stderr}")
            
        full_output = "\n".join(output_parts)
        if not full_output:
            full_output = "(Command executed successfully with no output)"
            
        truncated = truncate_output_for_token_efficiency(full_output)
        return f"[Exit Code: {res.returncode}]\n{truncated}"
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout} seconds."
    except Exception as e:
        return f"Execution error: {str(e)}"


# ``run_command_in_workspace`` formats its answer as ``[Exit Code: N]`` on the first line.
# That block is *never empty* -- even a clean run returns the header plus a success note --
# so a caller cannot test success with ``bool(result)``. This parser lives beside the format
# string so the two cannot drift apart.
_EXIT_CODE_RE = re.compile(r"^\[Exit Code:\s*(-?\d+)\]")


def command_exit_code(result: str) -> Optional[int]:
    """The exit code a ``run_command_in_workspace`` result reports, or ``None``.

    ``None`` means no exit status was produced at all -- the command timed out, could not
    start, or was refused before it ran. That is *inconclusive*, deliberately distinct from
    a command that actually ran and exited non-zero.
    """
    match = _EXIT_CODE_RE.match((result or "").strip())
    return int(match.group(1)) if match else None


def command_failed(result: str) -> bool:
    """True only when the command ran and exited non-zero.

    An inconclusive result -- no exit status at all -- reports ``False``: a verification
    gate must not read "could not judge" as "failed".
    """
    code = command_exit_code(result)
    return code is not None and code != 0

@tool
def execute_shell_command(command: str) -> str:
    """
    [Coder Full Access] Executes arbitrary system shell commands in the project directory.
    Use this to scaffold projects, install dependencies, run linters, and perform build tasks.
    Outputs are automatically token-optimized.
    """
    return run_command_in_workspace(command)

@tool
def execute_restricted_command(command: str) -> str:
    """
    [Architect Restricted Access] Executes restricted shell commands in the project directory.
    Strictly limited to: running test suites (e.g. pytest), verifying application output,
    and inspecting file logs. Cannot perform environment mutations like package installation.
    Outputs are automatically token-optimized.
    """
    cmd_clean = command.strip().lower()

    # 1. Check for disallowed mutation commands. Tokens are inspected so a file name
    # or an argument that merely *contains* a forbidden word is not mistaken for it.
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    first = os.path.basename(tokens[0]).lower() if tokens else ""

    denied_reason = None
    if first in RESTRICTED_DISALLOWED_COMMANDS:
        denied_reason = first
    elif len(tokens) >= 2 and (first, tokens[1].lower()) in RESTRICTED_DISALLOWED_SUBCOMMANDS:
        denied_reason = f"{first} {tokens[1].lower()}"
    elif any(seq in cmd_clean for seq in RESTRICTED_DISALLOWED_SEQUENCES):
        denied_reason = next(seq for seq in RESTRICTED_DISALLOWED_SEQUENCES if seq in cmd_clean)

    if denied_reason:
        return (
            f"Permission Denied: Command '{command}' involves environment mutation ({denied_reason}). "
            "The Lead Architect has restricted shell access (test & verify only). "
            "Delegate dependency installation or system changes to a Coder Sub-Agent."
        )

    # 2. Verify command fits verification/testing/inspecting scope
    is_allowed = any(cmd_clean.startswith(prefix) for prefix in RESTRICTED_ALLOWED_PREFIXES)
    if not is_allowed:
        if "test" in cmd_clean or cmd_clean.endswith(".py") or "check" in cmd_clean:
            is_allowed = True

    if not is_allowed:
        return (
            f"Permission Denied: Command '{command}' is not in the Architect's approved verification whitelist. "
            "Approved commands: pytest, python <verification_script>, ls, dir, cat, git diff/status."
        )

    return run_command_in_workspace(command)

# Tool bundles for export
coder_shell_tools = [execute_shell_command]
architect_shell_tools = [execute_restricted_command]
