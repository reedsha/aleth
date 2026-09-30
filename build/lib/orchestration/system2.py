"""System 2: the one place a real model request is made.

System 1 (``agents/laya.py``) decides without spending a token; System 2 is the
opposite -- an OpenAI-compatible chat completion whose answer *is* the product. This
module owns the provider seam for it: where the key and base URL come from, how a
route name in ``agents/model_routing.py`` (``openai:policy/coder-deep-test``) becomes
the model name the API expects, and how a failed call becomes a value the workflow
can fall back on instead of an exception thrown into its middle.

Three things are deliberate:

* the route's ``provider:`` prefix is stripped here, so a route may name its transport
  while the model name is what the client receives;
* a missing key or base URL is an ordinary "not configured" answer, not an error --
  a bare checkout with no ``.env`` must still run offline;
* ``complete`` returns ``None`` rather than raising. A Coder spawn that cannot reach
  the provider should fall back to what it would have done before the call existed,
  not take the whole workflow down with it.

Import-light on purpose: ``os`` and typing at module scope, and the ``openai`` client
built lazily inside the call. ``agents`` is imported by ``orchestration``, which is
imported by the entrypoints, so a module here that pulled a heavy dependency at import
time would decide the app's start-up cost.
"""

import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# The opt-out. Unset means "enabled once configured", so a checkout with a ``.env``
# starts making real calls, while the test suite sets this to ``0`` to pin the offline
# path deterministically instead of depending on the developer's shell.
ENV_ENABLED = "ALETH_SYSTEM2"

ENV_API_KEY = "OPENAI_API_KEY"
ENV_BASE_URL = "OPENAI_BASE_URL"

# Values of ``ALETH_SYSTEM2`` that turn System 2 off, beyond the bare "0".
DISABLED_VALUES = frozenset({"0", "false", "off", "no"})

# Route names carry their transport as a ``provider:`` prefix (``openai:policy/x``).
# The API wants only the model name.
PROVIDER_PREFIXES = ("openai:",)

# A local endpoint may legitimately have no key, and the OpenAI SDK refuses to build a client
# without one. The placeholder is used instead of the environment's key on purpose: sending a
# global credential to a tier that declared its own endpoint is the bug this module exists to stop.
LOCAL_ENDPOINT_KEY = "not-needed"


@dataclass
class Completion:
    """One successful call's answer, with the usage the provider billed for it."""

    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # The provider's stop reason. ``"length"`` means the output budget ran out and the
    # answer is a *partial* file -- the caller must not write it as if it were complete.
    finish_reason: str = ""
    # A reasoning model's chain of thought, when it exposes one. Empty for a plain chat
    # model; the workflow surfaces it in the agent's card so the reasoning behind an answer
    # can be read rather than guessed at.
    reasoning: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def usage_line(self) -> str:
        """A one-line summary for the UI, so a saving can be seen rather than assumed."""
        return (
            f"{self.model} \u00b7 {self.prompt_tokens:,} in / "
            f"{self.completion_tokens:,} out tokens"
        )


def route_model(route: str) -> str:
    """The model name the API expects, from a role's route name.

    ``"openai:policy/coder-deep-test"`` becomes ``"policy/coder-deep-test"``. A route
    without a known provider prefix is returned unchanged, so a plain model name works
    just as well.
    """
    for prefix in PROVIDER_PREFIXES:
        if route.startswith(prefix):
            return route[len(prefix):]
    return route


