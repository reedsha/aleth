"""The run-scoped MCP lifecycle: servers live exactly as long as one workflow run.

Every MCP server is an ephemeral child process. Something has to own their lifetime, and it
must not be ``runner.py``: process management is not sequencing. This module owns it.

* :class:`MCPSessionContext` spawns the servers, completes their handshakes, and aggregates
  their tools into bindable framework tools. A session built with a **capability set** -- a node's
  ``required_capabilities`` -- spawns only the servers that set needs and hands the model only the
  tools it named (Phase 7.4). An empty set is a deliberate and enforced "no tools".
* It is a context manager, so the children are **reaped unconditionally** on the way out --
  including when a branch raises or a halt unwinds the run. There is no path through
  ``__exit__`` that leaves a process behind.
* :class:`~orchestration.workflow.context.WorkflowContext` carries the live session, so every
  action branch reaches the servers through ``ctx.mcp_session`` instead of importing a file
  or shell helper. That is what lets the engine primitives and the shell runner live in
  their own modules -- ``tools/workspace_io.py`` and ``tools/mcp_exec_server.py`` -- instead
  of a shared file-and-shell toolbox the whole app reached into.

The servers are spawned once per run rather than once per call, so a run pays one process
start each instead of one per tool use. (A warm pool across runs is the next step -- see the
timeout note in ``tools/mcp_client.py``.)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from tools.mcp_client import (
    DEFAULT_TIMEOUT,
    MCPClient,
    default_command,
    default_exec_command,
)
from tools.mcp_tools import bind_mcp_tools

# Which servers each agent role binds. Both roles need to read and write the workspace and to
# run a verification command; the split exists so a future role can be narrowed without
# touching the agents. This is the *engine's* default scope (``capabilities=None``); a session
# scoped to a node's capabilities decides its tool set from that declaration instead.
ROLE_SERVERS: Dict[str, tuple] = {
    "architect": ("fs", "exec"),
    "coder": ("fs", "exec"),
}

FS_SERVER = "fs"
EXEC_SERVER = "exec"
# The orchestrator-side surgical tools, which are not a server: they are built per session and
# reach the disk through the filesystem server.
AST_GROUP = "ast"

# Tools an agent is never offered. ``write_file`` is the engine's overwrite -- the workflow
# must be able to refresh its own deliverable on a re-run -- so handing it to a model would
# restore exactly the blind-rewrite behaviour the chokehold exists to prevent. A model writes
# through ``create_file`` (refuses an existing path) or surgically through ``edit_ast_node``.
ENGINE_ONLY_TOOLS = frozenset({"write_file"})


class UnknownCapabilityError(ValueError):
    """A node asked for a capability this build does not implement.

    Raised rather than ignored. A capability that quietly resolves to nothing would leave the
    model without the tool it asked for and no explanation -- the failure would surface later as
    an off-contract artifact, and nobody would know why.
    """


# The capability vocabulary. A node's ``required_capabilities`` names *capabilities*, not tools:
# the model asks for what the work needs and this table decides which tools that is, so a server
# can gain a tool without the planner's vocabulary changing.
#
# ``servers`` is what must be *live* for the capability to work; ``groups`` is what the model is
# actually handed. The two differ for ``ast``: the surgical tools read and write through the
# filesystem server, so it must be spawned -- but the node asked to edit symbols, not to hold the
# raw file tools, and it is handed only what it asked for.
CAPABILITIES: Dict[str, Dict[str, tuple]] = {
    "fs": {"servers": (FS_SERVER,), "groups": (FS_SERVER,)},
    "exec": {"servers": (EXEC_SERVER,), "groups": (EXEC_SERVER,)},
    "ast": {"servers": (FS_SERVER,), "groups": (AST_GROUP,)},
}

# Accepted spellings. Explicit and small on purpose: this is a vocabulary, not fuzzy matching, so
# a planner that writes the URI-ish name it was shown (``mcp://local-fs``) is understood, and
# anything genuinely unknown is refused rather than guessed at.
CAPABILITY_ALIASES: Dict[str, str] = {
    "local-fs": "fs",
    "filesystem": "fs",
    "files": "fs",
    "shell": "exec",
    "terminal": "exec",
    "bash": "exec",
    "ast-parser": "ast",
    "surgical": "ast",
    "symbols": "ast",
}


def resolve_capability(name: Any) -> str:
    """A capability name as its canonical key, or raise :class:`UnknownCapabilityError`.

    ``mcp://`` is stripped and the name is lowercased before lookup, because the planner is shown
    the URI-ish spelling. The alias table is consulted after that and nothing else is: an
    unrecognised name is an error, not an empty tool set.
    """
    key = str(name or "").strip().lower().removeprefix("mcp://")
    key = CAPABILITY_ALIASES.get(key, key)
    if key not in CAPABILITIES:
        raise UnknownCapabilityError(
            f"unknown capability {name!r}; this build implements {sorted(CAPABILITIES)} "
            f"(accepted aliases: {sorted(CAPABILITY_ALIASES)})"
        )
    return key


class MCPSessionContext:
    """One run's MCP servers, their tools, and their teardown.

    ``capabilities`` decides the session's scope, and the two values mean different things:

    * ``None`` -- the **engine's** session. The workflow needs its own file and shell I/O whatever
      the plan says, so every server is spawned and the *role* decides the agent's tool set. This
      is the run's session in ``runner.py``.
    * a sequence -- a **node's** session. Only the servers the named capabilities need are spawned,
      and only the tools they name are bound. ``[]`` is the strongest form of that: no servers, no
      tools. It is how a node that declared nothing is punished for it rather than handed
      everything.
    """

    def __init__(
        self,
        root: str,
        *,
        capabilities: Optional[Sequence[str]] = None,
        timeout: float = DEFAULT_TIMEOUT,
        fs_command: Optional[List[str]] = None,
        exec_command: Optional[List[str]] = None,
    ):
        self.root = str(root)
        self.timeout = timeout
        self._commands = {
            FS_SERVER: fs_command or default_command(self.root),
            EXEC_SERVER: exec_command or default_exec_command(self.root),
        }
        self._clients: Dict[str, MCPClient] = {}
        self._bound: Dict[str, List[Any]] = {}
        # Servers that failed to start, by name, with the reason. Reported rather than raised:
        # a run that cannot reach one server should still be able to use the other.
        self.failures: Dict[str, str] = {}
        # Resolved eagerly so a hallucinated capability fails at construction, with a message that
        # names the vocabulary, rather than as a mysteriously empty tool set later.
        self.capabilities: Optional[tuple] = (
            None if capabilities is None else tuple(str(name) for name in capabilities)
        )
        self._resolved: Optional[tuple] = (
            None if capabilities is None
            else tuple(resolve_capability(name) for name in capabilities)
        )

    # -- lifecycle ---------------------------------------------------------------
    def _wanted_servers(self) -> tuple:
        """The servers this session may spawn. The engine's session spawns them all."""
        if self._resolved is None:
            return tuple(self._commands)
        wanted: List[str] = []
        for capability in self._resolved:
            for server in CAPABILITIES[capability]["servers"]:
                if server not in wanted:
                    wanted.append(server)
        return tuple(wanted)

    def start(self) -> "MCPSessionContext":
        for name in self._wanted_servers():
            try:
                client = MCPClient(self._commands[name], timeout=self.timeout)
                client.start()
                client.initialize()
            except Exception as error:
                self.failures[name] = str(error)
                continue
            self._clients[name] = client
        return self

    def close(self) -> None:
        """Kill and reap every child. Idempotent, and never raises."""
        for client in list(self._clients.values()):
            try:
                client.close()
            except Exception:
                pass
        self._clients.clear()
        self._bound.clear()

    def __enter__(self) -> "MCPSessionContext":
        return self.start()

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        # Unconditional: a raise, a halt or a clean return all land here.
        self.close()

    # -- servers and tools -------------------------------------------------------
    def client(self, server: str) -> Optional[MCPClient]:
        return self._clients.get(server)

    def is_live(self, server: str) -> bool:
        return server in self._clients

    def get_bound_tools(self, role: str) -> List[Any]:
        """The tools this session hands an agent role.

        Nothing is hardcoded about *what the servers offer*: the schemas come from each server's
        ``tools/list`` response at the moment of binding, so a tool added to a server is added to
        the agent with no Python change. What is decided here is *which* of them the model sees:

        * a session scoped to a node's capabilities binds only the groups that node named, and
          nothing else. An empty declaration binds nothing at all -- the diet is enforced, not
          suggested, and there is deliberately no "load everything" fallback for it;
        * the engine's session (``capabilities=None``) binds the role's servers plus the surgical
          tools, which is the full set the workflow's own agents have always had;
        * the engine's overwrite writer is withheld from a model either way (``ENGINE_ONLY_TOOLS``).
        """
        tools: List[Any] = []
        for group in self._bind_groups(role):
            tools.extend(self._group_tools(group))
        return tools

    def _bind_groups(self, role: str) -> tuple:
        """Which tool groups this session binds, in a stable order."""
        if self._resolved is None:
            # The engine's session: the role decides, and the surgical tools always ride along.
            return tuple(ROLE_SERVERS.get(role, ())) + (AST_GROUP,)
        groups: List[str] = []
        for capability in self._resolved:
            for group in CAPABILITIES[capability]["groups"]:
                if group not in groups:
                    groups.append(group)
        return tuple(groups)

    def _group_tools(self, group: str) -> List[Any]:
        """One group's tools: a server's advertised set, or the orchestrator-side surgical tools."""
        if group == AST_GROUP:
            return self._ast_tools()
        if group not in self._bound:
            client = self._clients.get(group)
            self._bound[group] = bind_mcp_tools(client) if client is not None else []
        return [tool for tool in self._bound[group] if tool.name not in ENGINE_ONLY_TOOLS]

    def _ast_tools(self) -> List[Any]:
        """The surgical tools, built once per session and cached with it."""
        if AST_GROUP not in self._bound:
            from tools.ast_tools import build_ast_tools

            self._bound[AST_GROUP] = build_ast_tools(self)
        return self._bound[AST_GROUP]

    def tool_names(self, role: str) -> List[str]:
        return [tool.name for tool in self.get_bound_tools(role)]

    # -- the run's I/O -----------------------------------------------------------
    def read_file(self, path: str) -> str:
        """Read a workspace file through the filesystem server."""
        client = self._clients.get(FS_SERVER)
        if client is None:
            raise RuntimeError(f"the {FS_SERVER!r} MCP server is not available: {self.failures.get(FS_SERVER, '')}")
        return client.read_file(path)

    def write_file(self, path: str, content: str) -> str:
        """Write a workspace file through the filesystem server."""
        client = self._clients.get(FS_SERVER)
        if client is None:
            raise RuntimeError(f"the {FS_SERVER!r} MCP server is not available: {self.failures.get(FS_SERVER, '')}")
        return client.write_file(path, content)

    def execute(self, command: str, timeout_seconds: Optional[int] = None, *, restricted: bool = False) -> str:
        """Run a command through the exec server, whose cwd is forced to the workspace.

        ``restricted=True`` selects the Architect's verification tool, which applies the
        allow-list before anything runs. The policy lives in the server, beside the
        containment it belongs with -- so deleting the legacy shell module did not delete the
        boundary along with it.
        """
        client = self._clients.get(EXEC_SERVER)
        if client is None:
            raise RuntimeError(f"the {EXEC_SERVER!r} MCP server is not available: {self.failures.get(EXEC_SERVER, '')}")
        tool = "execute_restricted_command" if restricted else "execute_command"
        arguments: Dict[str, Any] = {"command": command}
        if timeout_seconds is not None:
            arguments["timeout_seconds"] = timeout_seconds
        return client.call_tool(tool, arguments)
