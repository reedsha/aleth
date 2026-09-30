"""Phase 7: the deterministic router, proved over the whole input matrix.

    .\\venv\\Scripts\\python.exe -m pytest tests/test_model_router.py -n0 -q

The router is a pure function, so the test is a truth table: every complexity in its domain crossed
with every rejection count that can occur, asserting the exact tier. Nothing is mocked because
nothing needs to be -- that is what purity buys.
"""

from __future__ import annotations

import itertools
import logging

import pytest

from orchestration.model_router import (
    BASE_TIER_BY_COMPLEXITY,
    MAX_COMPLEXITY,
    MIN_COMPLEXITY,
    TIERS,
    TOP_TIER,
    RoutingError,
    base_tier,
    route,
    route_or_top,
)

COMPLEXITIES = list(range(MIN_COMPLEXITY, MAX_COMPLEXITY + 1))
REJECTIONS = [0, 1, 2, 3, 4, 5]


class TestTheMatrix:
    @pytest.mark.parametrize("complexity,rejections", list(itertools.product(COMPLEXITIES, REJECTIONS)))
    def test_every_combination_routes_to_the_expected_tier(self, complexity, rejections):
        """The truth table: ``min(base + rejections, top)`` for every reachable pair."""
        expected = min(BASE_TIER_BY_COMPLEXITY[complexity] + rejections, TOP_TIER)

        result = route(complexity, rejections)

        assert result["tier"] == TIERS[expected]["tier"]
        assert result["route"] == TIERS[expected]["route"]
        assert result["complexity_score"] == complexity
        assert result["rejection_attempts"] == rejections
        assert result["base_tier"] == TIERS[BASE_TIER_BY_COMPLEXITY[complexity]]["tier"]

    @pytest.mark.parametrize("complexity", COMPLEXITIES)
    def test_no_rejections_routes_to_the_base_tier(self, complexity):
        assert route(complexity, 0)["tier"] == TIERS[BASE_TIER_BY_COMPLEXITY[complexity]]["tier"]
        assert route(complexity, 0)["escalated"] is False

    @pytest.mark.parametrize("complexity", COMPLEXITIES)
    def test_escalation_is_one_step_per_rejection_until_the_top(self, complexity):
        base = BASE_TIER_BY_COMPLEXITY[complexity]
        for rejections in range(0, TOP_TIER + 3):
            expected = min(base + rejections, TOP_TIER)
            assert route(complexity, rejections)["tier"] == TIERS[expected]["tier"]

    def test_the_top_tier_is_a_ceiling_not_a_warning(self):
        """Past the strongest model there is nowhere to go, and the result says so honestly.

        ``escalated`` reports whether the tier actually *moved*, so at the ceiling it is False even
        with rejections to spare -- the tier did not move, because it could not.
        """
        top = route(MAX_COMPLEXITY, TOP_TIER + 5)
        assert top["tier"] == TIERS[TOP_TIER]["tier"]
        assert top["base_tier"] == TIERS[TOP_TIER]["tier"]
        assert top["escalated"] is False

    def test_a_task_below_the_ceiling_escalates_when_it_is_rejected(self):
        assert route(2, 1)["escalated"] is True
        assert route(2, 1)["tier"] == TIERS[BASE_TIER_BY_COMPLEXITY[2] + 1]["tier"]

    def test_a_trivial_task_is_not_escalated_to_the_strongest_model(self):
        assert route(1, 0)["tier"] == TIERS[0]["tier"]
        assert route(1, 0)["model"] != route(MAX_COMPLEXITY, 0)["model"]

    def test_a_complex_task_starts_high(self):
        assert route(MAX_COMPLEXITY, 0)["tier"] == TIERS[TOP_TIER]["tier"]


class TestPurity:
    def test_the_same_inputs_always_give_the_same_answer(self):
        first = route(4, 2)
        second = route(4, 2)
        assert first == second

    def test_the_result_is_self_describing(self):
        result = route(3, 1)
        assert set(result) >= {
            "tier", "label", "provider", "model", "route",
            "complexity_score", "rejection_attempts", "base_tier", "escalated", "why",
        }
        # ``route`` is the provider:model string the System 2 client expects, split for callers
        # that need the parts.
        assert result["route"] == f"{result['provider']}:{result['model']}"

    def test_base_tier_agrees_with_route(self):
        for complexity in COMPLEXITIES:
            assert base_tier(complexity) == BASE_TIER_BY_COMPLEXITY[complexity]


class TestRefusals:
    @pytest.mark.parametrize("bad", [0, 6, -1, 100])
    def test_a_complexity_outside_the_domain_is_refused(self, bad):
        with pytest.raises(RoutingError, match="between"):
            route(bad, 0)

    @pytest.mark.parametrize("bad", ["3", 3.5, None, True])
    def test_a_non_integer_complexity_is_refused(self, bad):
        with pytest.raises(RoutingError, match="must be an integer"):
            route(bad, 0)

    def test_a_negative_rejection_count_is_refused(self):
        with pytest.raises(RoutingError, match="negative"):
            route(3, -1)

    def test_a_non_integer_rejection_count_is_refused(self):
        with pytest.raises(RoutingError, match="must be an integer"):
            route(3, "1")


class TestTheDispatcherFallback:
    """``route_or_top`` -- the tick's entry point, which must never crash and never under-buy."""

    @pytest.mark.parametrize("bad", [None, "3", 3.5, True, 0, 6, -1, []])
    def test_an_unusable_complexity_routes_to_the_top_tier(self, bad):
        result = route_or_top(bad, 0)
        assert result["tier"] == TIERS[TOP_TIER]["tier"]
        assert result["route"] == TIERS[TOP_TIER]["route"]

    def test_an_unusable_rejection_count_also_routes_to_the_top_tier(self):
        assert route_or_top(1, None)["tier"] == TIERS[TOP_TIER]["tier"]
        assert route_or_top(1, -1)["tier"] == TIERS[TOP_TIER]["tier"]

    def test_the_fallback_is_logged_at_critical(self, caplog):
        with caplog.at_level(logging.CRITICAL, logger="orchestration.model_router"):
            route_or_top(None, None)
        assert any(record.levelno == logging.CRITICAL for record in caplog.records)
        assert "top tier" in caplog.text

    def test_the_fallback_does_not_invent_a_score(self):
        result = route_or_top(None, None)
        assert result["complexity_score"] is None
        assert result["rejection_attempts"] is None
        assert result["escalated"] is False
        assert result["route"] == f"{result['provider']}:{result['model']}"

    def test_a_usable_pair_delegates_to_the_pure_router(self):
        assert route_or_top(2, 1) == route(2, 1)
        assert route_or_top(MAX_COMPLEXITY, 0) == route(MAX_COMPLEXITY, 0)
