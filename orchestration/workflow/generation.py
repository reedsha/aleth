"""Turning a plan slice into one real Coder deliverable.

This is where System 2 first writes something: the ``next_step`` branch used to hand
the Coder a canned module from ``templates.py``. Now the template still supplies the
*shape* -- the filenames and the paired test -- while the main deliverable's contents
come from a real model call, so the app makes an actual request instead of replaying a
script.

The prompt is assembled from :func:`slice_task_context`, which is the point of the
exercise: the Coder is told what concerns its one task, not handed the whole plan and
its derived ``plan.json`` on every turn. That slice was measured at 12,743 -> 409
tokens against the real API, so this call is where the saving is realised rather than
just measured.

The seam degrades rather than fails. When System 2 is not configured or enabled, or a
call returns nothing usable, the deliverable falls back to the template's own code and
the workflow proceeds exactly as it did before. That keeps a keyless checkout working
and keeps the pinned event streams meaningful.
"""

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from agents.model_routing import coder_model
from orchestration.workflow.context import slice_task_context

# A model asked for a file often wraps it in a fence. Strip a single surrounding
# ```lang ... ``` pair so what lands on disk is the file itself.
_FENCE_RE = re.compile(r"^\s*```[^\n]*\n(?P<body>.*?)\n?```\s*$", re.DOTALL)

# The Coder's own system prompt tells it to inspect the plan and write files with
# ``write_file``. This call has no tools and asks for the file body directly, so without
# this rider the model answers with the tool call it was told to make instead of code.
_DIRECT_MODE_DIRECTIVE = (
    "\n\n### Direct File Generation Mode\n"
    "You are answering a single request with NO tools available. Do not emit tool calls,"
    " function syntax or JSON. Return exactly the complete contents of the requested"
    " file, and nothing else."
)

# Output must be a whole file, not a diff or an explanation.
_LANGUAGE_BY_SUFFIX = {
    ".py": "Python",
    ".html": "HTML",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".css": "CSS",
    ".md": "Markdown",
    ".json": "JSON",
}


@dataclass
class GeneratedFile:
    """The contents to write, and how they were obtained.

    ``used_llm`` is what tells the caller whether a real request happened, so the UI can
    report the token cost only when there is one, and the offline output stays
    byte-identical to what the templates produced before this module existed.
    """

    code: str
    used_llm: bool = False
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str = ""

    def usage_line(self) -> str:
        """A one-line summary of the call, for the Coder's summary panel."""
        return (
            f"System 2 call: {self.model} \u00b7 "
            f"{self.prompt_tokens:,} in / {self.completion_tokens:,} out tokens"
        )


def language_for(filename: str) -> str:
    """The language name for a filename's extension, or ``"text"`` when unknown."""
    lowered = (filename or "").lower()
    for suffix, language in _LANGUAGE_BY_SUFFIX.items():
        if lowered.endswith(suffix):
            return language
    return "text"


def strip_code_fence(text: str) -> str:
    """Removes one surrounding markdown fence, leaving the file's body.

    A response with no fence is returned unchanged (trailing whitespace trimmed), so the
    common case costs nothing and a nested fence inside the body is preserved.
    """
    match = _FENCE_RE.match(text or "")
    if match:
        return match.group("body").strip()
    return (text or "").strip()


def build_instruction(context_text: str, filename: str, task_title: str) -> str:
    """The user message for one deliverable: the task's slice, then the ask.

    The filename is stated explicitly rather than left to the model, because the
    workflow records it against the task and the paired test imports it by name.
    """
    language = language_for(filename)
    parts = [f"Target file: `{filename}` ({language})"]
    if context_text:
        parts.append("Task context from the plan:\n\n" + context_text)
    else:
        parts.append(f"Task: {task_title}")
    parts.append(
        f"Write the complete contents of `{filename}` that accomplish the task above.\n"
        "Return ONLY the file's contents, with no prose, commentary or markdown fences."
    )
    return "\n\n".join(parts)


def build_system_prompt(coder_id: str, plan_file: str) -> str:
    """The chosen Coder's system prompt, in direct file-generation mode.

    Imported here rather than at module scope: ``agents`` is imported by
    ``orchestration``, and this module is imported by ``orchestration.workflow``, so a
    module-level agent import would close an import cycle.

    The role prompt is reused so the identity and quality bar are the Coder's own; the
    directive is appended because this call has no tools, and the role prompt's first
    instruction is otherwise to call one.
    """
    from agents import coders
    from orchestration.agent_catalog import resolve_prompt_variables

    spec = coders.coder_deep if coder_id == "coder-deep" else coders.coder_standard
    return resolve_prompt_variables(spec["system_prompt"], plan_file) + _DIRECT_MODE_DIRECTIVE


def generate_deliverable(
    *,
    coder_id: str,
    task: Mapping[str, Any],
    plan: Mapping[str, Any],
    deliverable: Any,
    plan_file: str,
    completer: Optional[Callable[..., Any]] = None,
    on_request: Optional[Callable[[str, int], None]] = None,
) -> GeneratedFile:
    """A deliverable's contents, from System 2 when available, else from the template.

    ``deliverable`` is the ``templates`` NamedTuple supplying the filenames and the
    paired test; ``completer`` defaults to :func:`orchestration.system2.complete` and is
    injectable so the live path is testable without a network.

    ``on_request`` is called as ``(model, prompt_chars)`` immediately before the request,
    and only when a request is actually made -- the caller uses it to narrate a wait that
    can last seconds. The offline path never triggers it, so its output is unchanged.
    """
    from orchestration import system2

    fallback = GeneratedFile(code=deliverable.code)
    if not system2.is_enabled():
        return fallback

    if completer is None:
        completer = system2.complete

    task_title = str(task.get("title") or "")
    task_key = str(task.get("id") or task_title)
    context_text = slice_task_context(plan, task_key)
    instruction = build_instruction(context_text, deliverable.filename, task_title)
    model = coder_model(coder_id)

    if on_request is not None:
        on_request(model, len(instruction))

    try:
        completion = completer(
            model=model,
            system=build_system_prompt(coder_id, plan_file),
            user=instruction,
        )
    except Exception as exc:  # the completer contract is "return None"; stay safe anyway
        return GeneratedFile(code=deliverable.code, error=str(exc))

    if completion is None:
        return GeneratedFile(code=deliverable.code, error="no completion")

    if getattr(completion, "finish_reason", "") == "length":
        # A partial answer is a partial file: it looks like real output and is quietly
        # broken, so the template is the safer deliverable. The error is reported so the
        # budget can be seen to be the cause.
        return GeneratedFile(code=deliverable.code, error="truncated completion")

    code = strip_code_fence(getattr(completion, "text", "") or "")
    if not code:
        return GeneratedFile(code=deliverable.code, error="empty completion")

    return GeneratedFile(
        code=code,
        used_llm=True,
        model=getattr(completion, "model", ""),
        prompt_tokens=getattr(completion, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(completion, "completion_tokens", 0) or 0,
    )