def provider_config(
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[Dict[str, str]]:
    """The endpoint to call: the tier's own when one is named, else the environment's.

    An explicit ``base_url`` wins outright, and that is the whole point of the bootloader: a tier
    that declares its endpoint must be called *there*, with *that* credential. Falling back to a
    global environment variable would silently send the request somewhere the configuration never
    named -- a validated tier whose key was never used is worse than no tier at all, because it
    looks like it works.

    A named endpoint with no key is legitimate (a local model server has none) and the SDK insists
    on a key, so it gets the conventional placeholder rather than the environment's key, which
    would send the wrong credential to the wrong place. With no endpoint named, the environment is
    read as it always was, so an unconfigured checkout still runs offline.
    """
    declared_base = str(base_url or "").strip()
    if declared_base:
        declared_key = str(api_key or "").strip()
        return {
            "api_key": declared_key or LOCAL_ENDPOINT_KEY,
            "base_url": declared_base.rstrip("/"),
        }

    key = (os.environ.get(ENV_API_KEY) or "").strip()
    base = (os.environ.get(ENV_BASE_URL) or "").strip()
    if not key or not base:
        return None
    return {"api_key": key, "base_url": base.rstrip("/")}


def is_configured(*, base_url: Optional[str] = None, api_key: Optional[str] = None) -> bool:
    """Whether an endpoint is available at all -- the tier's, or the environment's."""
    return provider_config(base_url=base_url, api_key=api_key) is not None


def is_enabled(*, base_url: Optional[str] = None, api_key: Optional[str] = None) -> bool:
    """Whether a real call should be attempted right now.

    Enabled by default *once configured*, and switched off by ``ALETH_SYSTEM2``
    (``0`` / ``false`` / ``off`` / ``no``). Tests set the flag so the suite pins the
    offline path regardless of the shell it runs in.

    A routed tier counts as configured: the dispatcher hands the worker the endpoint the
    bootloader proved, so a deployment driven by a config *file* plans on it even when no global
    ``OPENAI_*`` variable is set.
    """
    flag = (os.environ.get(ENV_ENABLED) or "").strip().lower()
    if flag in DISABLED_VALUES:
        return False
    return is_configured(base_url=base_url, api_key=api_key)


def compose_system_prompt(system: str, skills: str = "") -> str:
    """The system prompt the provider is given: the retrieved playbooks, then the role prompt.

    This is where a skill becomes part of a request, and it happens **before** the client payload
    is built, so the model is handed one composed prompt rather than the transport silently
    rewriting messages afterwards. Skills go first because they are procedural guidance for the
    tools the model was handed, and a model reads the top of its system prompt as the standing
    instruction it must satisfy.

    With no skills the prompt is returned unchanged -- an empty block must not add a stray
    heading or a horizontal rule to every request.
    """
    body = str(system or "")
    block = str(skills or "").strip()
    if not block:
        return body
    from orchestration.retriever import SKILL_HEADER

    return f"{SKILL_HEADER}\n\n{block}\n\n---\n\n{body}"


def complete(
    *,
    model: str,
    system: str,
    user: str,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    max_tokens: int = 16384,
    timeout: float = 180.0,
    client: Any = None,
    images: Optional[List[str]] = None,
    skills: str = "",
) -> Optional[Completion]:
    """One chat completion, or ``None`` when it cannot be made.

    ``client`` is injectable so the seam can be exercised without a network; production
    callers leave it unset and the endpoint is the tier's (``base_url``/``api_key``) or, when
    none is named, the environment's.

    ``images`` are data URLs (``data:image/png;base64,...``) attached to the user turn as
    OpenAI-compatible ``image_url`` parts, for a vision-capable *model*. With none, the
    message is the plain string it has always been -- so the text-only path is unchanged.

    ``skills`` is the Markdown a node's capabilities selected
    (``orchestration.retriever.skills_for_capabilities``); it is prepended to ``system`` before
    the payload is built (:func:`compose_system_prompt`). The transport does not look skills up
    -- resolving them is the caller's decision, and this layer only composes what it is given.

    Returns ``None`` -- and reports one line to stderr -- for every failure mode: no
    configuration, a transport error, or an empty answer. The caller decides what to do
    without it; nothing here raises.
    """
    if client is None:
        config = provider_config(base_url=base_url, api_key=api_key)
        if config is None:
            return None
        try:
            from openai import OpenAI
        except Exception as exc:  # pragma: no cover - import failure is environmental
            print(f"[System2] openai package unavailable: {exc}", file=sys.stderr)
            return None
        client = OpenAI(
            api_key=config["api_key"], base_url=config["base_url"], timeout=timeout
        )

    # A vision request carries the text plus one ``image_url`` part per image. The URLs are
    # data URLs supplied by the caller, so nothing here knows where the bytes came from; with
    # no images the content stays a plain string, which is what keeps the text path identical.
    user_content: Any = user
    if images:
        user_content = [{"type": "text", "text": user}] + [
            {"type": "image_url", "image_url": {"url": url}} for url in images
        ]

    try:
        response = client.chat.completions.create(
            model=route_model(model),
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": compose_system_prompt(system, skills)},
                {"role": "user", "content": user_content},
            ],
        )
    except Exception as exc:
        print(f"[System2] call to {model} failed: {exc}", file=sys.stderr)
        return None

    return _completion_from(response, model)


