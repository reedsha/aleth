"""What a run costs, counted *before* it is spent.

The step ceiling bounds the **work**; this bounds the **bill**. They are not the same limit: thirty
steps of a small model is cheap, and five steps dragging a hundred thousand tokens of history is
not. An unbounded agent is malware -- it will happily spend a user's budget proving it cannot solve
the task.

The counter uses the **vendored** ``cl100k_base`` encoding (``vendor/tiktoken/``), so it is exact and
fully offline: tiktoken's BPE files are otherwise downloaded on first use, and this engine cannot
fetch one mid-run. The boot sequence points ``TIKTOKEN_CACHE_DIR`` at the vendored copy, and this
module resolves the same directory itself as well -- a swarm child is a different process, and its
environment has been through the sanitizer, so relying on the variable alone would leave the child
guessing.

The estimate behind it is a **last resort, and it says so**: if the encoding cannot be loaded the
counter reports that once on stderr rather than silently approximating a user's budget.
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path
from typing import Any, Optional

# The ceiling on one intent's cumulative tokens. Generous for real work -- a plan's worth of reading,
# editing and verifying -- and small enough that a runaway loop cannot cost a fortune. Configurable
# through ``ALETH_MAX_INTENT_TOKENS`` for a deployment that wants a different number.
MAX_INTENT_TOKENS = 250_000
MAX_TOKENS_ENV = "ALETH_MAX_INTENT_TOKENS"

# What a token costs, in **micros** -- 1e-6 of a dollar, so 1 cent is 10,000 micros -- and the
# ceiling on one intent's bill (Phases 44-45). Integers, always: money is never a float. 0.25c / 1K
# prompt and 1.0c / 1K completion, expressed in micros.
MICROS_PER_1K_PROMPT_TOKENS = 2_500
MICROS_PER_1K_COMPLETION_TOKENS = 10_000
MAX_COST_MICROS = 5_000_000  # $5.00
MAX_COST_ENV = "ALETH_MAX_COST_MICROS"

# The vendored encoding's filename is the SHA-1 of the URL tiktoken fetched it from -- that is the
# cache key tiktoken computes, so the file has to be named it for the loader to find it. The blob
# itself is checked against tiktoken's published SHA-256 when it is installed here.
ENCODING_NAME = "cl100k_base"
ENCODING_CACHE_KEY = "9b5ad71b2ce5302211f9c61530b329a4922fc6a4"
VENDOR_SUBDIR = os.path.join("vendor", "tiktoken")
CACHE_DIR_ENV = "TIKTOKEN_CACHE_DIR"

# The fallback ratio. An English-and-code mix runs about four characters per token for the common
# encodings, and rounding up is the direction a *budget* should err. Only ever used when the vendored
# encoding is missing, and reported loudly when it is.
CHARS_PER_TOKEN = 4

_ENCODING_CACHE: Any = None
_ENCODING_TRIED = False
_ESTIMATE_REPORTED = False


def vendor_dir() -> str:
    """The vendored encoding directory, or ``""`` when it is not where it should be.

    Three places, in order, because the engine runs from a checkout *and* from an installed wheel:
    an explicit ``TIKTOKEN_CACHE_DIR``, the repository beside this module, and the prefix a wheel's
    data files land in. The first is what the boot sets; the others make the module correct on its
    own, which is what a swarm child needs.
    """
    candidates = []
    configured = (os.environ.get(CACHE_DIR_ENV) or "").strip()
    if configured:
        candidates.append(configured)
    candidates.append(str(Path(__file__).resolve().parents[1] / VENDOR_SUBDIR))
    candidates.append(str(Path(sys.prefix) / VENDOR_SUBDIR))
    for candidate in candidates:
        if os.path.isfile(os.path.join(candidate, ENCODING_CACHE_KEY)):
            return candidate
    return ""


def install_cache_dir() -> str:
    """Point ``TIKTOKEN_CACHE_DIR`` at the vendored copy. Returns the directory, or ``""``.

    Called by the boot sequence (``main.py``) and again by :func:`_encoding`, so the value is right
    whether the engine was started normally or this module was imported into a bare process.
    """
    found = vendor_dir()
    if found:
        os.environ[CACHE_DIR_ENV] = found
    return found


class TokenBudgetExceeded(RuntimeError):
    """The intent has spent its whole token budget. A hard stop, and deliberately not retryable."""


class CostBudgetExceeded(TokenBudgetExceeded):
    """The intent has spent its whole money allowance. A hard stop, like the token ceiling.

    A subclass because it is the same fact in a different unit -- the budget is spent -- so a caller
    that already handles :class:`TokenBudgetExceeded` handles this too, and the token ceiling keeps
    its own meaning.
    """


def max_intent_tokens() -> int:
    """The ceiling, from the environment when it is set to a usable number."""
    raw = (os.environ.get(MAX_TOKENS_ENV) or "").strip()
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return MAX_INTENT_TOKENS


def _encoding() -> Optional[Any]:
    """The vendored tiktoken encoding, or ``None`` when it genuinely cannot be loaded."""
    global _ENCODING_CACHE, _ENCODING_TRIED, _ESTIMATE_REPORTED
    if _ENCODING_TRIED:
        return _ENCODING_CACHE
    _ENCODING_TRIED = True
    install_cache_dir()
    try:
        import tiktoken
    except Exception:  # not installed: the estimate is the whole story, and it is reported
        return _report_estimate()
    try:
        _ENCODING_CACHE = tiktoken.get_encoding(ENCODING_NAME)
    except Exception:
        # No vendored blob and no network. This engine does not fetch one mid-run.
        return _report_estimate()
    return _ENCODING_CACHE


def _report_estimate() -> None:
    """Say, once, that the budget is being approximated -- and where the encoding should be."""
    global _ESTIMATE_REPORTED
    if not _ESTIMATE_REPORTED:
        _ESTIMATE_REPORTED = True
        print(
            f"[tokens] the vendored {ENCODING_NAME} encoding is not available; counting at "
            f"{CHARS_PER_TOKEN} characters per token. Expected it under {VENDOR_SUBDIR}/.",
            file=sys.stderr,
        )
    return None


def count_tokens(text: str) -> int:
    """The token count of ``text``: tiktoken's when it is available offline, else the estimate."""
    value = str(text or "")
    if not value:
        return 0
    encoder = _encoding()
    if encoder is not None:
        try:
            return len(encoder.encode(value))
        except Exception:  # pragma: no cover - an encoder that cannot encode
            pass
    return max(1, math.ceil(len(value) / CHARS_PER_TOKEN))


