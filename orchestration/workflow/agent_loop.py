"""The agent execution loop: a model driving its bound MCP tools until it answers.

This is the piece the procedural branches never had. They hand a model a context blob and
take back text; an *agent* is a model that can act. Here the loop sends the model the tool
schemas bound from the live MCP session, executes whatever tool calls it returns **through
those same bound tools** (which route to the servers over JSON-RPC), feeds the results back,
and repeats until the model answers without asking for a tool.

Nothing is faked at the transport layer: a tool call here reaches a real server process and
comes back as a real result. Only the model's *decision* is injectable -- ``completer`` is a
parameter -- which is what lets a test drive the loop deterministically without mocking MCP.

**The ceiling is a fault, not an answer.** A model still asking for tools after ``MAX_AGENT_STEPS``
is not deliberating, it is looping. So the loop stops, records the fault, and raises: returning an
empty answer instead would hand the caller a blank plan that looks like a decision.

**History is bounded, not linear (Phase 31).** Appending every prompt, diff and 16 KB tool result
forever is how an agent bankrupts its user: by turn fifteen the payload is a hundred thousand tokens,
the model's attention degrades into "lost in the middle", and every subsequent call pays for the
whole history again. The payload is therefore three blocks -- the immutable system prompt, a rolling
summary of the older turns, and the last few turns verbatim -- and everything older is folded into
the summary by a cheap secondary model, or dropped with the model told so.

**And the bill is bounded too (Phase 32).** ``tools/token_budget`` counts what each call costs, the
**ledger** accumulates it (a swarm child spends against the same intent), and the breaker consults
that total before every call and every tool execution.
"""

from __future__ import annotations

import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from tools import token_budget

# The paused status is the ledger's vocabulary, not this module's: one spelling, defined where the
# state is written.
from storage.intents import PAUSED

# The hard ceiling on model-driven tool calls in one pass. Generous for real work -- a plan needs a
# handful of reads and one edit -- and small enough that a hallucinated loop cannot run all day.
MAX_AGENT_STEPS = 30

# One spelling, shared with the engine that reads the terminal event and fails the intent.
STEP_LIMIT_MESSAGE = "Exceeded maximum execution steps"

# The context window (Phase 31): the last few turns are carried verbatim, and once the history is
# longer than ``COMPRESS_AFTER_TURNS`` everything outside that window is folded into the summary.
CONTEXT_WINDOW_TURNS = 4
COMPRESS_AFTER_TURNS = 5

# The summary's hard ceiling, in tokens (Phase 33). The compressor is *recursive* -- it is handed the
# previous summary plus the newly dropped turns -- and its prompt asks for at most 500 words, but a
# model is not a bound. This is: without it, a summariser that ignored its instructions would make the
# summary grow every time it ran, and the payload would breach the window it exists to protect.
MAX_SUMMARY_TOKENS = 400
SUMMARY_WORD_LIMIT = 500

# How long the loop holds for a person after an interruption before giving the intent up. The run is
# paused, not failed -- but nothing may wait forever, and the plan TTL is the engine's own answer to
# "how long is too long".
PAUSE_TIMEOUT_SECONDS = 900.0
PAUSE_POLL_SECONDS = 0.25

# The fallback's message, and it is a *system* message on purpose: it is an instruction about the
# model's own memory, not something the user said.
TRUNCATION_NOTICE = (
    "Earlier turns have been truncated to bound the context window. Rely on the shadow workspace "
    "state: re-read a file or re-run a command if you need something that is no longer here."
)

SUMMARY_HEADING = "### Rolling state summary (older turns, compressed)\n\n"


class AgentStepLimitExceeded(RuntimeError):
    """The model kept calling tools past the ceiling. A fault, not an answer.

    Deliberately *not* caught by the planner's broad ``except Exception`` (which means "the model
    did not return a plan"): a loop that ran away is a different thing, and swallowing it would
    turn the ceiling into a silent no-plan.
    """


