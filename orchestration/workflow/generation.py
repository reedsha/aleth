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

import posixpath
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from agents.model_routing import coder_model
from orchestration.workflow.context import slice_task_context

# A deliverable is written where the *task* says it belongs, not where a template happens
# to put it. Tasks name their file in backticks -- "Upgrade AST parser
# (`tools/plan_parser.py`)" -- which is precise, in the author's own words, and free to
# read. Requiring the backticks keeps prose like "the ``runner.resolve_intent`` tag" from
# being mistaken for a path.
_TARGET_PATH_RE = re.compile(
    r"`([\w][\w./\\-]*\.(?:py|pyi|js|mjs|cjs|jsx|ts|tsx|css|scss|html|htm|json|sql|sh"
    r"|yml|yaml|toml|go|rs|rb|java|kt|swift|vue|svelte))`"
)


def task_target_path(task: Mapping[str, Any]) -> Optional[str]:
    """The workspace-relative file a task's deliverable belongs in, or ``None``.

    Consulted in order of how deliberate the statement is: the backticked path in the
    title, then one in the task's notes, then a declared deliverable. The title wins
    because ``files`` is also what the workflow *records* once a task has run -- so a task
    executed before carries the path that run wrote, wrong or not. The author's title is
    the intent; the record is a consequence.

    ``None`` means the task does not name a file, and the caller should keep whatever the
    template chose.
    """
    for text in [str(task.get("title") or "")] + [str(d) for d in (task.get("details") or [])]:
        found = path_in_text(text)
        if found:
            return found

    declared = [str(f).strip().replace("\\", "/") for f in (task.get("files") or []) if str(f).strip()]
    return declared[0] if declared else None


def path_in_text(text: str) -> Optional[str]:
    """The first backticked source path in ``text``, normalised, or ``None``.

    Shared by the plan-task target and by the two free-form paths -- a bug report and a
    custom directive -- which have no task to read a title from.
    """
    match = _TARGET_PATH_RE.search(str(text or ""))
    return match.group(1).replace("\\", "/").strip("/") if match else None


def paired_test_path(path: str) -> str:
    """The test file that belongs beside a deliverable: ``a/b.py`` -> ``a/test_b.py``."""
    directory, base = posixpath.split(path)
    stem = base.rsplit(".", 1)[0] or base
    name = f"test_{stem}.py"
    return f"{directory}/{name}" if directory else name

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
    # The model's chain of thought, when the provider exposes one. It is *not* part of the
    # deliverable -- it is what the agent's card shows so the reasoning behind the file can
    # be read rather than guessed at.
    reasoning: str = ""
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


def generate_code(
    *,
    coder_id: str,
    filename: str,
    context_text: str,
    task_title: str,
    plan_file: str,
    fallback_code: str,
    completer: Optional[Callable[..., Any]] = None,
    on_request: Optional[Callable[[str, int], None]] = None,
) -> GeneratedFile:
    """One file's contents, from System 2 when available, else the caller's fallback.

    The general seam behind every action that writes code. ``context_text`` is whatever
    the Coder needs to see -- a plan slice for a roadmap task, a bug report and the current
    file for a patch, a directive for a custom request -- so this function stays about the
    call rather than about where its input came from.

    ``fallback_code`` is what is written when System 2 is off, unreachable, or did not
    return a whole file. That is what keeps the offline behaviour of every action exactly
    as it was, byte for byte.

    ``completer`` defaults to :func:`orchestration.system2.complete` and is injectable so
    the live path is testable without a network. ``on_request`` is called as
    ``(model, prompt_chars)`` immediately before a request is actually made, so a caller
    can narrate a wait that can last seconds; the offline path never triggers it.
    """
    from orchestration import system2

    if not system2.is_enabled():
        return GeneratedFile(code=fallback_code)

    if completer is None:
        completer = system2.complete

    instruction = build_instruction(context_text, filename, task_title)
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
        return GeneratedFile(code=fallback_code, error=str(exc))

    if completion is None:
        return GeneratedFile(code=fallback_code, error="no completion")

    if getattr(completion, "finish_reason", "") == "length":
        # A partial answer is a partial file: it looks like real output and is quietly
        # broken, so the template is the safer deliverable. The error is reported so the
        # budget can be seen to be the cause.
        return GeneratedFile(code=fallback_code, error="truncated completion")

    code = strip_code_fence(getattr(completion, "text", "") or "")
    if not code:
        return GeneratedFile(code=fallback_code, error="empty completion")

    return GeneratedFile(
        code=code,
        used_llm=True,
        model=getattr(completion, "model", ""),
        prompt_tokens=getattr(completion, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(completion, "completion_tokens", 0) or 0,
        reasoning=getattr(completion, "reasoning", "") or "",
    )


def generate_deliverable(
    *,
    coder_id: str,
    task: Mapping[str, Any],
    plan: Mapping[str, Any],
    deliverable: Any,
    plan_file: str,
    filename: Optional[str] = None,
    completer: Optional[Callable[..., Any]] = None,
    on_request: Optional[Callable[[str, int], None]] = None,
) -> GeneratedFile:
    """A roadmap task's deliverable: the plan slice, handed to :func:`generate_code`.

    ``deliverable`` is the ``templates`` NamedTuple whose source is the fallback;
    ``filename`` is where the deliverable is actually written (``task_target_path``),
    which can differ from the template's own name.
    """
    task_title = str(task.get("title") or "")
    task_key = str(task.get("id") or task_title)
    return generate_code(
        coder_id=coder_id,
        filename=filename or deliverable.filename,
        context_text=slice_task_context(plan, task_key),
        task_title=task_title,
        plan_file=plan_file,
        fallback_code=deliverable.code,
        completer=completer,
        on_request=on_request,
    )
