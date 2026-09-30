"""Dynamic MCP tool bindings: a server's schemas *are* the agent's tools.

There is deliberately no static catalog here. The orchestrator spawns an MCP server, asks it
for ``tools/list``, and translates each JSON Schema into the structured tool the agent
framework (and, through it, the LLM provider) needs. When the model emits a tool call this
module is a **dumb router**: it forwards the arguments to the server's ``tools/call`` and
returns the stringified result. Nothing is re-implemented, and nothing is hardcoded.

That is the whole point of the protocol. Adding a tool to a server adds it to every agent
that binds that server, with no Python change -- so the legacy static ``@tool`` catalogs
(the file tools and the two shell tools) have nothing left to do once a server is bound in
their place.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Type

from pydantic import Field, create_model

from tools.mcp_client import MCPClient

# JSON Schema primitives -> Python types. A schema type outside this map is carried as a
# string rather than rejected: a server's schema is the authority, and refusing an unfamiliar
# type would make an otherwise usable tool disappear.
_JSON_TYPES: Dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}


def _python_type(schema: Dict[str, Any]) -> Any:
    declared = str(schema.get("type") or "string")
    return _JSON_TYPES.get(declared, str)


def json_schema_to_pydantic(name: str, schema: Dict[str, Any]) -> Type[Any]:
    """A pydantic model for one MCP tool's ``inputSchema``.

    ``required`` decides which fields have no default; everything else is optional. The model
    is what the framework validates a model's tool call against and what it converts into the
    provider's function schema, so this is the single translation point -- a field that is
    wrong here is wrong everywhere, and only here.
    """
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or [])

    fields: Dict[str, Any] = {}
    for field_name, field_schema in properties.items():
        field_schema = field_schema if isinstance(field_schema, dict) else {}
        description = str(field_schema.get("description") or "")
        if field_name in required:
            fields[field_name] = (_python_type(field_schema), Field(..., description=description))
        else:
            fields[field_name] = (
                Optional[_python_type(field_schema)],
                Field(default=None, description=description),
            )
    return create_model(f"{name}_args", **fields)


def bind_mcp_tools(client: MCPClient, *, prefix: str = "") -> List[Any]:
    """The server's advertised tools, as framework tools that route back to it.

    ``prefix`` namespaces the tools when more than one server is bound, so two servers may
    each expose ``read_file`` without one shadowing the other. The description is the
    server's own, passed through unchanged -- the model reads what the server wrote.
    """
    from langchain_core.tools import StructuredTool

    bound: List[Any] = []
    for advertised in client.list_tools():
        name = str(advertised.get("name") or "")
        if not name:
            continue
        schema = advertised.get("inputSchema") or {}
        tool_name = f"{prefix}{name}"
        args_schema = json_schema_to_pydantic(tool_name, schema)

        def _run(_tool=name, **kwargs):
            # The router. The arguments the model produced go to the server verbatim; the
            # result comes back as the string the tool contract requires.
            return client.call_tool(_tool, kwargs)

        bound.append(
            StructuredTool.from_function(
                func=_run,
                name=tool_name,
                description=str(advertised.get("description") or ""),
                args_schema=args_schema,
            )
        )
    return bound


def openai_tool_schemas(tools: List[Any]) -> List[Dict[str, Any]]:
    """The provider-facing function schemas for a bound tool set.

    This is the second half of the translation -- framework tool -> provider schema -- kept
    here so the whole path (MCP JSON Schema -> pydantic -> provider) is in one module and can
    be asserted end to end.
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    return [convert_to_openai_tool(tool) for tool in tools]