class RunAborted(RuntimeError):
    """The intent this pass belongs to was aborted underneath it.

    Raised by the liveness gate, which is checked before every step and before every tool call: the
    engine must not mutate a file, or write telemetry, for an intent that has already been aborted.
    """


ABORTED_MESSAGE = "the intent was aborted before this pass finished"


def _tool_schemas(tools: List[Any]) -> List[Dict[str, Any]]:
    if not tools:
        return []
    from tools.mcp_tools import openai_tool_schemas

    return openai_tool_schemas(tools)


def _render_turn(turn: Sequence[Dict[str, Any]]) -> str:
    """One turn as plain text, for the compressor or the transcript.

    The compressor is a model call, so what it is handed has to be text -- and it is rendered from
    the *messages* rather than from the transcript so a turn's tool results are included exactly as
    the model saw them.
    """
    lines: List[str] = []
    for message in turn:
        role = str(message.get("role") or "")
        if role == "assistant":
            text = str(message.get("content") or "").strip()
            if text:
                lines.append(f"[assistant] {text}")
            for call in message.get("tool_calls") or []:
                name = str((call or {}).get("name") or "")
                lines.append(f"[tool call] {name}({(call or {}).get('arguments')})")
        else:
            lines.append(f"[{role}] {str(message.get('content') or '').strip()}")
    return "\n".join(lines)


def _payload(
    *,
    summary: str,
    dropped: bool,
    user_message: str,
    turns: Sequence[Sequence[Dict[str, Any]]],
    steering: Sequence[Dict[str, Any]] = (),
) -> List[Dict[str, Any]]:
    """The three-block payload: summary, the pinned task, then the verbatim window.

    The original user message is pinned rather than compressible. It is the instruction the whole
    pass exists to satisfy, and a compressor that dropped it would let the model drift onto
    whatever the summary happened to emphasise -- the one thing a bounded context must not lose.

    ``steering`` is where an interruption's correction lands (Phase 33): the user's own words, in the
    high-fidelity block and at the *end* of it, which is the position a course correction wants.
    """
    head: List[Dict[str, Any]] = []
    if dropped:
        head.append({"role": "system", "content": TRUNCATION_NOTICE})
    if summary:
        head.append({"role": "system", "content": SUMMARY_HEADING + summary})
    head.append({"role": "user", "content": user_message})
    body = [message for turn in turns for message in turn]
    return head + body + list(steering)