@dataclass
class ToolCompletion:
    """One tool-capable call's answer: the text, plus any tool calls the model asked for."""

    text: str = ""
    model: str = ""
    # ``[{"name": ..., "arguments": {...}}, ...]`` -- already parsed out of the provider's
    # JSON-string arguments, so a caller never re-parses a nested encoding.
    tool_calls: List[Dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.tool_calls is None:
            self.tool_calls = []


def _tool_calls_from(message: Any) -> List[Dict[str, Any]]:
    """The tool calls in a message, with their arguments decoded.

    OpenAI-compatible providers send each call's arguments as a JSON *string*; a malformed or
    absent payload becomes empty arguments rather than an exception, because a model that
    emits a broken call should get the error back as a tool result, not crash the run.
    """
    calls = []
    for raw in (getattr(message, "tool_calls", None) or []):
        function = getattr(raw, "function", None)
        name = str(getattr(function, "name", "") or "")
        if not name:
            continue
        arguments = getattr(function, "arguments", None)
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments or "{}")
            except ValueError:
                parsed = {}
        else:
            parsed = arguments or {}
        calls.append({"name": name, "arguments": parsed if isinstance(parsed, dict) else {}})
    return calls


def complete_with_tools(
    *,
    model: str,
    system: str,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    max_tokens: int = 16384,
    timeout: float = 180.0,
    client: Any = None,
    skills: str = "",
) -> ToolCompletion:
    """One tool-capable chat completion.

    ``model`` is required and used verbatim. There is deliberately **no** fallback to a default
    route: a caller that cannot name the model it wants does not get to run one, because a silent
    default here would be a routing decision made by the transport layer -- the exact second
    authority Phase 7 exists to remove. A caller with no route passes none and gets an empty
    completion, which is an honest "nothing ran" rather than a guess.

    ``base_url``/``api_key`` are the **routed tier's** endpoint, threaded down from the
    bootloader's configuration through the dispatch descriptor. When they are given they are used
    as declared; when they are not, the environment is read, so the pre-bootloader paths are
    unchanged.

    Never returns ``None``: a missing provider or a failed call answers with no text and no
    tool calls, which ends an agent loop cleanly instead of throwing into the workflow. A
    provider that does not accept ``tools`` is the same shape -- the loop simply never
    receives a call and degrades to the single-shot behaviour.

    ``skills`` is the Markdown a node's capabilities selected; it is prepended to ``system``
    before the payload is built (:func:`compose_system_prompt`), so the tool-using loop and the
    single-shot call present the model with the same standing instructions.
    """
    if not model:
        return ToolCompletion()

    if client is None:
        config = provider_config(base_url=base_url, api_key=api_key)
        if config is None:
            return ToolCompletion(model=model)
        try:
            from openai import OpenAI
        except Exception as exc:  # pragma: no cover - import failure is environmental
            print(f"[System2] openai package unavailable: {exc}", file=sys.stderr)
            return ToolCompletion(model=model)
        client = OpenAI(
            api_key=config["api_key"], base_url=config["base_url"], timeout=timeout
        )

    request: Dict[str, Any] = {
        "model": route_model(model),
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": compose_system_prompt(system, skills)}]
        + list(messages),
    }
    if tools:
        request["tools"] = tools

    try:
        response = client.chat.completions.create(**request)
    except Exception as exc:
        print(f"[System2] tool call to {model} failed: {exc}", file=sys.stderr)
        return ToolCompletion(model=model)

    choices = getattr(response, "choices", None) or []
    if not choices:
        return ToolCompletion(model=model)
    message = getattr(choices[0], "message", None)
    return ToolCompletion(
        text=str(getattr(message, "content", "") or "") if message is not None else "",
        model=model,
        tool_calls=_tool_calls_from(message),
    )


def _completion_from(response: Any, model: str) -> Optional[Completion]:
    """Reads the first choice and the usage out of a response, tolerating absences."""
    choices = getattr(response, "choices", None) or []
    if not choices:
        print(f"[System2] {model} returned no choices", file=sys.stderr)
        return None
    message = getattr(choices[0], "message", None)
    text = (getattr(message, "content", "") or "") if message is not None else ""
    usage = getattr(response, "usage", None)
    return Completion(
        text=text,
        model=model,
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        finish_reason=str(getattr(choices[0], "finish_reason", "") or ""),
        reasoning=reasoning_from(message),
    )


def reasoning_from(message: Any) -> str:
    """A message's chain of thought, from whichever field the provider used.

    There is no one spelling for this. DeepSeek-style endpoints put it in
    ``reasoning_content``; some use ``reasoning``; others return a list of parts under
    ``reasoning_details``. All three are read, and an absent one is simply an empty string
    -- a plain chat model has no chain of thought and that is not an error.
    """
    if message is None:
        return ""
    for attr in ("reasoning_content", "reasoning"):
        value = getattr(message, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()

    details = getattr(message, "reasoning_details", None)
    if isinstance(details, list):
        parts = []
        for item in details:
            text = item.get("text") or item.get("content") if isinstance(item, dict) else getattr(item, "text", None)
            if text:
                parts.append(str(text))
        return "\n".join(parts).strip()
    return ""
