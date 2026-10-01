"""The routing gateway: System 1 classifies a task, and the engine picks the brain.

Phase 19 makes the *agent* decision -- System 2's Architect or a System 1 Coder -- an explicit,
typed, auditable step rather than an implicit consequence of a title. Three properties, in order
of importance:

1. **Total.** :func:`classify_task` never raises into the dispatcher. A classifier that fails,
   times out, or answers off-contract routes to the **safest** brain -- the Architect, which can
   reason its way out -- and records ``CLASSIFIER_FAULT``. A fault is visible in the ledger, never
   silently absorbed.
2. **Injectable.** The classifier is a parameter, not a global lookup. The default is
   ``agents.laya_model.classify`` (the seam that answers with the checkpoint when
   ``LAYA_BACKEND=model``, the word list otherwise); a test injects a fake that returns fixed
   verdicts. That is what makes the characterization suite immune to the ambient environment:
   see ``tests/conftest.py``.
3. **Auditable.** Every decision is appended to the ``routing_decisions`` ledger
   (:mod:`storage.telemetry`) with the evidence behind it -- the intent, the domain, the
   confidence band, the chosen route, and whether the classifier faulted.

This module owns the *agent* decision only. The **model** tier remains
:mod:`orchestration.model_router`'s decision (Phase 7): a classifier band is a second,
independent input, and collapsing the two would put a second authority in the model choice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

LOGGER = logging.getLogger(__name__)

# The two brains. ``architect`` is System 2 (the reasoning brain) and the safe default; ``coder``
# is the System 1 implementation path.
ROUTE_ARCHITECT = "architect"
ROUTE_CODER = "coder"

# A coarse complexity band, derived from the verdict, that selects the route.
COMPLEXITY_HIGH = "high"
COMPLEXITY_LOW = "low"

# Recorded in the ``fault`` column when the classifier could not answer. The engine reads it to
# tell a real routing decision from a default taken because the classifier was unavailable.
CLASSIFIER_FAULT = "CLASSIFIER_FAULT"

# The evidence a fault carries. Kept as one string so the ledger row and the reason read the same.
ENGINE_HOPELESS = "the classifier produced no usable verdict"

__all__ = [
    "CLASSIFIER_FAULT",
    "COMPLEXITY_HIGH",
    "COMPLEXITY_LOW",
    "ROUTE_ARCHITECT",
    "ROUTE_CODER",
    "RouteDecision",
    "classify_task",
    "decision_payload",
    "record_decision",
    "route_for_complexity",
]


@dataclass(frozen=True)
class RouteDecision:
    """One classified route: which brain runs the task, and the evidence for it.

    ``fault`` is non-empty exactly when the classifier could not answer; in that case ``route``
    is :data:`ROUTE_ARCHITECT` and ``complexity`` is :data:`COMPLEXITY_HIGH` by policy, so the
    invariant ``route == ARCHITECT iff complexity == HIGH`` holds on every path.
    """

    route: str
    complexity: str
    intent: str = ""
    domain: str = ""
    confidence: float = 0.0
    reasons: Tuple[str, ...] = ()
    fault: str = ""
    engine: str = ""

    @property
    def is_fault(self) -> bool:
        return bool(self.fault)


def route_for_complexity(complexity: str) -> str:
    """The brain a complexity band selects. High is System 2; everything else is a Coder."""
    return ROUTE_ARCHITECT if complexity == COMPLEXITY_HIGH else ROUTE_CODER


def _default_classifier() -> Callable[..., Any]:
    """``agents.laya_model.classify`` -- the checkpoint-or-word-list seam, imported lazily.

    Deferred because ``agents.laya`` (and through it this module) sits under the workflow package
    that imports ``laya_model``; a module-scope import here would close the cycle the engine's
    own docstrings warn about.
    """
    from agents import laya_model

    return laya_model.classify


def _verdict_is_wellformed(verdict: Any) -> bool:
    """Whether a verdict carries a decision this engine can act on.

    An injected fake -- or a future classifier -- can return ``None``, the wrong type, or a
    verdict with an empty domain and a non-numeric confidence. Any of those is
    ``CLASSIFIER_FAULT``, not a route: acting on a half-filled answer is how a malformed response
    becomes a wrong brain.
    """
    from agents import laya

    if getattr(verdict, "intent", None) not in (laya.INTENT_ADMIN, laya.INTENT_CODE):
        return False
    domain = getattr(verdict, "domain", None)
    if not isinstance(domain, str) or not domain:
        return False
    confidence = getattr(verdict, "confidence", None)
    # ``bool`` is an ``int``; a boolean confidence is not a confidence.
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return False
    return True


def _complexity_of(verdict: Any) -> str:
    """The complexity band a verdict implies.

    Two signals are high: an **administrative** intent -- the directive is analysis or
    architecture review, which is the Architect's own work -- and a **core** domain -- algorithms,
    concurrency, security, auth, the work where a wrong answer is expensive. Everything else is
    implementation the role-based Coder path handles.
    """
    from agents import laya

    if verdict.intent == laya.INTENT_ADMIN:
        return COMPLEXITY_HIGH
    if verdict.domain == laya.DOMAIN_CORE:
        return COMPLEXITY_HIGH
    return COMPLEXITY_LOW


def _fault(reason: str) -> RouteDecision:
    """The safe route: the Architect, with the reason recorded at ``CRITICAL``.

    A fault is not a cheap default. Routing a task whose classifier failed to a Coder would be
    betting that the failure happened on an easy task -- the one bet a fault gives no evidence
    for. The Architect can do the simple case and the hard one.
    """
    LOGGER.critical("CLASSIFIER_FAULT: %s -- routing to the Architect", reason)
    return RouteDecision(
        route=ROUTE_ARCHITECT,
        complexity=COMPLEXITY_HIGH,
        reasons=(reason,),
        fault=CLASSIFIER_FAULT,
    )


def classify_task(
    text: str,
    *,
    tag: Optional[str] = None,
    classifier: Optional[Callable[..., Any]] = None,
) -> RouteDecision:
    """Classify one task and return its route. **Never raises.**

    ``classifier`` is the injected engine; the default reads ``agents.laya_model``, so the
    checkpoint answers when it is selected and the word list otherwise. ``text`` is the task's
    title and ``tag`` its declared tag, if any -- an explicit tag is a fact, not a judgement, and
    the classifier is told it rather than asked to re-derive it.

    On any failure the decision is the Architect route with :data:`CLASSIFIER_FAULT`; the caller
    can always dispatch on the result.
    """
    engine = classifier if classifier is not None else _default_classifier()
    context: Optional[Mapping[str, Any]] = {"tag": tag} if tag else None

    try:
        verdict = engine(str(text or ""), context)
    except BaseException as exc:  # a classifier failure must not take the dispatch thread down
        return _fault(f"{type(exc).__name__}: {exc}")

    if not _verdict_is_wellformed(verdict):
        return _fault(ENGINE_HOPELESS)

    complexity = _complexity_of(verdict)
    engine_name = "injected"
    if classifier is None:
        from agents import laya_model

        engine_name = laya_model.active_engine()
    return RouteDecision(
        route=route_for_complexity(complexity),
        complexity=complexity,
        intent=str(verdict.intent),
        domain=str(verdict.domain),
        confidence=float(verdict.confidence),
        reasons=tuple(str(reason) for reason in (getattr(verdict, "reasons", ()) or ())),
        engine=engine_name,
    )


def decision_payload(decision: RouteDecision) -> Dict[str, Any]:
    """A decision as JSON-able data, for the role descriptor and the ledger.

    Plain types on purpose: this crosses the process boundary in the worker's descriptor and is
    written verbatim to SQLite, so it may carry nothing live.
    """
    return {
        "route": decision.route,
        "complexity": decision.complexity,
        "intent": decision.intent,
        "domain": decision.domain,
        "confidence": decision.confidence,
        "fault": decision.fault,
        "engine": decision.engine,
        "reasons": list(decision.reasons),
    }


def record_decision(
    db_path: str,
    decision: RouteDecision,
    *,
    plan_id: str = "",
    task_id: str = "",
    session_id: str = "",
) -> str:
    """Append the decision to the routing ledger and return its id. **Raises** on failure.

    The audit guarantee matches ``storage.telemetry.record``'s: a routing decision that could not
    be written is not a decision this engine can claim to have made, so the write failure is the
    caller's to see rather than a silent no-op.
    """
    from storage import telemetry

    return telemetry.record_routing_decision(
        db_path,
        plan_id=plan_id,
        task_id=task_id,
        session_id=session_id,
        route=decision.route,
        complexity=decision.complexity,
        intent=decision.intent,
        domain=decision.domain,
        confidence=decision.confidence,
        fault=decision.fault,
        engine=decision.engine,
        evidence=list(decision.reasons),
    )