def _cap_summary(text: str) -> str:
    """Hold the summary to :data:`MAX_SUMMARY_TOKENS`, keeping both ends.

    A *hard* cap rather than the prompt's word count: the recursion means the summary is rewritten
    from the previous summary every time, so an unbounded output would grow the payload on every
    compression and undo the window it exists to protect. Truncating keeps the newest material (the
    tail) and the standing context (the head).
    """
    value = str(text or "").strip()
    if not value or token_budget.count_tokens(value) <= MAX_SUMMARY_TOKENS:
        return value
    # Halve by characters until it fits: ``count_tokens`` is exact, so this converges immediately in
    # practice, and the loop is bounded regardless.
    while value and token_budget.count_tokens(value) > MAX_SUMMARY_TOKENS:
        half = max(1, len(value) // 2)
        value = value[: half // 2] + "\n... [summary truncated] ...\n" + value[-half:]
    return value


def _compact(
    turns: List[List[Dict[str, Any]]],
    summary: str,
    summarizer: Optional[Callable[[str, str], str]],
    dropped: bool = False,
) -> Tuple[str, bool]:
    """Fold everything outside the window into ``summary``. Returns ``(summary, dropped)``.

    **Recursive.** The summariser is handed ``(newly dropped turns, previous summary)`` and returns
    the replacement summary, so the block is *rewritten* rather than appended to and its size does
    not depend on how long the run has been going. The result is then capped by :func:`_cap_summary`,
    because a prompt is a request and the cap is the guarantee.

    A summariser that answers gives a compressed history; one that is absent, fails, or answers with
    nothing leaves the fallback -- drop the turns and tell the model so. Both are bounded, which is
    the property that matters: the payload never grows past the window plus the summary.

    ``dropped`` is **sticky**: once history has been folded away, the model is told so on every later
    payload, not only on the step that happened to cross the threshold.
    """
    if len(turns) <= COMPRESS_AFTER_TURNS:
        return summary, dropped
    older = turns[:-CONTEXT_WINDOW_TURNS]
    del turns[:-CONTEXT_WINDOW_TURNS]
    if summarizer is None:
        return summary, True
    rendered = "\n\n".join(_render_turn(turn) for turn in older)
    try:
        produced = _cap_summary(summarizer(rendered, summary))
    except Exception as error:
        print(
            f"[agent-loop] the context compressor failed: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return summary, True
    if not produced:
        return summary, True
    # A summary *is* the compressed history, so the model has been told: no truncation notice.
    return produced, False


def _emit(emit: Optional[Callable[[Dict[str, Any]], None]], event: Dict[str, Any]) -> None:
    """Publish one structured event. Best effort: the stream is an observer of the run.

    A UI that has gone away must not take the run with it, so a failing emitter is reported once per
    call and the loop continues. The *durable* record is the ledger; this is the live view.
    """
    if emit is None:
        return
    try:
        emit(dict(event))
    except Exception as error:
        print(
            f"[agent-loop] an event could not be published: {type(error).__name__}: {error}",
            file=sys.stderr,
        )


def _gate(
    ledger: Any,
    intent_id: str,
    *,
    emit: Optional[Callable[[Dict[str, Any]], None]],
    pause_timeout: float,
    steering: List[Dict[str, Any]],
) -> None:
    """The liveness, interrupt and steering gate. Called before every call and every tool.

    Three questions in one place, because they are the same question -- *may this run continue?*

    * **aborted** (the intent is no longer ``running`` and not paused either): raise
      :class:`RunAborted`, as it always has;
    * **interrupted** (the user asked for the wheel): *pause*, announce it, and hold for a payload;
      a correction arrives through the ledger, is drained exactly once, and is injected into the
      window as a ``[user]`` message. A hold that outlives :data:`PAUSE_TIMEOUT_SECONDS` is a
      failure -- nothing may wait forever;
    * **running**: continue -- and still take any queued correction. The pause and the resume are
      two writes from another thread, and a fast resume can land *before* this gate ever observes
      the pause; draining on the running path is what keeps a correction from being stranded.
    """
    if ledger is None or not str(intent_id or "").strip():
        return
    status = _status_of(ledger, intent_id)
    if status == PAUSED:
        _await_resume(
            ledger, intent_id, emit=emit, pause_timeout=pause_timeout, steering=steering
        )
        return
    if status != "running":
        raise RunAborted(_abort_detail(ledger, intent_id))
    _take_steering(ledger, intent_id, emit=emit, steering=steering)


def _take_steering(
    ledger: Any,
    intent_id: str,
    *,
    emit: Optional[Callable[[Dict[str, Any]], None]],
    steering: List[Dict[str, Any]],
) -> int:
    """Drain the queued corrections into the window as ``[user]`` messages. Returns the count.

    The steering wheel: the user's own words, appended to the high-fidelity block. Drained rather
    than peeked, so a correction is delivered exactly once even if the run is paused again.
    """
    corrections = _drain_input(ledger, intent_id)
    for correction in corrections:
        steering.append({"role": "user", "content": correction})
        _emit(emit, {"type": "intent_steered", "intent_id": intent_id,
                     "correction": correction})
    return len(corrections)


def _status_of(ledger: Any, intent_id: str) -> str:
    """The intent's status, or ``"running"`` when the ledger cannot say. Never raises."""
    try:
        return str(ledger.status_of(intent_id) or "")
    except Exception as error:
        print(
            f"[agent-loop] the intent status could not be read: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return "running"


def _await_resume(
    ledger: Any,
    intent_id: str,
    *,
    emit: Optional[Callable[[Dict[str, Any]], None]],
    pause_timeout: float,
    steering: List[Dict[str, Any]],
) -> None:
    """Hold the run while the user decides, then take their correction. Raises on timeout/abort."""
    import time

    _emit(emit, {"type": "intent_paused", "intent_id": intent_id,
                 "message": "paused for user input"})
    deadline = time.monotonic() + max(0.0, float(pause_timeout))
    while True:
        status = _status_of(ledger, intent_id)
        if status == "running":
            break
        if status != PAUSED:
            raise RunAborted(_abort_detail(ledger, intent_id))
        if time.monotonic() >= deadline:
            raise RunAborted(
                f"the intent was paused for user input and nothing arrived within "
                f"{int(pause_timeout)}s"
            )
        time.sleep(PAUSE_POLL_SECONDS)
    corrections = _take_steering(ledger, intent_id, emit=emit, steering=steering)
    _emit(emit, {"type": "intent_resumed", "intent_id": intent_id,
                 "corrections": corrections})


def _drain_input(ledger: Any, intent_id: str) -> List[str]:
    """Take the queued corrections, or ``[]``. Never raises."""
    try:
        return list(ledger.drain_input(intent_id))
    except Exception as error:
        print(
            f"[agent-loop] the steering payload could not be read: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return []


def _spent(ledger: Any, intent_id: str) -> int:
    """Tokens this intent has spent so far, from the ledger. ``0`` when there is no ledger."""
    if ledger is None or not str(intent_id or "").strip():
        return 0
    try:
        prompt, completion = ledger.token_totals(intent_id)
    except Exception as error:  # the count is an observer; it must not take the loop down
        print(
            f"[agent-loop] the token ledger could not be read: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 0
    return int(prompt) + int(completion)


def _record_spend(ledger: Any, intent_id: str, prompt: int, completion: int) -> None:
    """Add this call's cost to the intent's total. Best effort, and never fatal."""
    if ledger is None or not str(intent_id or "").strip():
        return
    try:
        ledger.add_tokens(intent_id, prompt=prompt, completion=completion)
    except Exception as error:
        print(
            f"[agent-loop] the token spend could not be recorded: {type(error).__name__}: {error}",
            file=sys.stderr,
        )


def _check_budget(ledger: Any, intent_id: str, limit: Optional[int]) -> None:
    """Raise when the intent has spent its budget. Checked before every call and every tool."""
    spent = _spent(ledger, intent_id)
    if spent and token_budget.budget_exceeded(spent, limit=limit):
        raise token_budget.TokenBudgetExceeded(
            f"this intent has spent {spent} tokens, at or past its budget of "
            f"{int(limit if limit is not None else token_budget.max_intent_tokens())}"
        )


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
    summarizer: Optional[Callable[[str, str], str]] = None,
    max_tokens: Optional[int] = None,
    emit: Optional[Callable[[Dict[str, Any]], None]] = None,
    pause_timeout: float = PAUSE_TIMEOUT_SECONDS,
) -> Tuple[str, List[Dict[str, Any]]]:
    """Drive ``completer`` through the tools bound for ``role``.

    Returns ``(final_text, transcript)``. The transcript is the evidence: one entry per tool
    call, with the arguments the model produced and the result the server returned.

    ``completer`` is called as ``completer(system=..., messages=..., tools=...)`` and must
    return an object with ``text`` and (optionally) ``tool_calls`` -- a list of
    ``{"name": ..., "arguments": {...}}``. A turn that returns no tool calls ends the loop.

    A tool that raises is reported into the transcript and back to the model as its result,
    rather than aborting: a refused edit is information the model needs, not a crash.

    ``intent_id``/``ledger`` are the correlation, the liveness gate and the token ledger. They are
    injected rather than resolved here, because this loop also runs in a swarm *child* process where
    the plan directory is not the parent's. Each gate applies only when its inputs are present, so a
    bare unit test still gets the step ceiling.

    ``emit`` is the live stream (Phase 33): structured ``agent_thought``,
    ``tool_execution_start``/``_complete`` and ``token_budget_update`` events as they happen. It is
    an observer -- the durable record is the ledger -- so a failing emitter never stops a run.
    """
    tools = session.get_bound_tools(role) if session is not None else []
    by_name = {tool.name: tool for tool in tools}
    schemas = _tool_schemas(tools)

    turns: List[List[Dict[str, Any]]] = []
    steering: List[Dict[str, Any]] = []
    summary = ""
    dropped = False
    transcript: List[Dict[str, Any]] = []

    for _step in range(max_steps):
        _gate(ledger, intent_id, emit=emit, pause_timeout=pause_timeout, steering=steering)
        _check_budget(ledger, intent_id, max_tokens)

        payload = _payload(
            summary=summary, dropped=dropped, user_message=user_message, turns=turns,
            steering=steering,
        )
        prompt_tokens = token_budget.count_tokens(
            system_prompt + "\n" + "\n".join(str(m.get("content") or "") for m in payload)
        )
        completion = completer(system=system_prompt, messages=payload, tools=schemas)
        text = str(getattr(completion, "text", "") or "")
        calls = list(getattr(completion, "tool_calls", None) or [])
        completion_tokens = token_budget.count_tokens(text)
        _record_spend(ledger, intent_id, prompt_tokens, completion_tokens)
        _emit(emit, {
            "type": "agent_thought", "intent_id": intent_id, "step": _step + 1,
            "text": text, "tool_calls": [str((c or {}).get("name") or "") for c in calls],
        })
        _emit(emit, {
            "type": "token_budget_update", "intent_id": intent_id,
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "spent": _spent(ledger, intent_id),
            "limit": int(max_tokens or token_budget.max_intent_tokens()),
        })

        if not calls:
            return text, transcript

        turn: List[Dict[str, Any]] = [
            {"role": "assistant", "content": text, "tool_calls": calls}
        ]
        for call in calls:
            # Re-checked per call, not only per step: a tool *mutates*, and an abort, an interruption
            # or a budget that landed while the model was thinking must not be overtaken by the write
            # it planned.
            _gate(ledger, intent_id, emit=emit, pause_timeout=pause_timeout, steering=steering)
            _check_budget(ledger, intent_id, max_tokens)
            name = str((call or {}).get("name") or "")
            arguments = dict((call or {}).get("arguments") or {})
            tool = by_name.get(name)
            _emit(emit, {
                "type": "tool_execution_start", "intent_id": intent_id,
                "tool": name, "arguments": arguments,
            })
            if tool is None:
                result = f"Error: no tool named {name!r} is bound for role {role!r}."
            else:
                try:
                    # A bound tool: its body reads/writes through the live MCP session.
                    result = str(tool.invoke(arguments))
                except Exception as error:
                    result = f"Error: {type(error).__name__}: {error}"
            _emit(emit, {
                "type": "tool_execution_complete", "intent_id": intent_id,
                "tool": name, "result": result,
            })
            transcript.append({"tool": name, "arguments": arguments, "result": result})
            turn.append({"role": "tool", "name": name, "content": result})
        turns.append(turn)
        summary, dropped = _compact(turns, summary, summarizer, dropped)

    _record_step_limit(ledger, intent_id, role, max_steps)
    raise AgentStepLimitExceeded(STEP_LIMIT_MESSAGE)


def _abort_detail(ledger: Any, intent_id: str) -> str:
    """Why the gate closed, naming the ledger it read.

    A run aborted by a *misrouted* ledger -- a child reading a different database than its parent --
    looks exactly like a run the user stopped, and the two need opposite fixes. Naming the file is
    what makes that distinguishable in a log.
    """
    return f"{ABORTED_MESSAGE} (intent {intent_id!r} is not running in {getattr(ledger, 'path', '?')})"


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
