"""The deterministic model router: a complexity score and a rejection count in, a tier out.

A **pure function**. No database, no network, no clock, no environment: the same two integers
always produce the same configuration. That is the whole point -- routing is a decision the engine
must be able to explain and test exhaustively, and a router that reads global state cannot be
either.

One deliberate exception: :func:`route_or_top`, the dispatcher's entry point, which catches an
unusable assessment and routes to the top tier with a ``CRITICAL`` log instead of letting a bad
value reach the dispatch thread. The pure function stays pure; the shell around it absorbs the one
failure a tick cannot afford.

Two inputs, and they mean different things:

* ``complexity_score`` (1-5) is the Architect's assessment of the *work*. It sets the **base** tier:
  a one-line change does not need the strongest model in the fleet, and a core-architecture task
  should never be handed to the cheapest.
* ``rejection_attempts`` is how many times a *human* has refused the result. It **escalates** the
  tier, one step per rejection, because a second attempt at the same problem with the same model
  is how you get the same answer twice. (Machine failures do not escalate: an OOM is not evidence
  that the model was too weak -- see ``system_failures`` in ``storage/db.py``.)

The tier table is the single place a real provider mapping lives. Today every tier resolves to a
route the rest of the codebase already uses, so the router is exercised end to end without inventing
model names; swapping in real providers is a change to this table and nothing else.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

LOGGER = logging.getLogger(__name__)

# The tiers, weakest first. ``label`` is for logs and the UI; ``route`` is the
# ``provider:model`` string the System 2 client expects (``agents/model_routing`` uses the same
# shape, and ``orchestration/system2.route_model`` strips the prefix).
TIERS: List[Dict[str, str]] = [
    {
        "tier": "economy",
        "label": "Economy",
        "route": "openai:policy/coder-standard-test",
        "why": "A small, well-specified change does not need the strongest model.",
    },
    {
        "tier": "standard",
        "label": "Standard",
        "route": "openai:policy/coder-standard-test",
        "why": "Ordinary implementation work.",
    },
    {
        "tier": "deep",
        "label": "Deep",
        "route": "openai:policy/coder-deep-test",
        "why": "Core logic, data models, security -- work where a wrong answer is expensive.",
    },
    {
        "tier": "architect",
        "label": "Architect-grade",
        "route": "openai:policy/coder-deep-test",
        "why": "System-wide change, or a problem a weaker model has already failed twice.",
    },
]

MIN_COMPLEXITY = 1
MAX_COMPLEXITY = 5
TOP_TIER = len(TIERS) - 1

# The policy ladder maps onto the bootloader's three slots: the two cheapest rungs share the
# weakest configured model, which is why a task can be routed to "economy" and still land on the
# fleet's tier_0. The mapping is policy, so it is written out rather than computed.
BOOT_SLOT_BY_TIER: Dict[str, str] = {
    "economy": "tier_0",
    "standard": "tier_0",
    "deep": "tier_1",
    "architect": "tier_2",
}
BOOT_SLOTS: tuple = ("tier_0", "tier_1", "tier_2")

# Complexity 1-5 -> a base tier. 1 is trivial, 5 is architectural. The mapping is deliberately
# explicit rather than arithmetic: it is a policy, and a policy should be readable.
BASE_TIER_BY_COMPLEXITY: Dict[int, int] = {1: 0, 2: 1, 3: 2, 4: 2, 5: 3}


class RoutingError(ValueError):
    """A routing input is outside the domain the router understands."""


class TaskUnroutable(RoutingError):
    """No configured tier is capable enough to run this task, and falling back would be reckless.

    Distinct from :class:`RoutingError` on purpose: a bad *input* is a bug, while this is a real
    task the deployment cannot run. The dispatcher fails the node on it instead of retrying.
    """


def _validate(complexity_score: Any, rejection_attempts: Any) -> tuple:
    # ``bool`` is an ``int`` in Python, and ``True`` is not a complexity. Rejected explicitly.
    if isinstance(complexity_score, bool) or not isinstance(complexity_score, int):
        raise RoutingError(f"complexity_score must be an integer, not {type(complexity_score).__name__}")
    if not MIN_COMPLEXITY <= complexity_score <= MAX_COMPLEXITY:
        raise RoutingError(
            f"complexity_score must be between {MIN_COMPLEXITY} and {MAX_COMPLEXITY}, "
            f"not {complexity_score}"
        )
    if isinstance(rejection_attempts, bool) or not isinstance(rejection_attempts, int):
        raise RoutingError(
            f"rejection_attempts must be an integer, not {type(rejection_attempts).__name__}"
        )
    if rejection_attempts < 0:
        raise RoutingError(f"rejection_attempts must not be negative, not {rejection_attempts}")
    return complexity_score, rejection_attempts


def base_tier(complexity_score: int) -> int:
    """The tier the work itself calls for, before any rejection escalates it."""
    _validate(complexity_score, 0)
    return BASE_TIER_BY_COMPLEXITY[complexity_score]


def route(complexity_score: int, rejection_attempts: int = 0) -> Dict[str, Any]:
    """The model configuration for a task, from its complexity and its rejection count.

    Returns the tier's configuration plus the two inputs and the reason, so a caller can log *why*
    a node was routed where it was without re-deriving it.

    Escalation is one step per rejection and stops at the top tier: past that there is nowhere
    stronger to go, and pretending otherwise would hide that the task itself is the problem.
    """
    complexity, rejections = _validate(complexity_score, rejection_attempts)

    base = BASE_TIER_BY_COMPLEXITY[complexity]
    escalated = min(base + rejections, TOP_TIER)
    tier = TIERS[escalated]

    route_string = tier["route"]
    provider, _, model = route_string.partition(":")

    return {
        "tier": tier["tier"],
        "label": tier["label"],
        "provider": provider,
        "model": model,
        "route": route_string,
        "complexity_score": complexity,
        "rejection_attempts": rejections,
        "base_tier": TIERS[base]["tier"],
        "escalated": escalated > base,
        "why": tier["why"],
    }


def _top_tier(reason: str) -> Dict[str, Any]:
    """The strongest tier's configuration, with the fallback recorded in ``why``.

    ``complexity_score`` and ``rejection_attempts`` are ``None`` rather than invented numbers: the
    inputs were unusable, so there is no honest score to report, and reporting one would hide the
    very thing the fallback exists to expose.
    """
    tier = TIERS[TOP_TIER]
    provider, _, model = tier["route"].partition(":")
    return {
        "tier": tier["tier"],
        "label": tier["label"],
        "provider": provider,
        "model": model,
        "route": tier["route"],
        "complexity_score": None,
        "rejection_attempts": None,
        "base_tier": tier["tier"],
        "escalated": False,
        "why": f"Unroutable assessment ({reason}); defaulted to maximum intelligence.",
    }


def route_or_top(complexity_score: Any, rejection_attempts: Any = 0) -> Dict[str, Any]:
    """Route a node, or route it to the top tier and say so loudly.

    The dispatcher's entry point, and deliberately **not** pure like :func:`route`: it absorbs the
    one failure the dispatch thread cannot afford. A missing or nonsensical assessment -- ``NULL``
    from legacy data, or a planner that never wrote one -- must not crash a tick, and it must not
    quietly buy a weak model either. An unroutable node is planned by the strongest tier there is,
    and the reason is logged at ``CRITICAL`` so the missing assessment is *visible* rather than
    papered over. Defaulting to a cheap tier is how a task that needed the best model gets three
    identical wrong answers and burns its retry budget learning nothing.
    """
    try:
        return route(complexity_score, rejection_attempts)
    except RoutingError as error:
        LOGGER.critical(
            "unroutable node (complexity_score=%r, rejection_attempts=%r): %s -- routing to the "
            "top tier",
            complexity_score, rejection_attempts, error,
        )
        return _top_tier(str(error))


def resolve_endpoint(
    routed: Dict[str, Any],
    config: Any = None,
    *,
    refuse: bool = True,
) -> Dict[str, Any]:
    """The configured endpoint for a routed tier, with the bootloader's fallback matrix applied.

    :func:`route` decides *how strong* the work needs the model to be; this decides *which model
    that is*, by reading the fleet the bootloader proved at start-up. Still a pure function of its
    inputs -- ``config`` defaults to the active configuration, but it is a parameter, so the
    matrix is exhaustively testable without touching global state.

    The matrix, and nothing else:

    * the required tier is configured -- use it;
    * it is not, but a weaker one is -- log ``CRITICAL`` and use the strongest configured tier at
      or below the requirement. Never silently, and never the other way round;
    * the requirement is the **strongest** tier and the only thing configured is the **weakest**
      -- refuse (:class:`TaskUnroutable`) when ``refuse`` is set. Two rungs down is not a
      fallback, it is a decision to hand system-wide work to a model that cannot do it, and a
      wasted artifact is worse than a stopped task.

    ``refuse=False`` is for the one caller that has no alternative: the workflow planner, where
    the Architect *is* the run and failing the tier would fail the request outright. Node dispatch
    keeps the refusal, because a node it will not run can be reported and revisited.
    """
    from core.config import active_config

    fleet = config if config is not None else active_config()
    slot = BOOT_SLOT_BY_TIER.get(str(routed.get("tier") or ""), BOOT_SLOTS[0])
    required = BOOT_SLOTS.index(slot)
    configured = fleet.configured_slots()

    at_or_below = [index for index in configured if index <= required]
    if at_or_below:
        chosen = max(at_or_below)
    else:
        # Nothing as cheap as the task asked for; the weakest configured tier is stronger than
        # requested, which wastes budget but cannot under-power the work.
        chosen = max(configured)

    if chosen == required:
        pass
    elif chosen == 0 and required == len(BOOT_SLOTS) - 1 and refuse:
        raise TaskUnroutable(
            f"the task needs {slot} ({routed.get('label')}) but only tier_0 is configured: "
            f"{fleet.tier(0).route()} cannot run work of this complexity"
        )
    else:
        LOGGER.critical(
            "%s is not configured; falling back to %s (%s) for a %s task",
            slot, BOOT_SLOTS[chosen], fleet.tier(chosen).route(), routed.get("label"),
        )

    tier = fleet.tier(chosen)
    return {
        "provider": str(tier.provider).strip(),
        "model": str(tier.model_name).strip(),
        "route": tier.route(),
        "base_url": str(tier.base_url or ""),
        "api_key": str(tier.api_key or ""),
        "boot_tier": BOOT_SLOTS[chosen],
        "requested_boot_tier": slot,
        "fell_back": chosen != required,
        "why": (
            f"{slot} configured as {tier.route()}" if chosen == required
            else f"{slot} unavailable; fell back to {BOOT_SLOTS[chosen]} ({tier.route()})"
        ),
    }


def endpoint_for_tier(tier_name: str, config: Any = None) -> Dict[str, Any]:
    """The configured endpoint for a named policy tier, with the same fallback matrix.

    The dispatcher routes a *node* by its complexity (:func:`route_or_top` then
    :func:`resolve_endpoint`); the workflow planner is not a node, it is the Architect planning a
    directive, and its tier is named rather than computed. Both go through the same matrix, so a
    config file governs both paths and there is no second place a model can be chosen.

    It does not refuse: a named tier falls back to the best configured one and says so at
    ``CRITICAL``. There is no alternative path for the Architect -- refusing would fail the
    request itself -- while a node the dispatcher will not run is reported and revisited.
    """
    tier = next((entry for entry in TIERS if entry["tier"] == tier_name), None)
    if tier is None:
        raise RoutingError(f"unknown tier {tier_name!r}; known: {[e['tier'] for e in TIERS]}")
    return resolve_endpoint(
        {"tier": tier["tier"], "label": tier["label"]}, config, refuse=False
    )
