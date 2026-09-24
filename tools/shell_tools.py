import os
import subprocess
import shlex
import sys
from langchain_core.tools import tool
from tools.file_tools import get_project_dir

# Token Efficiency Limit: Max output characters before truncating middle
MAX_SHELL_OUTPUT_CHARS = 2400

# Disallowed command prefixes/keywords for restricted Architect shell
RESTRICTED_DISALLOWED_PATTERNS = [
    "pip install",
    "pip uninstall",
    "npm install",
    "npm uninstall",
    "yarn add",
    "rmdir",
    "del /",
    "rm -rf",
    "format",
    "reg",
    "powershell -c \"remove",
]

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

    # 1. Check for disallowed mutation patterns
    for dis in RESTRICTED_DISALLOWED_PATTERNS:
        if dis in cmd_clean:
            return (
                f"Permission Denied: Command '{command}' involves environment mutation ({dis}). "
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
