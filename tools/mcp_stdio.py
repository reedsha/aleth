"""The stdio JSON-RPC runtime both in-repo MCP servers speak.

The framing and the method dispatch are the protocol, not the server's business: a
filesystem server and an exec server differ only in the tools they advertise and the
function those tools call. Keeping the wire in one place means a protocol fix -- a new
method, a corrected error shape -- lands once, and neither server can drift from MCP by
accident.

Stdout is the protocol channel. Nothing but framed JSON-RPC may be written to it, so every
server must send diagnostics to stderr.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Dict, List, Optional

PROTOCOL_VERSION = "2024-11-05"

ToolCall = Callable[[str, Dict[str, Any]], str]


def handle_message(
    message: Dict[str, Any],
    *,
    server_name: str,
    server_version: str,
    tools: List[Dict[str, Any]],
    call: ToolCall,
) -> Optional[Dict[str, Any]]:
    """Handle one JSON-RPC message. Returns a response, or ``None`` for a notification."""
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    # A notification carries no id and takes no reply -- ``notifications/initialized`` is the
    # one the handshake sends, and answering it would be a protocol violation.
    if request_id is None:
        return None

    if method == "initialize":
        requested = str(params.get("protocolVersion") or PROTOCOL_VERSION)
        return {
            "jsonrpc": "2.0", "id": request_id,
            "result": {
                "protocolVersion": requested,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": server_name, "version": server_version},
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}}

    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        try:
            text = call(name, arguments)
        except Exception as error:
            # A tool-level refusal is a *result* with ``isError``, not a JSON-RPC error: the
            # call was well-formed and the answer is "no". Either way the text never leaks
            # anything the caller was not allowed to have.
            return {
                "jsonrpc": "2.0", "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": f"{type(error).__name__}: {error}"}],
                    "isError": True,
                },
            }
        return {
            "jsonrpc": "2.0", "id": request_id,
            "result": {"content": [{"type": "text", "text": text}], "isError": False},
        }

    return {
        "jsonrpc": "2.0", "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def serve(
    *,
    server_name: str,
    server_version: str,
    tools: List[Dict[str, Any]],
    call: ToolCall,
) -> int:
    """Read framed JSON-RPC from stdin until EOF, answering on stdout."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        response = handle_message(
            message, server_name=server_name, server_version=server_version, tools=tools, call=call
        )
        if response is None:
            continue
        sys.stdout.write(json.dumps(response, ensure_ascii=True) + "\n")
        sys.stdout.flush()
    return 0
