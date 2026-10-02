"""What a run costs, counted *before* it is spent.

The step ceiling bounds the **work**; this bounds the **bill**. They are not the same limit: thirty
steps of a small model is cheap, and five steps dragging a hundred thousand tokens of history is
not. An unbounded agent is malware -- it will happily spend a user's budget proving it cannot solve
the task.

The counter prefers ``tiktoken`` when it can load an encoding **offline** and falls back to the
standard four-characters-per-token estimate otherwise. That fallback is not laziness: tiktoken's BPE
files are downloaded on first use, and this engine runs offline by design, so a hard dependency on
it would be a dependency the engine cannot actually use in the environment it promises. The estimate
over-counts rather than under-counts, which is the safe direction for a budget.
"""

from __future__ import annotations

import math
import os
from typing import Any, Optional

# The ceiling on one intent's cumulative tokens. Generous for real work -- a plan's worth of reading,
# editing and verifying -- and small enough that a runaway loop cannot cost a fortune. Configurable
# through ``ALETH_MAX_INTENT_TOKENS`` for a deployment that wants a different number.
MAX_INTENT_TOKENS = 250_000
MAX_TOKENS_ENV = "ALETH_MAX_INTENT_TOKENS"

# The fallback ratio. An English-and-code mix runs about four characters per token for the common
# encodings, and rounding up is the direction a *budget* should err.
CHARS_PER_TOKEN = 4

_ENCODING_CACHE: Any = None
_ENCODING_TRIED = False


class TokenBudgetExceeded(RuntimeError):
    """The intent has spent its whole token budget. A hard stop, and deliberately not retryable."""


def max_intent_tokens() -> int:
    """The ceiling, from the environment when it is set to a usable number."""
    raw = (os.environ.get(MAX_TOKENS_ENV) or "").strip()
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return MAX_INTENT_TOKENS


def _encoding() -> Optional[Any]:
    """A tiktoken encoding, or ``None`` when one cannot be had offline. Cached either way."""
    global _ENCODING_CACHE, _ENCODING_TRIED
    if _ENCODING_TRIED:
        return _ENCODING_CACHE
    _ENCODING_TRIED = True
    try:
        import tiktoken
    except Exception:  # not installed: the estimate is the whole story
        return None
    try:
        _ENCODING_CACHE = tiktoken.get_encoding("cl100k_base")
    except Exception:
        # No cached BPE file and no network. The engine does not fetch one mid-run.
        _ENCODING_CACHE = None
    return _ENCODING_CACHE


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


__all__ = [
    "CHARS_PER_TOKEN",
    "MAX_INTENT_TOKENS",
    "MAX_TOKENS_ENV",
    "TokenBudgetExceeded",
    "budget_exceeded",
    "count_tokens",
    "max_intent_tokens",
]
