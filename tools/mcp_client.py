"""A Model Context Protocol client over stdio.

The orchestrator spawns an MCP server as an **ephemeral child process**, speaks JSON-RPC
2.0 to it over the pipe (one JSON object per line, the framing MCP defines), and kills the
process when the execution phase yields. There is no daemon and no HTTP: a server lives
exactly as long as the pass that needs it, so there is nothing left running to leak.

This is deliberately dependency-free. The ``mcp`` package is not required -- the transport
is a pipe and the messages are JSON, and both are in the standard library -- so the client
works in a checkout with no MCP wheels installed, and a stock server can be dropped in by
changing the command.

A reader thread drains stdout into a queue rather than a blocking ``readline`` on the
request path: a server that dies, or that never answers, must surface as a timeout on the
call rather than hanging the workflow thread forever.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from tools import env_sanitizer, process_control, stream_drain

PROTOCOL_VERSION = "2024-11-05"
# Every IPC read is bounded. A server that stalls is killed and reaped rather than left
# holding a pipe, which is the one failure mode that hangs an orchestrator forever.
#
# 15s, and the number is measured rather than chosen: this client spawns a *cold* Python
# interpreter per pass, and the exec server's calls additionally pay for a sandboxed child
# spawn. Under the parallel test suite (16 workers, each with its own children) a 5s budget
# was observed to fail healthy servers on a loaded machine, which is worse than useless. The
# kill-on-stall behaviour is what matters and it is unconditional; the budget only has to be
# generous enough not to fire on a healthy but slow call. Once a server is reused across runs
# (a warm pool) this can drop to a few hundred milliseconds.
DEFAULT_TIMEOUT = 15.0

# The in-repo filesystem server. A stock server replaces it by passing a different command;
# nothing in this client is specific to it.
SERVER_SCRIPT = Path(__file__).with_name("mcp_fs_server.py")
EXEC_SERVER_SCRIPT = Path(__file__).with_name("mcp_exec_server.py")
# The repo root (the directory holding the ``tools`` package). The server children are
# launched from here and given it on ``PYTHONPATH``: they import ``tools.mcp_stdio``, so the
# *server process's* working directory must be importable. The workspace they are caged in is
# passed as ``--root`` and is a different thing entirely -- conflating the two is what broke
# the first version of this.
REPO_ROOT = SERVER_SCRIPT.parent.parent


class MCPError(RuntimeError):
    """The server refused, died, or did not answer in time."""


class MCPClient:
    """One stdio session with one MCP server process."""

    def __init__(
        self,
        command: List[str],
        *,
        cwd: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.command = list(command)
        self.cwd = cwd
        self.timeout = timeout
        self._process: Optional[subprocess.Popen] = None
        self._responses: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        # Bounded: a server that floods its log must not be able to grow this process. See
        # ``tools.stream_drain`` for the window and why both ends are kept.
        self._stderr = stream_drain.DualBuffer()
        self._next_id = 1
        self._lock = threading.Lock()
        self.server_info: Dict[str, Any] = {}

    # -- lifecycle ---------------------------------------------------------------
    def start(self) -> "MCPClient":
        if self._process is not None:
            return self
        # The child gets an **allow-listed** environment, never the host's. A model-authored tool
        # call reaches this process, and ``dict(os.environ)`` handed it every credential the
        # operator had exported. ``PYTHONPATH`` is added deliberately: the server imports
        # ``tools``, and that is this app's decision rather than something the shell exported.
        env = env_sanitizer.sanitized_environment(extra={"PYTHONPATH": str(REPO_ROOT)})
        self._process = subprocess.Popen(
            self.command,
            cwd=str(REPO_ROOT),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            # Its own process group, so one signal reaches the whole tree the server forks.
            **process_control.spawn_kwargs(),
        )
        threading.Thread(target=self._drain_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        return self

    def _drain_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._responses.put(json.loads(line))
            except ValueError:
                continue

    def _drain_stderr(self) -> None:
        """Read stderr continuously into a bounded window.

        Continuously, because a child that writes more than the pipe buffer blocks in ``write()``
        until someone reads. Bounded, because the window is 1 MB: before this, the lines were
        appended to an unbounded list, so a server that logged megabytes -- or looped -- grew
        this process without limit.
        """
        process = self._process
        if process is None or process.stderr is None:
            return
        stream_drain.drain_text(process.stderr, self._stderr)

    def close(self) -> None:
        """End the child's whole process group and reap it. Idempotent.

        The group, not the process: a server that forked a helper would leave it running if only
        the direct child were signalled. The final ``wait()`` is what leaves no zombie -- see
        ``tools.process_control`` for why a ``waitpid(-1)`` sweep is the wrong tool here.
        """
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            process_control.terminate_group(process)
        else:
            # Already gone: collect the status so it cannot linger as a zombie.
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        process_control.close_pipes(process)

    def __enter__(self) -> "MCPClient":
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.close()

    @property
    def stderr_text(self) -> str:
        """The server's stderr, as the bounded receipt (head, marker, tail)."""
        return stream_drain.decode(self._stderr.render()).strip()

    # -- JSON-RPC ----------------------------------------------------------------
    def _send(self, payload: Dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise MCPError("the MCP server is not running")
        try:
            process.stdin.write(json.dumps(payload, ensure_ascii=True) + "\n")
            process.stdin.flush()
        except OSError as error:
            raise MCPError(f"the MCP server closed the pipe: {error}") from error

    def _request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})

        deadline = time.monotonic() + self.timeout
        while True:
            try:
                message = self._responses.get(
                    timeout=max(0.0, deadline - time.monotonic())
                )
            except queue.Empty:
                # Stalled: kill it, reap it, fail the call. Leaving the child alive would
                # leak a process and a pipe, and a retry would queue behind it.
                stderr = self.stderr_text
                self.close()
                raise MCPError(
                    f"no answer to {method!r} within {self.timeout}s; the MCP server was killed"
                    + (f". Server stderr: {stderr}" if stderr else "")
                )
            if message.get("id") != request_id:
                continue  # a notification or a stray response: not ours
            if "error" in message:
                error = message["error"] or {}
                raise MCPError(f"{method!r} failed: {error.get('message') or error}")
            return message.get("result")

    def _notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # -- MCP ---------------------------------------------------------------------
    def initialize(self) -> Dict[str, Any]:
        """The MCP handshake: negotiate, then announce the client is ready."""
        result = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "aleth-orchestrator", "version": "1.0.0"},
        })
        self.server_info = result or {}
        self._notify("notifications/initialized")
        return self.server_info

    def list_tools(self) -> List[Dict[str, Any]]:
        result = self._request("tools/list") or {}
        return result.get("tools") or []

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """Call one tool. A tool-level refusal raises, so it can never read as content."""
        result = self._request("tools/call", {"name": name, "arguments": arguments}) or {}
        text = "\n".join(
            str(block.get("text", ""))
            for block in (result.get("content") or [])
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if result.get("isError"):
            raise MCPError(text or f"tool {name!r} failed")
        return text

    # -- convenience for the executor --------------------------------------------
    def read_file(self, path: str) -> str:
        return self.call_tool("read_file", {"path": path})

    def write_file(self, path: str, content: str) -> str:
        return self.call_tool("write_file", {"path": path, "content": content})


def default_command(root: str) -> List[str]:
    """The command that starts the in-repo filesystem server bound to ``root``."""
    return [sys.executable, str(SERVER_SCRIPT), "--root", str(root)]


def default_exec_command(
    root: str,
    *,
    db_path: Optional[str] = None,
    session_id: Optional[str] = None,
    resource_profile: Optional[str] = None,
) -> List[str]:
    """The command that starts the in-repo exec server bound to ``root``.

    ``db_path``/``session_id`` are how the server records its forensic receipt
    (``storage.telemetry``). They travel as arguments rather than through the environment on
    purpose: the child's environment is an allow-list (``tools.env_sanitizer``), so a path the
    child needs is a decision the parent states, not something it happens to export.

    ``resource_profile`` is the same kind of decision, and it is the only way the sandbox's
    hardware budget is ever widened. It is set from the plan's ``required_capabilities`` (see
    ``orchestration.mcp_session``) -- never from a tool call, so a model cannot grant itself memory.

    There is deliberately no network argument (Phase 28): egress is severed unconditionally, so
    there is nothing here to open it with.
    """
    command = [sys.executable, str(EXEC_SERVER_SCRIPT), "--root", str(root)]
    if db_path:
        command += ["--db-path", str(db_path)]
    if session_id:
        command += ["--session-id", str(session_id)]
    # Only a *non-default* profile travels: the baseline is the absence of the flag, which is what
    # lets the exec server's own default apply and keeps the engine's argv minimal.
    if resource_profile and resource_profile != "default":
        command += ["--resource-profile", str(resource_profile)]
    return command


@contextmanager
def mcp_workspace_client(
    root: str,
    *,
    command: Optional[List[str]] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Iterator[MCPClient]:
    """An initialised MCP session for ``root``, torn down when the block exits.

    The child is killed on the way out -- including when the block raises -- so an execution
    pass that fails still leaves no server running.
    """
    client = MCPClient(command or default_command(root), timeout=timeout)
    client.start()
    try:
        client.initialize()
        yield client
    finally:
        client.close()
