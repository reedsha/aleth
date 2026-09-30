"""The agent execution loop: a model driving its bound MCP tools until it answers.

This is the piece the procedural branches never had. They hand a model a context blob and
take back text; an *agent* is a model that can act. Here the loop sends the model the tool
schemas bound from the live MCP session, executes whatever tool calls it returns **through
those same bound tools** (which route to the servers over JSON-RPC), feeds the results back,
and repeats until the model answers without asking for a tool.

Nothing is faked at the transport layer: a tool call here reaches a real server process and
comes back as a real result. Only the model's *decision* is injectable -- ``completer`` is a
parameter -- which is what lets a test drive the loop deterministically without mocking MCP.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Tuple

DEFAULT_MAX_TURNS = 6


def _tool_schemas(tools: List[Any]) -> List[Dict[str, Any]]:
    if not tools:
        return []
    from tools.mcp_tools import openai_tool_schemas

    return openai_tool_schemas(tools)


def run_tool_loop(
    *,
    session: Any,
    role: str,
    system_prompt: str,
    user_message: str,
    completer: Callable[..., Any],
    max_turns: int = DEFAULT_MAX_TURNS,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Drive ``completer`` through the tools bound for ``role``.

    Returns ``(final_text, transcript)``. The transcript is the evidence: one entry per tool
    call, with the arguments the model produced and the result the server returned.

    ``completer`` is called as ``completer(system=..., messages=..., tools=...)`` and must
    return an object with ``text`` and (optionally) ``tool_calls`` -- a list of
    ``{"name": ..., "arguments": {...}}``. A turn that returns no tool calls ends the loop.

    A tool that raises is reported into the transcript and back to the model as its result,
    rather than aborting: a refused edit is information the model needs, not a crash. The
    turn budget is a hard stop, so a model that loops forever cannot hang the run.
    """
    tools = session.get_bound_tools(role) if session is not None else []
    by_name = {tool.name: tool for tool in tools}
    schemas = _tool_schemas(tools)

    messages: List[Dict[str, Any]] = [{"role": "user", "content": user_message}]
    transcript: List[Dict[str, Any]] = []

    for _turn in range(max_turns):
        completion = completer(system=system_prompt, messages=list(messages), tools=schemas)
        text = str(getattr(completion, "text", "") or "")
        calls = list(getattr(completion, "tool_calls", None) or [])

        if not calls:
            return text, transcript

        messages.append({"role": "assistant", "content": text, "tool_calls": calls})
        for call in calls:
            name = str((call or {}).get("name") or "")
            arguments = dict((call or {}).get("arguments") or {})
            tool = by_name.get(name)
            if tool is None:
                result = f"Error: no tool named {name!r} is bound for role {role!r}."
            else:
                try:
                    # A bound tool: its body reads/writes through the live MCP session.
                    result = str(tool.invoke(arguments))
                except Exception as error:
                    result = f"Error: {type(error).__name__}: {error}"
            transcript.append({"tool": name, "arguments": arguments, "result": result})
            messages.append({"role": "tool", "name": name, "content": result})

    return "", transcript
