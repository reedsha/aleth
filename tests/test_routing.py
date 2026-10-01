"""Phase 19: the routing gateway, the injection seam, and the ledger it writes.

Every test here injects its classifier -- there is no live inference and no dependence on the
ambient environment. The two failures this batch exists to eliminate were both *ordering*
failures: ``env_boot`` loads ``.env`` with ``override=True``, so a test that imports ``app``
mid-process armed ``LAYA_BACKEND=model`` for every later test in the same worker, and the
characterization suite then observed a checkpoint instead of the word list it pins. The fix is
injection (``agents.laya_model.install``), and the first test below is the regression proof.
"""

from __future__ import annotations

import sqlite3

import pytest

from agents import laya, laya_model
from orchestration import routing
from storage import telemetry


def _verdict(
    intent: str = laya.INTENT_CODE,
    domain: str = laya.DOMAIN_GENERAL,
    confidence: float = 0.5,
    reasons=("fake classifier",),
):
    """A well-formed verdict, built from the real dataclass."""
    return laya.Verdict(
        intent=intent,
        domain=domain,
        coder=laya.CODER_STANDARD,
        confidence=confidence,
        reasons=tuple(reasons),
    )


class _Classifier:
    """A recording classifier: returns a fixed verdict, or raises a fixed error."""

    def __init__(self, verdict=None, error=None):
        self.verdict = verdict
        self.error = error
        self.calls = []

    def __call__(self, text, context=None):
        self.calls.append((text, context))
        if self.error is not None:
            raise self.error
        return self.verdict


def _state_db(tmp_path) -> str:
    """A real state database with the real ledger schema, on a throwaway path."""
    path = str(tmp_path / "aleth_state.db")
    connection = sqlite3.connect(path)
    try:
        telemetry.apply_telemetry_schema(connection)
        connection.commit()
    finally:
        connection.close()
    return path


# -- the seam is injected, not inherited from the environment --------------------------------


def test_a_model_environment_does_not_arm_the_checkpoint(monkeypatch):
    """The flake, pinned: ``.env`` may set ``LAYA_BACKEND=model`` mid-process.

    The suite injects its System 1 (``tests/conftest.py``), so the environment cannot change the
    engine a test observes -- and this asserts that directly, because the failure it prevents was
    intermittent and order-dependent.
    """
    monkeypatch.setenv(laya_model.ENV_BACKEND, "model")

    engine = laya_model.active_engine()
    verdict = laya_model.classify("explain the architecture")

    assert engine == laya_model.BACKEND_HEURISTIC
    assert verdict == laya.classify("explain the architecture")


def test_install_replaces_the_engine_and_returns_the_previous_one():
    substitute = laya_model.Resolver(False, lambda: None)
    previous = laya_model.install(substitute)
    try:
        assert laya_model.installed() is substitute
    finally:
        restored = laya_model.install(previous)
    assert restored is substitute


# -- classification is total: it always returns a route ---------------------------------------


def test_a_raising_classifier_routes_to_the_architect_and_records_the_fault():
    decision = routing.classify_task("anything", classifier=_Classifier(error=RuntimeError("down")))

    assert decision.route == routing.ROUTE_ARCHITECT
    assert decision.complexity == routing.COMPLEXITY_HIGH
    assert decision.fault == routing.CLASSIFIER_FAULT
    assert "RuntimeError" in decision.reasons[0]


@pytest.mark.parametrize(
    "verdict",
    [
        None,
        _verdict(intent="refactor"),
        _verdict(domain=""),
        _verdict(domain=None),
        _verdict(confidence=True),
        _verdict(confidence="0.9"),
    ],
)
def test_a_malformed_verdict_is_a_fault_not_a_route(verdict):
    decision = routing.classify_task("anything", classifier=_Classifier(verdict=verdict))

    assert decision.route == routing.ROUTE_ARCHITECT
    assert decision.fault == routing.CLASSIFIER_FAULT


# -- the route follows the verdict ------------------------------------------------------------


def test_plain_implementation_routes_to_a_coder():
    decision = routing.classify_task(
        "add a login endpoint", classifier=_Classifier(verdict=_verdict(domain=laya.DOMAIN_API))
    )

    assert decision.route == routing.ROUTE_CODER
    assert decision.complexity == routing.COMPLEXITY_LOW
    assert decision.fault == ""
    assert decision.intent == laya.INTENT_CODE


def test_an_administrative_verdict_routes_to_the_architect():
    decision = routing.classify_task(
        "explain the architecture",
        classifier=_Classifier(verdict=_verdict(intent=laya.INTENT_ADMIN, domain=laya.DOMAIN_DOCS)),
    )

    assert decision.route == routing.ROUTE_ARCHITECT
    assert decision.complexity == routing.COMPLEXITY_HIGH


def test_a_core_domain_verdict_routes_to_the_architect():
    decision = routing.classify_task(
        "wire the auth core",
        classifier=_Classifier(verdict=_verdict(domain=laya.DOMAIN_CORE)),
    )

    assert decision.route == routing.ROUTE_ARCHITECT
    assert decision.complexity == routing.COMPLEXITY_HIGH


def test_an_explicit_tag_is_handed_to_the_classifier_as_context():
    classifier = _Classifier(verdict=_verdict(domain=laya.DOMAIN_UI))

    routing.classify_task("do the needful", tag="FE", classifier=classifier)

    assert classifier.calls == [("do the needful", {"tag": "FE"})]


def test_no_tag_asks_the_classifier_without_context():
    classifier = _Classifier(verdict=_verdict())

    routing.classify_task("add a login endpoint", classifier=classifier)

    assert classifier.calls == [("add a login endpoint", None)]


def test_route_for_complexity_is_the_single_mapping():
    assert routing.route_for_complexity(routing.COMPLEXITY_HIGH) == routing.ROUTE_ARCHITECT
    assert routing.route_for_complexity(routing.COMPLEXITY_LOW) == routing.ROUTE_CODER
    assert routing.route_for_complexity("") == routing.ROUTE_CODER


# -- the decision is auditable ----------------------------------------------------------------


def test_a_decision_is_appended_to_the_ledger_with_its_evidence(tmp_path):
    db_path = _state_db(tmp_path)
    decision = routing.classify_task(
        "wire the auth core", classifier=_Classifier(verdict=_verdict(domain=laya.DOMAIN_CORE))
    )

    decision_id = routing.record_decision(db_path, decision, plan_id="PLAN", task_id="task-1")

    rows = telemetry.routing_decisions_for_task(db_path, "PLAN", "task-1")
    assert len(rows) == 1
    row = rows[0]
    assert row["decision_id"] == decision_id
    assert row["route"] == routing.ROUTE_ARCHITECT
    assert row["complexity"] == routing.COMPLEXITY_HIGH
    assert row["domain"] == laya.DOMAIN_CORE
    assert row["fault"] == ""
    assert row["engine"] == "injected"


def test_a_fault_is_appended_to_the_ledger(tmp_path):
    db_path = _state_db(tmp_path)
    decision = routing.classify_task("anything", classifier=_Classifier(error=ValueError("nope")))

    routing.record_decision(db_path, decision, plan_id="PLAN", task_id="task-2")

    rows = telemetry.routing_decisions_for_task(db_path, "PLAN", "task-2")
    assert len(rows) == 1
    assert rows[0]["route"] == routing.ROUTE_ARCHITECT
    assert rows[0]["fault"] == routing.CLASSIFIER_FAULT
