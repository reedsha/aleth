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

import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# The opt-out. Unset means "enabled once configured", so a checkout with a ``.env``
# starts making real calls, while the test suite sets this to ``0`` to pin the offline
# path deterministically instead of depending on the developer's shell.
ENV_ENABLED = "DEEPAGENTS_SYSTEM2"

ENV_API_KEY = "OPENAI_API_KEY"
ENV_BASE_URL = "OPENAI_BASE_URL"

# Values of ``DEEPAGENTS_SYSTEM2`` that turn System 2 off, beyond the bare "0".
DISABLED_VALUES = frozenset({"0", "false", "off", "no"})

# Route names carry their transport as a ``provider:`` prefix (``openai:policy/x``).
# The API wants only the model name.
PROVIDER_PREFIXES = ("openai:",)


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


def provider_config() -> Optional[Dict[str, str]]:
    """The configured provider endpoint, or ``None`` when it is incomplete.

    An empty export counts as unset, the same rule ``env_boot`` applies to a shadowed
    variable: a name present but empty is not a configuration.
    """
    key = (os.environ.get(ENV_API_KEY) or "").strip()
    base = (os.environ.get(ENV_BASE_URL) or "").strip()
    if not key or not base:
        return None
    return {"api_key": key, "base_url": base.rstrip("/")}


def is_configured() -> bool:
    """Whether a provider endpoint is available at all."""
    return provider_config() is not None


def is_enabled() -> bool:
    """Whether a real call should be attempted right now.

    Enabled by default *once configured*, and switched off by ``DEEPAGENTS_SYSTEM2``
    (``0`` / ``false`` / ``off`` / ``no``). Tests set the flag so the suite pins the
    offline path regardless of the shell it runs in.
    """
    flag = (os.environ.get(ENV_ENABLED) or "").strip().lower()
    if flag in DISABLED_VALUES:
        return False
    return is_configured()


def complete(
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int = 16384,
    timeout: float = 180.0,
    client: Any = None,
    images: Optional[List[str]] = None,
) -> Optional[Completion]:
    """One chat completion, or ``None`` when it cannot be made.

    ``client`` is injectable so the seam can be exercised without a network; production
    callers leave it unset and the provider endpoint is read from the environment.

    ``images`` are data URLs (``data:image/png;base64,...``) attached to the user turn as
    OpenAI-compatible ``image_url`` parts, for a vision-capable *model*. With none, the
    message is the plain string it has always been -- so the text-only path is unchanged.

    Returns ``None`` -- and reports one line to stderr -- for every failure mode: no
    configuration, a transport error, or an empty answer. The caller decides what to do
    without it; nothing here raises.
    """
    if client is None:
        config = provider_config()
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
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
        )
    except Exception as exc:
        print(f"[System2] call to {model} failed: {exc}", file=sys.stderr)
        return None

    return _completion_from(response, model)


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