def budget_exceeded(spent: int, *, limit: Optional[int] = None) -> bool:
    """Whether ``spent`` has reached the ceiling. One comparison, in one place."""
    return int(spent) >= int(limit if limit is not None else max_intent_tokens())


def max_cost_micros() -> int:
    """The money ceiling, from the environment when it is set to a usable number."""
    raw = (os.environ.get(MAX_COST_ENV) or "").strip()
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return MAX_COST_MICROS


def cost_micros(prompt_tokens: int, completion_tokens: int) -> int:
    """What those token counts cost, in micros, rounded to the nearest micro.

    Integer arithmetic throughout. A fractional cent cannot be represented in binary floating point,
    and a bill that drifts by a fraction of a cent per step drifts without bound -- which is exactly
    what a hard ceiling must not do.
    """
    total = (
        max(0, int(prompt_tokens)) * MICROS_PER_1K_PROMPT_TOKENS
        + max(0, int(completion_tokens)) * MICROS_PER_1K_COMPLETION_TOKENS
    )
    return (total + 500) // 1000


def cost_exceeded(spent_micros: int, *, limit: Optional[int] = None) -> bool:
    """Whether the bill has reached the ceiling. One integer comparison, in one place."""
    return int(spent_micros) >= int(limit if limit is not None else max_cost_micros())


__all__ = [
    "CHARS_PER_TOKEN",
    "ENCODING_CACHE_KEY",
    "ENCODING_NAME",
    "CostBudgetExceeded",
    "MAX_COST_ENV",
    "MAX_COST_MICROS",
    "MAX_INTENT_TOKENS",
    "MAX_TOKENS_ENV",
    "MICROS_PER_1K_COMPLETION_TOKENS",
    "MICROS_PER_1K_PROMPT_TOKENS",
    "TokenBudgetExceeded",
    "budget_exceeded",
    "cost_exceeded",
    "cost_micros",
    "count_tokens",
    "install_cache_dir",
    "max_cost_micros",
    "max_intent_tokens",
    "vendor_dir",
]
