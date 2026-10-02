"""The agent execution loop: a model driving its bound MCP tools until it answers.

This is the piece the procedural branches never had. They hand a model a context blob and
take back text; an *agent* is a model that can act. Here the loop sends the model the tool
schemas bound from the live MCP session, executes whatever tool calls it returns **through
those same bound tools** (which route to the servers over JSON-RPC), feeds the results back,
and repeats until the model answers without asking for a tool.

Nothing is faked at the transport layer: a tool call here reaches a real server process and
comes back as a real result. Only the model's *decision* is injectable -- ``completer`` is a
parameter -- which is what lets a test drive the loop deterministically without mocking MCP.

**The ceiling is a fault, not an answer (Phase 26).** A model still asking for tools after
``MAX_AGENT_STEPS`` is not deliberating, it is looping -- rewriting the same file, re-reading the
same symbol, burning the token budget. So the loop stops, records the fault in the ledger, and
raises: returning an empty answer instead would hand the caller a blank plan that looks like a
decision, and the run would carry on against nothing.
"""

from __future__ import annotations

import sys
from typing import Any, Callable, Dict, List, Tuple

# The hard ceiling on model-driven tool calls in one pass. Generous for real work -- a plan needs a
# handful of reads and one edit -- and small enough that a hallucinated loop cannot run all day.
MAX_AGENT_STEPS = 30

# One spelling, shared with the engine that reads the terminal event and fails the intent.
STEP_LIMIT_MESSAGE = "Exceeded maximum execution steps"


class AgentStepLimitExceeded(RuntimeError):
    """The model kept calling tools past the ceiling. A fault, not an answer.

    Deliberately *not* caught by the planner's broad ``except Exception`` (which means "the model
    did not return a plan"): a loop that ran away is a different thing, and swallowing it would
    turn the ceiling into a silent no-plan.
    """


class RunAborted(RuntimeError):
    """The intent this pass belongs to was aborted underneath it (Phase 27).

    Raised by the liveness gate, which is checked before every step and before every tool call: the
    engine must not mutate a file, or write telemetry, for an intent that has already been aborted.
    """


ABORTED_MESSAGE = "the intent was aborted before this pass finished"


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
    max_steps: int = MAX_AGENT_STEPS,
    intent_id: str = "",
    ledger: Any = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Drive ``completer`` through the tools bound for ``role``.

    Returns ``(final_text, transcript)``. The transcript is the evidence: one entry per tool
    call, with the arguments the model produced and the result the server returned.

    ``completer`` is called as ``completer(system=..., messages=..., tools=...)`` and must
    return an object with ``text`` and (optionally) ``tool_calls`` -- a list of
    ``{"name": ..., "arguments": {...}}``. A turn that returns no tool calls ends the loop.

    A tool that raises is reported into the transcript and back to the model as its result,
    rather than aborting: a refused edit is information the model needs, not a crash. The step
    ceiling is a hard stop *and a fault*: it records the fault and raises
    :class:`AgentStepLimitExceeded`, so a model that loops forever cannot hang the run and cannot
    be mistaken for one that answered.

    ``intent_id``/``ledger`` are the correlation and the liveness gate (Phase 27). ``ledger`` is an
    object with ``path`` and ``is_live(intent_id)`` -- injected rather than resolved here, because
    this loop also runs in a swarm *child* process where the plan directory is not the parent's. The
    gate only applies when both are present, so a caller with neither (a bare unit test) still gets
    the step ceiling.
    """
    tools = session.get_bound_tools(role) if session is not None else []
    by_name = {tool.name: tool for tool in tools}
    schemas = _tool_schemas(tools)
    gated = bool(ledger is not None and str(intent_id or "").strip())

    def _live() -> bool:
        return not gated or bool(ledger.is_live(intent_id))

    messages: List[Dict[str, Any]] = [{"role": "user", "content": user_message}]
    transcript: List[Dict[str, Any]] = []

    for _step in range(max_steps):
        if not _live():
            raise RunAborted(ABORTED_MESSAGE)
        completion = completer(system=system_prompt, messages=list(messages), tools=schemas)
        text = str(getattr(completion, "text", "") or "")
        calls = list(getattr(completion, "tool_calls", None) or [])

        if not calls:
            return text, transcript

        messages.append({"role": "assistant", "content": text, "tool_calls": calls})
        for call in calls:
            # Re-checked per call, not only per step: a tool *mutates*, and an abort that landed
            # while the model was thinking must not be overtaken by the write it was planning.
            if not _live():
                raise RunAborted(ABORTED_MESSAGE)
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

    _record_step_limit(ledger, intent_id, role, max_steps)
    raise AgentStepLimitExceeded(STEP_LIMIT_MESSAGE)


def _record_step_limit(ledger: Any, intent_id: str, role: str, steps: int) -> None:
    """Write the fault to the forensic ledger, joined to the intent. Best effort.

    Skipped outright when the intent is no longer live: the engine must not write telemetry for a
    run that has already been aborted, and a fault row for one would be exactly the detached noise
    the correlation id exists to remove.

    A telemetry write that fails is reported and the raise goes ahead regardless: losing the
    *record* of a runaway loop is bad, and losing the *stop* would be worse.
    """
    if ledger is None:
        print(
            "[agent-loop] the step-limit fault has no ledger to be recorded in",
            file=sys.stderr,
        )
        return
    if intent_id and not ledger.is_live(intent_id):
        return

    from storage import telemetry

    try:
        telemetry.record_agent_fault(
            ledger.path,
            intent_id=intent_id,
            kind="STEP_LIMIT_EXCEEDED",
            role=str(role),
            detail=STEP_LIMIT_MESSAGE,
            steps=int(steps),
        )
    except Exception as error:
        print(
            f"[agent-loop] the step-limit fault could not be recorded: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
        )
