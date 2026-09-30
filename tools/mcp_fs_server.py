"""A minimal Model Context Protocol filesystem server, over stdio.

This is a **standard-protocol** MCP server: it speaks JSON-RPC 2.0 over stdin/stdout with
the framing MCP defines (one JSON object per line), implements ``initialize``,
``tools/list`` and ``tools/call``, and declares its tools with JSON Schemas. It is not an
in-process helper -- the orchestrator spawns it as an ephemeral child process, talks to it
over the pipe, and kills it (see :mod:`tools.mcp_client`). A stock filesystem server can
replace it by changing the command, because nothing here is bespoke to the caller.

Three tools, and only three:

* ``read_file(path)`` -- the file's text, or an error result when it is missing;
* ``create_file(path, content)`` -- the **model's** writer. It refuses an existing path, so a
  model cannot dump a whole file over code it did not reproduce;
* ``write_file(path, content)`` -- the **engine's** writer. It creates or replaces, because the
  workflow must be able to refresh its own deliverable on a re-run.

The split is the chokehold. Phase 3.5 established that whole-file rewriting by an LLM is how
unrelated lines get silently deleted; the answer is not a flag on one tool but two tools with
different callers -- the model gets ``create_file``, the engine keeps ``write_file``, and a
surgical change goes through the orchestrator's ``edit_ast_node`` (``tools/ast_tools.py``),
which replaces an exact byte span.

**Containment is the whole security story.** Every path is resolved with ``Path.resolve()``
and must land inside the server's root, which is the workspace the orchestrator granted. A
resolved-path comparison cannot be talked around: ``..``, a Windows backslash traversal, an
absolute path, a symlink pointing out of the tree and a drive letter are all refused by the
same check. This is the boundary that must not leak ``~/.ssh/id_rsa``.

Run standalone for a smoke test:

    python tools/mcp_fs_server.py --root <workspace>
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools import mcp_stdio

SERVER_NAME = "aleth-filesystem"
SERVER_VERSION = "1.0.0"

# The refusal every blind whole-file overwrite gets. One spelling, so the message the model
# sees and the message a test asserts on cannot drift.
OVERWRITE_REFUSED = "Overwriting entire files is forbidden. Use edit_ast_node."

# The tools this server advertises. The schemas are JSON Schema, as MCP requires.
TOOLS: List[Dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file inside the workspace root.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Workspace-relative path."}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write a UTF-8 text file inside the workspace root, creating parents.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative path."},
                "content": {"type": "string", "description": "The full text to write."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "create_file",
        "description": (
            "Create a NEW file with the given contents. Refuses to overwrite an existing file: "
            "to change code that already exists, call list_symbols then edit_ast_node."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative path."},
                "content": {"type": "string", "description": "The full text of the new file."},
            },
            "required": ["path", "content"],
        },
    },
]


class PathDenied(Exception):
    """A path resolved outside the server's root. Never a soft failure."""


class FilesystemServer:
    """The tool implementations, bound to one root directory."""

    def __init__(self, root: str):
        self.root = Path(root).resolve()

    def _resolve(self, raw_path: str) -> Path:
        """Resolve ``raw_path`` against the root and refuse anything outside it.

        ``Path.resolve()`` on both sides is the whole guard: it normalises ``..``, expands
        a symlink to its target and makes an absolute argument absolute, so the containment
        test that follows cannot be fooled by the shape of the string.
        """
        candidate = (self.root / str(raw_path or "")).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise PathDenied(f"Path traversal denied: {raw_path!r} is outside the workspace root.")
        return candidate

    def read_file(self, path: str) -> str:
        target = self._resolve(path)
        with open(target, "r", encoding="utf-8", errors="replace", newline="") as handle:
            return handle.read()

    def write_file(self, path: str, content: str) -> str:
        target = self._resolve(path)
        if target.parent and not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        return f"Wrote {len(content)} characters to {path}."

    def create_file(self, path: str, content: str) -> str:
        """The model's writer: create, never replace.

        Refused rather than warned, because the damage a blind rewrite does -- dropping every
        line the model did not happen to reproduce -- is silent and unrecoverable from the
        tool result. A surgical change has a tool of its own (``edit_ast_node``), so refusing
        this costs the model nothing but a second call.
        """
        target = self._resolve(path)
        if target.exists():
            raise FileExistsError(f"{OVERWRITE_REFUSED} ({path})")
        return self.write_file(path, content)

    def call(self, name: str, arguments: Dict[str, Any]) -> str:
        if name == "read_file":
            return self.read_file(str(arguments.get("path") or ""))
        if name == "write_file":
            return self.write_file(str(arguments.get("path") or ""), str(arguments.get("content") or ""))
        if name == "create_file":
            return self.create_file(str(arguments.get("path") or ""), str(arguments.get("content") or ""))
        raise ValueError(f"unknown tool {name!r}")


def handle(server: FilesystemServer, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One message, answered through the shared runtime.

    A boundary refusal stays a *tool result* with ``isError`` rather than a JSON-RPC error:
    the call was well-formed and the answer is "no", and either way the text never leaks.
    """
    def call(name: str, arguments: Dict[str, Any]) -> str:
        return server.call(name, arguments)

    return mcp_stdio.handle_message(
        message,
        server_name=SERVER_NAME,
        server_version=SERVER_VERSION,
        tools=TOOLS,
        call=call,
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = os.getcwd()
    if "--root" in args:
        root = args[args.index("--root") + 1]
    server = FilesystemServer(root)
    # stdout is the protocol channel: nothing but framed JSON-RPC may be written to it. A
    # diagnostic goes to stderr, where the orchestrator can read it without corrupting the
    # stream.
    return mcp_stdio.serve(
        server_name=SERVER_NAME,
        server_version=SERVER_VERSION,
        tools=TOOLS,
        call=server.call,
    )


if __name__ == "__main__":
    raise SystemExit(main())
