"""The Architect's own System 2 answer, and the model's thought process as UI text.

The administrative bypass means no *Coder* is spawned for a plan edit, a codebase analysis
or a recommendation -- the Architect resolves those alone, at zero Coder tokens. It does
not mean the answer has to be a script: those three intents are exactly the ones the
Architect is positioned to reason about (this plan, this workspace), so when a provider is
configured it answers them itself.

Two things are deliberate:

* the seam degrades to *nothing*. With System 2 off, unreachable or empty, ``used_llm`` is
  False and every caller keeps the deterministic text it emitted before, byte for byte, so
  the pinned offline event streams and a keyless checkout are unaffected;
* the thought process is capped before it becomes UI text. A reasoning model's chain of
  thought can run to thousands of lines, and ``stream_text`` emits one event per line --
  buffer nothing, cap everything.
"""

from dataclasses import dataclass
from typing import Any, Callable, List, Optional

# The answer is a short architectural reply, so it is explicitly out of prose mode: the
# caller's structured fields (proposals, deliverables) stay their own.
ARCHITECT_DIRECTIVE = (
    "\n\n### Administrative Answer Mode\n"
    "You are answering one request directly as the Lead Software Architect. No tools are "
    "available and no Coder will be spawned. Be specific to the state you are shown and "
    "concise -- at most 10 short lines. Do not emit tool calls, function syntax or JSON."
)

@dataclass
class ArchitectAnswer:
    """One Architect reply, and how it was obtained.

    ``used_llm`` is the switch every caller reads: False means "System 2 gave me nothing,
    fall back to the deterministic text".
    """

    text: str = ""
    reasoning: str = ""
    used_llm: bool = False
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""

    def usage_line(self) -> str:
        """A one-line cost summary, for the Architect's summary panel."""
        return (
            f"System 2 call: {self.model} \u00b7 "
            f"{self.prompt_tokens:,} in / {self.completion_tokens:,} out tokens"
        )

    def answer_lines(self) -> List[str]:
        """The answer as non-empty lines, with list markers stripped."""
        lines = []
        for line in (self.text or "").splitlines():
            cleaned = line.strip().lstrip("-*\u2022 ").strip()
            if cleaned:
                lines.append(cleaned)
        return lines


def build_system_prompt(plan_file: str) -> str:
    """The Architect's own system prompt, in administrative answer mode.

    Imported lazily and guarded: ``agents`` is imported by ``orchestration``, so a
    module-level import would close a cycle, and the prompt is a nicety -- a bare
    fallback still answers correctly if the agent module cannot be reached.
    """
    try:
        from agents.architect import ARCHITECT_SYSTEM_PROMPT
        from orchestration.agent_catalog import resolve_prompt_variables

        return resolve_prompt_variables(ARCHITECT_SYSTEM_PROMPT, plan_file) + ARCHITECT_DIRECTIVE
    except Exception:
        return "You are the Lead Software Architect." + ARCHITECT_DIRECTIVE


def build_instruction(prompt: str, context_text: str = "") -> str:
    """The user message: the state the Architect is shown, then the ask."""
    parts = []
    if context_text:
        parts.append("Current state:\n\n" + context_text)
    parts.append(prompt)
    return "\n\n".join(parts)


def architect_answer(
    *,
    prompt: str,
    context_text: str = "",
    plan_file: str = "PLAN.md",
    completer: Optional[Callable[..., Any]] = None,
) -> ArchitectAnswer:
    """One Architect reply from System 2, or an empty answer when it cannot be made.

    Never raises and never returns ``None`` -- the caller's fallback is the scripted text it
    already has, so the failure mode is "no answer", not "broken workflow".
    """
    from agents.model_routing import architect_model
    from orchestration import system2

    if not system2.is_enabled():
        return ArchitectAnswer()

    if completer is None:
        completer = system2.complete

    model = architect_model()
    instruction = build_instruction(prompt, context_text)
    try:
        completion = completer(
            model=model,
            system=build_system_prompt(plan_file),
            user=instruction,
        )
    except Exception as exc:  # the completer contract is "return None"; stay safe anyway
        return ArchitectAnswer(error=str(exc))

    if completion is None:
        return ArchitectAnswer(error="no completion")

    text = (getattr(completion, "text", "") or "").strip()
    reasoning = (getattr(completion, "reasoning", "") or "").strip()
    if not text and not reasoning:
        return ArchitectAnswer(error="empty completion")

    return ArchitectAnswer(
        text=text,
        reasoning=reasoning,
        used_llm=True,
        model=getattr(completion, "model", "") or model,
        prompt_tokens=getattr(completion, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(completion, "completion_tokens", 0) or 0,
    )


MAX_THOUGHT_LINES = 40
MAX_THOUGHT_CHARS = 4000

# The label the card shows above the model's own deliberation. Kept here so the Architect
# and the Coder paths announce it identically.
THOUGHT_LABEL = "> \U0001F9E0 Thought process:"


def stream_thought(stream_text: Callable[..., None], agent: str, reasoning: str) -> None:
    """Streams a model's chain of thought into an agent's card, capped and labelled.

    Emitted as ``log_type="reasoning"`` so the pane renders it distinctly from the
    workflow's own narration: this is the model talking, not the orchestrator.
    """
    stream_text(agent, THOUGHT_LABEL, log_type="reasoning", delay=0.0)
    for line in thought_lines(reasoning):
        stream_text(agent, f">   {line}", log_type="reasoning", delay=0.0)


def thought_lines(reasoning: str) -> List[str]:
    """The reasoning to show in the agent card: quoted, capped, and marked when cut.

    Every line becomes one event, so the cap is what keeps a long chain of thought from
    flooding the wire and the pane. Returns a single explanatory line when the model
    exposed no reasoning at all, rather than an empty list the caller has to special-case.
    """
    lines = [line.rstrip() for line in (reasoning or "").splitlines() if line.strip()]
    if not lines:
        return ["(the model returned no visible reasoning)"]

    out: List[str] = []
    used = 0
    for line in lines:
        if len(out) >= MAX_THOUGHT_LINES or used + len(line) > MAX_THOUGHT_CHARS:
            out.append("\u2026 (reasoning truncated)")
            break
        out.append(line)
        used += len(line)
    return out
