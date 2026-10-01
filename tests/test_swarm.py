"""Phase 6 Batch 2/3: the reactive swarm -- ``tick()``, callbacks, and the human gate.

    .\\venv\\Scripts\\python.exe -m pytest tests/test_swarm.py -n0 -q

Real processes, a real SQLite file, real MCP sessions inside each worker. Only the model's answer
is scripted (``tests.stub_planner``), because a worker runs in another interpreter and a canned
artifact has to travel as a reference rather than as a closure.

The swarm has no loop: a ``tick()`` dispatches, and a worker's completion arrives on the parent's
thread through a ``Future`` callback -- which is the only thread that can emit to the UI. These
tests therefore drive ticks and wait for the pool, rather than iterating a generator.
"""

from __future__ import annotations

import os
import sqlite3
import time
from unittest import mock

import pytest

from orchestration.model_router import TIERS, TOP_TIER
from orchestration.scheduler import get_executable_node_ids, get_executable_nodes
from orchestration.swarm import NodeOutcome, Swarm, verification_refusal
from storage.db import PlanDAG, TaskNode
from tests import stub_worker

# System 2 has to look configured for the planner to reach its completer at all. Nothing is
# fetched: the scripted completer answers, so the endpoint is never contacted.
SYSTEM2_ENV = {
    "OPENAI_API_KEY": "test-key",
    "OPENAI_BASE_URL": "https://example.invalid/v1",
    "ALETH_SYSTEM2": "1",
    "ALETH_EMBEDDER": "hashing",
}

PLANNER_SPEC = "tests.stub_planner:scripted"


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """A real plan database on a real path, plus a workspace directory for the workers.

    The kernel's global store is pointed at this directory, which is the production invariant: the
    swarm's ``db_path`` *is* the active plan's database, so the Artifact Gate's writes and the
    workers' reads are the same file.
    """
    for key, value in SYSTEM2_ENV.items():
        monkeypatch.setenv(key, value)

    root = tmp_path / "workspace"
    root.mkdir()

    from storage.db import get_store, reset_stores
    from tools import workspace as workspace_module

    monkeypatch.setattr(workspace_module, "PLAN_DIR", str(tmp_path))
    monkeypatch.setattr(workspace_module, "PROJECT_DIR", str(root))
    monkeypatch.setattr(workspace_module, "ACTIVE_PLAN_FILE", "PLAN.md")
    reset_stores()
    store = get_store()

    def build(tasks):
        """``tasks`` is ``(id, status, [dependencies])`` in document order."""
        store.save_dag(
            PlanDAG(
                plan_id="PLAN",
                title="T",
                order=[task[0] for task in tasks],
                nodes={
                    task[0]: TaskNode(
                        id=task[0], plan_id="PLAN", title=task[0],
                        status=task[1], dependencies=list(task[2]),
                    )
                    for task in tasks
                },
            ),
            sync_edges=True,
        )

    yield store, build, store.path, str(root)
    reset_stores()


def _wait_idle(swarm, timeout=90):
    """Wait until every dispatched node has finished *and* its outcome has been applied.

    ``in_flight`` alone is not enough: a callback clears its node and then commits, so watching only
    the in-flight set would let a test proceed while an outcome was still being written. The callback
    itself calls ``tick()``, so a completion that unblocks more work dispatches it without help.
    """
    deadline = time.time() + timeout
    while time.time() < deadline and not swarm.settled():
        time.sleep(0.05)
    assert swarm.settled(), "workers did not settle"


def _wait_for_status(store, node_id, status, timeout=90):
    """Poll the *stored* state until the node reaches ``status``; report whether it did.

    Reading the database rather than the swarm's in-memory sets is deliberate. An automatic retry is
    re-dispatched from inside a worker callback, so between "outcome applied" and "re-dispatched"
    the swarm looks settled for an instant; the stored status has no such window.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if store.get_dag("PLAN").nodes[node_id].status == status:
            return True
        time.sleep(0.05)
    return False


def _swarm(workspace, **kwargs):
    _, _, db_path, root = workspace
    # ``planner_spec`` is overridable so a test can point a node at a planner that fails.
    planner_spec = kwargs.pop("planner_spec", PLANNER_SPEC)
    return Swarm(db_path=db_path, plan_id="PLAN", workspace_dir=root,
                 planner_spec=planner_spec, **kwargs)


def _dispatch_node(store, node_id):
    """The node exactly as the dispatcher sees it: through ``get_executable_nodes``."""
    connection = sqlite3.connect(store.path)
    connection.row_factory = sqlite3.Row
    try:
        nodes = {node["id"]: node for node in get_executable_nodes(connection, "PLAN", max_retries=9)}
    finally:
        connection.close()
    return nodes[node_id]


class TestReactiveTick:
    def test_a_tick_dispatches_every_unblocked_root(self, workspace):
        """The headline: independent branches go out in one tick, not one at a time."""
        _, build, _, _ = workspace
        build([("a", "pending", []), ("b", "pending", []), ("c", "pending", [])])

        swarm = _swarm(workspace, max_workers=3)
        try:
            assert sorted(swarm.tick()) == ["a", "b", "c"]
            _wait_idle(swarm)
        finally:
            swarm.close()

        assert sorted(o.node_id for o in (swarm.outcome_for("a"), swarm.outcome_for("b"), swarm.outcome_for("c"))) == ["a", "b", "c"]
        assert all(swarm.outcome_for(node).ok for node in ("a", "b", "c"))

    def test_a_tick_does_not_redispatch_a_node_in_flight(self, workspace):
        """De-duplication is the pool's own bookkeeping, not a database flag."""
        _, build, _, _ = workspace
        build([("a", "pending", [])])
        swarm = _swarm(workspace, max_workers=1)
        try:
            assert swarm.tick() == ["a"]
            assert swarm.tick() == [], "a node already running must not be submitted again"
            _wait_idle(swarm)
        finally:
            swarm.close()

    def test_a_chain_dispatches_only_its_head(self, workspace):
        _, build, _, _ = workspace
        build([("a", "pending", []), ("b", "pending", ["a"]), ("c", "pending", ["b"])])

        swarm = _swarm(workspace, max_workers=3)
        try:
            assert swarm.tick() == ["a"]
            _wait_idle(swarm)
            # b is blocked on a, and committing a's artifact halts it in ``planned`` -- which is
            # not a completion, so nothing else becomes runnable.
            assert swarm.tick() == []
        finally:
            swarm.close()

    def test_a_diamond_releases_both_branches_after_approval(self, workspace):
        store, build, _, root = workspace
        build([
            ("root", "pending", []),
            ("left", "pending", ["root"]),
            ("right", "pending", ["root"]),
            ("join", "pending", ["left", "right"]),
        ])

        swarm = _swarm(workspace, max_workers=2)
        try:
            assert swarm.tick() == ["root"]
            _wait_idle(swarm)
            assert swarm.outcome_for("root").ok

            # The artifact is yielded, not applied: the node is planned, waiting for a human.
            assert swarm.tick() == []

            _approve_and_apply("root", root)

            assert sorted(swarm.tick()) == ["left", "right"]
            _wait_idle(swarm)
        finally:
            swarm.close()

    def test_the_worker_does_not_apply_the_artifact(self, workspace):
        """The worker produces a payload; applying it is the human's decision."""
        _, build, _, root = workspace
        build([("a", "pending", [])])
        swarm = _swarm(workspace, max_workers=1)
        try:
            swarm.tick()
            _wait_idle(swarm)
        finally:
            swarm.close()

        assert os.listdir(root) == []
        assert swarm.outcome_for("a").ok

    def test_a_failed_worker_is_reported_not_retried(self, workspace):
        _, build, _, _ = workspace
        build([("a", "pending", []), ("b", "pending", [])])
        swarm = _swarm(workspace, max_workers=1,
                       planner_spec="tests.stub_planner:does_not_exist")
        try:
            assert sorted(swarm.tick()) == ["a", "b"]
            _wait_idle(swarm)
            # The failure is in the outcome, and the tick does not resubmit either node.
            assert "AttributeError" in swarm.outcome_for("a").error or "callable" in swarm.outcome_for("a").error
            assert swarm.tick() == []
        finally:
            swarm.close()

    def test_committing_halts_the_node_for_approval(self, workspace):
        store, build, _, _ = workspace
        build([("a", "pending", [])])
        swarm = _swarm(workspace, max_workers=1)
        try:
            swarm.tick()
            _wait_idle(swarm)
        finally:
            swarm.close()

        state = store.get_dag("PLAN").nodes["a"].status
        assert state == "planned"
        assert store.get_artifact("PLAN", "a") is not None

    def test_arguments_are_validated(self, workspace):
        _, _, db_path, root = workspace
        with pytest.raises(ValueError, match="at least 1"):
            Swarm(db_path=db_path, plan_id="PLAN", workspace_dir=root, max_workers=0)
        with pytest.raises(ValueError, match="at least 1"):
            Swarm(db_path=db_path, plan_id="PLAN", workspace_dir=root, max_retries=0)


class TestRejectionThroughTheDatabase:
    """A rejection is persisted, so a restart cannot resurrect the loop."""

    def test_the_critique_is_persisted_and_reaches_the_next_prompt(self, workspace, tmp_path, monkeypatch):
        """The retry must ask a *different* question, or it is compute spent going nowhere."""
        store, build, _, _ = workspace
        build([("a", "pending", [])])
        log = tmp_path / "prompts.log"
        monkeypatch.setenv("STUB_PROMPT_LOG", str(log))

        swarm = _swarm(workspace, max_workers=1, max_retries=1)
        try:
            swarm.tick()
            _wait_idle(swarm)
            assert swarm.reject("a", "the endpoint needs a return type") is True

            _wait_idle(swarm)
            prompts = log.read_text(encoding="utf-8")
            assert prompts.count("---PROMPT---") == 2, "the node was planned twice"
            assert "the endpoint needs a return type" in prompts
            assert "REJECTED BY REVIEW" in prompts
            first = prompts.split("---PROMPT---")[1]
            assert "REJECTED BY REVIEW" not in first
        finally:
            swarm.close()

        # The count and the note are in the database, not in the swarm's memory.
        assert store.rejection_state("PLAN", "a") == {
            "attempts": 1, "feedback": ["the endpoint needs a return type"],
        }

    def test_a_maxed_out_node_is_excluded_from_dispatch_but_stays_pending(self, workspace):
        """The guard, end to end: the node is dead to the pool without being mislabelled."""
        store, build, _, _ = workspace
        build([("a", "pending", [])])
        # A budget of 1: the node runs, is rejected once (that retry is allowed), and a second
        # rejection exhausts it.
        swarm = _swarm(workspace, max_workers=1, max_retries=1)
        try:
            swarm.tick()
            _wait_idle(swarm)
            assert swarm.reject("a", "no") is True           # attempts 1 <= 1: the retry runs
            _wait_idle(swarm)
            assert swarm.reject("a", "still no") is False     # attempts 2 > 1: budget spent
            assert swarm.tick() == []

            assert swarm.abandoned() == ["a"]
            # It is still a legitimate pending node in the graph -- the state kernel is not told a
            # lie to express the swarm's retry policy.
            assert store.get_dag("PLAN").nodes["a"].status == "pending"
            connection = sqlite3.connect(store.path)
            connection.row_factory = sqlite3.Row
            try:
                assert get_executable_node_ids(connection, "PLAN", max_retries=1) == []
                assert get_executable_node_ids(connection, "PLAN", max_retries=9) == ["a"]
            finally:
                connection.close()
        finally:
            swarm.close()

    def test_the_retry_state_survives_a_restart(self, workspace):
        """A fresh swarm -- a new process, in effect -- still refuses to re-dispatch."""
        store, build, _, root = workspace
        build([("a", "pending", [])])

        first = _swarm(workspace, max_workers=1, max_retries=1)
        try:
            first.tick()
            _wait_idle(first)
            first.reject("a", "no")
            _wait_idle(first)
            first.reject("a", "still no")
        finally:
            first.close()

        # A brand-new swarm holds no history, and the database still bounds it.
        second = _swarm(workspace, max_workers=1, max_retries=1)
        try:
            assert second.tick() == []
            assert second.abandoned() == ["a"]
        finally:
            second.close()


class TestRoutedDispatch:
    """Phase 7 wired into the tick: the dispatch loop assigns a model from the node's own state.

    The route is read from values the dispatch query already hydrated -- no scalar SELECT in the
    loop -- and it travels across the process boundary in the role descriptor, where the worker
    applies it verbatim.
    """

    @staticmethod
    def _node(store, node_id):
        """The node exactly as the dispatcher sees it: through ``get_executable_nodes``."""
        return _dispatch_node(store, node_id)

    def test_the_descriptor_carries_the_routers_route_for_the_node(self, workspace):
        store, build, _, _ = workspace
        build([("a", "pending", [])])
        store.record_assessment("PLAN", "a", 5, ["shell"])

        swarm = _swarm(workspace, max_workers=1)
        try:
            descriptor = swarm._descriptor_for(self._node(store, "a"))
        finally:
            swarm.close()

        assert descriptor["model"] == TIERS[TOP_TIER]["route"]
        assert descriptor["routing"]["tier"] == TIERS[TOP_TIER]["tier"]
        # ``provider`` is split out of the route for callers that need the parts.
        assert descriptor["routing"]["provider"] == TIERS[TOP_TIER]["route"].partition(":")[0]
        assert descriptor["routing"]["complexity_score"] == 5

    def test_a_trivial_node_is_not_routed_to_the_strongest_model(self, workspace):
        store, build, _, _ = workspace
        build([("a", "pending", [])])
        store.record_assessment("PLAN", "a", 1, [])

        swarm = _swarm(workspace, max_workers=1)
        try:
            descriptor = swarm._descriptor_for(self._node(store, "a"))
        finally:
            swarm.close()

        assert descriptor["model"] == TIERS[0]["route"]
        assert descriptor["model"] != TIERS[TOP_TIER]["route"]

    def test_a_rejection_escalates_the_route(self, workspace):
        store, build, _, _ = workspace
        build([("a", "pending", [])])
        store.record_assessment("PLAN", "a", 1, [])

        swarm = _swarm(workspace, max_workers=1, max_retries=5)
        try:
            before = swarm._descriptor_for(self._node(store, "a"))
            store.record_rejection("PLAN", "a", "no")
            after = swarm._descriptor_for(self._node(store, "a"))
        finally:
            swarm.close()

        assert before["routing"]["tier"] == TIERS[0]["tier"]
        assert after["routing"]["tier"] == TIERS[1]["tier"]
        assert after["model"] == TIERS[1]["route"]

    def test_the_critique_is_hydrated_by_the_dispatch_query(self, workspace):
        """The retry prompt's input comes from the one query, not a second read per node."""
        store, build, _, _ = workspace
        build([("a", "pending", [])])
        store.record_rejection("PLAN", "a", "needs a return type")

        swarm = _swarm(workspace, max_workers=1, max_retries=5)
        try:
            descriptor = swarm._descriptor_for(self._node(store, "a"))
        finally:
            swarm.close()

        assert descriptor["rejection_feedback"] == ["needs a return type"]
        assert descriptor["routing"]["rejection_attempts"] == 1

    def test_the_worker_hands_the_routed_model_to_the_planner(self, workspace):
        """The worker has no authority to guess: it uses the route the descriptor carries."""
        from orchestration.worker import execute_node

        store, build, _, _ = workspace
        build([("a", "pending", [])])
        store.record_assessment("PLAN", "a", 5, [])

        swarm = _swarm(workspace, max_workers=1)
        try:
            descriptor = swarm._descriptor_for(self._node(store, "a"))
        finally:
            swarm.close()

        with mock.patch("orchestration.workflow.planner.plan_task", return_value="artifact") as plan_task, \
                mock.patch("orchestration.mcp_session.MCPSessionContext"):
            execute_node("PLAN::a", descriptor, store.path)

        assert plan_task.call_args.kwargs["model"] == TIERS[TOP_TIER]["route"]

    def test_the_worker_hands_the_routed_endpoint_to_the_planner(self, workspace):
        """The tier's base_url and key travel with its model, and are used verbatim."""
        from core import config as boot_config
        from orchestration.worker import execute_node

        store, build, _, _ = workspace
        build([("a", "pending", [])])
        store.record_assessment("PLAN", "a", 5, [])

        boot_config.set_active_config(boot_config.BootloaderConfig(
            tier_0=boot_config.TierConfig(
                provider="openai", model_name="small", base_url="https://tier-zero.invalid/v1"
            ),
            tier_2=boot_config.TierConfig(
                provider="openai", model_name="big",
                base_url="https://tier-two.invalid/v1", api_key="sk-tier-two",
            ),
        ))
        try:
            swarm = _swarm(workspace, max_workers=1)
            try:
                descriptor = swarm._descriptor_for(_dispatch_node(store, "a"))
            finally:
                swarm.close()
        finally:
            boot_config.reset_config()

        assert descriptor["model"] == "openai:big"
        assert descriptor["routing"]["base_url"] == "https://tier-two.invalid/v1"

        with mock.patch("orchestration.workflow.planner.plan_task", return_value="artifact") as plan_task, \
                mock.patch("orchestration.mcp_session.MCPSessionContext"):
            execute_node("PLAN::a", descriptor, store.path)

        assert plan_task.call_args.kwargs["model"] == "openai:big"
        assert plan_task.call_args.kwargs["base_url"] == "https://tier-two.invalid/v1"
        assert plan_task.call_args.kwargs["api_key"] == "sk-tier-two"


    def test_a_node_the_fleet_cannot_run_fails_instead_of_dispatching(self, workspace):
        """The refusal path: no weak model, no retry -- the node is failed with the reason."""
        from core import config as boot_config

        store, build, _, _ = workspace
        build([("a", "pending", [])])
        store.record_assessment("PLAN", "a", 5, ["fs"])  # top-tier work

        boot_config.set_active_config(boot_config.BootloaderConfig(
            tier_0=boot_config.TierConfig(
                provider="openai", model_name="small", base_url="https://example.invalid/v1"
            ),
        ))

        swarm = _swarm(workspace, max_workers=1)
        try:
            assert swarm.tick() == [], "an unroutable node must not reach a worker"
        finally:
            swarm.close()
            boot_config.reset_config()

        node = store.get_dag("PLAN").nodes["a"]
        assert node.status == "failed"
        assert any("Unroutable" in note for note in node.details)


class TestRoutingGateway:
    """Phase 19: the classifier picks the brain, and the choice is written to the ledger.

    Every test injects its classifier, so these run without a checkpoint and without the ambient
    environment: the fake answers with a fixed verdict, and the descriptor and the
    ``routing_decisions`` row are asserted together -- a decision that is not auditable is not a
    decision the engine may claim to have made.
    """

    @staticmethod
    def _classifier(domain, intent=None):
        from agents import laya

        verdict = laya.Verdict(
            intent=intent or laya.INTENT_CODE,
            domain=domain,
            coder=laya.CODER_STANDARD,
            confidence=0.9,
            reasons=("test classifier",),
        )
        return lambda text, context=None: verdict

    @staticmethod
    def _build(workspace):
        store, build, db_path, _ = workspace
        build([("a", "pending", [])])
        return store, db_path

    def test_a_core_node_routes_to_the_architect_and_is_logged(self, workspace):
        from agents.architect import ARCHITECT_ROLE
        from storage import telemetry

        store, db_path = self._build(workspace)
        swarm = _swarm(workspace, max_workers=1, classifier=self._classifier("CORE"))
        try:
            descriptor = swarm._descriptor_for(_dispatch_node(store, "a"))
        finally:
            swarm.close()

        assert descriptor["name"] == ARCHITECT_ROLE.name
        assert descriptor["routing"]["classifier"]["route"] == "architect"
        rows = telemetry.routing_decisions_for_task(db_path, "PLAN", "a")
        assert len(rows) == 1
        assert rows[0]["route"] == "architect"
        assert rows[0]["fault"] == ""

    def test_a_plain_node_routes_to_the_standard_coder(self, workspace):
        from agents import laya
        from orchestration import routing

        store, _ = self._build(workspace)
        swarm = _swarm(workspace, max_workers=1, classifier=self._classifier(laya.DOMAIN_GENERAL))
        try:
            descriptor = swarm._descriptor_for(_dispatch_node(store, "a"))
        finally:
            swarm.close()

        assert descriptor["name"] == laya.CODER_STANDARD
        assert descriptor["routing"]["classifier"]["route"] == routing.ROUTE_CODER

    def test_a_faulting_classifier_routes_to_the_architect_and_logs_the_fault(self, workspace):
        from agents.architect import ARCHITECT_ROLE
        from storage import telemetry

        store, db_path = self._build(workspace)

        def boom(text, context=None):
            raise RuntimeError("classifier unavailable")

        swarm = _swarm(workspace, max_workers=1, classifier=boom)
        try:
            descriptor = swarm._descriptor_for(_dispatch_node(store, "a"))
        finally:
            swarm.close()

        assert descriptor["name"] == ARCHITECT_ROLE.name
        assert descriptor["routing"]["classifier"]["fault"] == "CLASSIFIER_FAULT"
        rows = telemetry.routing_decisions_for_task(db_path, "PLAN", "a")
        assert rows[0]["fault"] == "CLASSIFIER_FAULT"

    def test_the_gateway_does_not_take_over_the_model_choice(self, workspace):
        """The gateway owns the *agent*; Phase 7's router still owns the model tier."""
        store, _ = self._build(workspace)
        store.record_assessment("PLAN", "a", 5, [])

        swarm = _swarm(workspace, max_workers=1, classifier=self._classifier("CORE"))
        try:
            descriptor = swarm._descriptor_for(_dispatch_node(store, "a"))
        finally:
            swarm.close()

        assert descriptor["model"] == TIERS[TOP_TIER]["route"]

    def test_a_ledger_write_failure_does_not_block_dispatch(self, workspace, monkeypatch, capsys):
        """A routing row that cannot be written is reported, never fatal.

        The decision still travels in the descriptor, so the node is dispatched with the correct
        brain; only the audit row is lost, and it is announced on stderr rather than swallowed.
        """
        from agents.architect import ARCHITECT_ROLE
        from orchestration import routing

        store, _ = self._build(workspace)

        def unwritable(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(routing, "record_decision", unwritable)

        swarm = _swarm(workspace, max_workers=1, classifier=self._classifier("CORE"))
        try:
            descriptor = swarm._descriptor_for(_dispatch_node(store, "a"))
        finally:
            swarm.close()

        assert descriptor["name"] == ARCHITECT_ROLE.name
        assert descriptor["routing"]["classifier"]["route"] == "architect"
        assert "routing decision not recorded" in capsys.readouterr().err


class TestCapabilityDiet:
    """Phase 7.4: the node's declaration decides the tool set, and nothing else does.

    The declaration travels in the descriptor; the worker's session is scoped to exactly that; and
    a node that declared nothing is offered nothing. The last two tests are the end-to-end proof --
    the tool list is recorded from inside the worker process, which is the only place it can be
    seen for real.
    """

    def test_the_descriptor_carries_the_nodes_capabilities(self, workspace):
        store, build, _, _ = workspace
        build([("a", "pending", [])])
        store.record_assessment("PLAN", "a", 3, ["fs", "ast"])

        swarm = _swarm(workspace, max_workers=1)
        try:
            descriptor = swarm._descriptor_for(_dispatch_node(store, "a"))
        finally:
            swarm.close()

        assert descriptor["required_capabilities"] == ["fs", "ast"]

    def test_a_node_that_declared_nothing_carries_an_empty_list(self, workspace):
        """The key is present and empty: the child must not read "absent" as "everything"."""
        store, build, _, _ = workspace
        build([("a", "pending", [])])

        swarm = _swarm(workspace, max_workers=1)
        try:
            descriptor = swarm._descriptor_for(_dispatch_node(store, "a"))
        finally:
            swarm.close()

        assert descriptor["required_capabilities"] == []

    def test_the_worker_scopes_its_session_to_the_declaration(self, workspace):
        from orchestration.worker import execute_node

        store, build, _, _ = workspace
        build([("a", "pending", [])])
        store.record_assessment("PLAN", "a", 3, ["fs"])

        swarm = _swarm(workspace, max_workers=1)
        try:
            descriptor = swarm._descriptor_for(_dispatch_node(store, "a"))
        finally:
            swarm.close()

        with mock.patch("orchestration.workflow.planner.plan_task", return_value="artifact"), \
                mock.patch("orchestration.mcp_session.MCPSessionContext") as session:
            execute_node("PLAN::a", descriptor, store.path)

        assert session.call_args.kwargs["capabilities"] == ["fs"]

    def test_the_worker_hands_its_nodes_role_to_the_planning_pass(self, workspace):
        """The planning pass binds the node's own role from the live session, not a constant."""
        from orchestration.worker import execute_node

        store, build, _, _ = workspace
        build([("a", "pending", [])])

        swarm = _swarm(workspace, max_workers=1)
        try:
            descriptor = swarm._descriptor_for(_dispatch_node(store, "a"))
        finally:
            swarm.close()

        assert descriptor["name"] in ("coder-deep", "coder-standard")

        with mock.patch("orchestration.workflow.planner.plan_task", return_value="artifact") as plan_task, \
                mock.patch("orchestration.mcp_session.MCPSessionContext"):
            execute_node("PLAN::a", descriptor, store.path)

        assert plan_task.call_args.kwargs["role"] == descriptor["name"]

    def test_a_worker_given_no_capabilities_boots_with_no_tools(self, workspace, tmp_path, monkeypatch):
        """The acceptance criterion: an empty declaration means zero tools, end to end."""
        _, build, _, _ = workspace
        build([("a", "pending", [])])  # never assessed: the schema default is an empty list
        log = tmp_path / "tools.log"
        monkeypatch.setenv("STUB_TOOLS_LOG", str(log))

        swarm = _swarm(workspace, max_workers=1)
        try:
            assert swarm.tick() == ["a"]
            _wait_idle(swarm)
        finally:
            swarm.close()

        assert log.read_text(encoding="utf-8").splitlines() == ["0:"], \
            "a node that declared nothing was offered a tool"

    def test_a_worker_given_a_capability_boots_with_exactly_those_tools(self, workspace, tmp_path, monkeypatch):
        """And the other half: the declaration is what the model actually receives."""
        store, build, _, _ = workspace
        build([("a", "pending", [])])
        store.record_assessment("PLAN", "a", 3, ["fs"])
        log = tmp_path / "tools.log"
        monkeypatch.setenv("STUB_TOOLS_LOG", str(log))

        swarm = _swarm(workspace, max_workers=1)
        try:
            assert swarm.tick() == ["a"]
            _wait_idle(swarm)
        finally:
            swarm.close()

        # The filesystem server's agent-visible tools, and the engine's overwrite is still withheld.
        assert log.read_text(encoding="utf-8").splitlines() == ["2:create_file,read_file"]


class TestCapabilityBirth:
    """Phase 7.4, upstreamed: the node is born knowing what it needs.

    The declaration is made where the node is created -- the scaffolded DAG, the plan document,
    the directive -- so the very first dispatch is equipped. The worker never decides.
    """

    @staticmethod
    def _scaffold(workspace):
        from orchestration import plan_session

        plan_session.scaffold_plan_file("PLAN.md", "Demo idea")
        return workspace[0]

    def test_a_scaffolded_plan_is_born_with_its_capabilities(self, workspace):
        store = self._scaffold(workspace)
        dag = store.get_dag("PLAN")

        assert dag.nodes["task-3"].required_capabilities == ["fs", "ast"]
        assert dag.nodes["task-6"].required_capabilities == ["fs", "ast", "exec"]
        # An empty declaration is a statement, not a gap: task-1 writes no code.
        assert dag.nodes["task-1"].required_capabilities == []

    def test_the_declaration_survives_an_ordinary_status_write(self, workspace):
        """The projection does not carry the field, so a status save must not erase it."""
        from tools.plan_state import update_plan_task_status

        store = self._scaffold(workspace)
        update_plan_task_status("task-3", "in_progress")

        assert store.get_dag("PLAN").nodes["task-3"].required_capabilities == ["fs", "ast"]

    def test_an_adhoc_directive_is_born_with_the_architects_declaration(self, workspace):
        from orchestration.workflow import planner

        store = self._scaffold(workspace)
        node = planner.ensure_task_for_directive(
            plan_id="PLAN", task=None, title="Fix the endpoint", files=["app.py"]
        )

        assert store.get_dag("PLAN").nodes[node["id"]].required_capabilities == ["fs", "ast"]

    def test_the_very_first_dispatch_is_equipped(self, workspace, tmp_path, monkeypatch):
        """The headline: no starvation pass, no wasted retry. Tick one is already armed."""
        store = self._scaffold(workspace)
        log = tmp_path / "tools.log"
        monkeypatch.setenv("STUB_TOOLS_LOG", str(log))

        swarm = _swarm(workspace, max_workers=1)
        try:
            # task-1 is completed and the rest are blocked on their blockers, so task-2 -- the
            # first node with real work -- is the only runnable root.
            assert swarm.tick() == ["task-2"]
            _wait_idle(swarm)
        finally:
            swarm.close()

        offered = log.read_text(encoding="utf-8").splitlines()[0]
        count, _, names = offered.partition(":")
        offered_names = names.split(",")
        assert count == "4", f"task-2 declared fs+exec; the worker was offered {offered!r}"
        assert "read_file" in offered_names
        assert "execute_command" in offered_names


class TestSystemFailureSegregation:
    """Phase 9: a machine fault is not the work being wrong.

    A broken pipe or a timeout is the *environment* misbehaving, so the node is retried without a
    human -- the reviewer is a gatekeeper for quality, not a babysitter for the process pool. A
    contract violation is the *work* misbehaving, and is failed at once because a retry would only
    reproduce it. Both paths run through a real worker process; only the worker's outcome is stubbed.
    """

    def test_a_system_failure_is_retried_up_to_the_limit_then_fails(self, workspace, tmp_path, monkeypatch):
        """The auto-retry loop: the machine gets its budget, then the branch is abandoned."""
        store, build, _, _ = workspace
        build([("a", "pending", [])])
        log = tmp_path / "attempts.log"
        monkeypatch.setenv("STUB_WORKER_LOG", str(log))

        swarm = _swarm(workspace, max_workers=1, max_system_retries=3,
                       worker=stub_worker.machine_fault)
        try:
            assert swarm.tick() == ["a"]
            assert _wait_for_status(store, "a", "failed"), "the machine fault never exhausted its budget"
        finally:
            swarm.close()

        # One original attempt plus two automatic retries: the budget decided, not a human. The
        # count is read from the child's own log, which is the only witness across the boundary.
        assert log.read_text(encoding="utf-8").splitlines() == ["PLAN::a"] * 3
        assert store.system_failure_state("PLAN", "a")["failures"] == 3
        assert store.get_dag("PLAN").nodes["a"].status == "failed"
        # The human never spoke, so no rejection was recorded: the two counters are separate budgets.
        assert store.rejection_state("PLAN", "a") == {"attempts": 0, "feedback": []}

    def test_a_work_failure_fails_at_once_without_a_retry(self, workspace, tmp_path, monkeypatch):
        """A contract violation is not the environment's fault, so it is not retried."""
        store, build, _, _ = workspace
        build([("a", "pending", [])])
        log = tmp_path / "attempts.log"
        monkeypatch.setenv("STUB_WORKER_LOG", str(log))

        swarm = _swarm(workspace, max_workers=1, max_system_retries=3,
                       worker=stub_worker.work_fault)
        try:
            assert swarm.tick() == ["a"]
            _wait_idle(swarm)
            assert swarm.tick() == []
        finally:
            swarm.close()

        # Exactly one dispatch: retrying the same broken contract would reproduce it.
        assert log.read_text(encoding="utf-8").splitlines() == ["PLAN::a"]
        assert store.system_failure_state("PLAN", "a")["failures"] == 0
        assert store.get_dag("PLAN").nodes["a"].status == "failed"


class TestLifecycleAndTeardown:
    """The explicit lifecycle: drain in-flight work, then close. Nothing may raise in teardown.

    A worker's completion is delivered on the pool's own thread, so nothing above it can catch a
    raise. A swarm whose database disappears underneath it -- which is exactly what a test's
    temporary directory does -- must therefore *report* a failure, not spray an unhandled
    traceback into teardown.
    """

    def test_shutdown_drains_then_closes(self, workspace):
        _, build, _, _ = workspace
        build([("a", "pending", [])])
        swarm = _swarm(workspace, max_workers=1)

        assert swarm.tick() == ["a"]
        assert swarm.shutdown() is True
        assert swarm.settled()
        # Closed: a later tick is inert rather than dispatching into a shut-down pool.
        assert swarm.tick() == []

    def test_drain_waits_for_a_dispatched_node(self, workspace):
        _, build, _, _ = workspace
        build([("a", "pending", [])])
        swarm = _swarm(workspace, max_workers=1)
        try:
            swarm.tick()
            assert swarm.drain(timeout=60) is True
            assert swarm.settled()
        finally:
            swarm.close()

    def test_a_completion_after_the_database_is_gone_does_not_raise(self, workspace, tmp_path):
        """The teardown race, pinned: the callback reports, it does not raise into the pool."""
        from concurrent.futures import Future

        _, build, _, _ = workspace
        build([("a", "pending", [])])
        swarm = _swarm(workspace, max_workers=1)
        try:
            # The database is no longer reachable, as it is once a test's directory is unlinked.
            swarm.db_path = str(tmp_path / "gone" / "nope.db")
            future = Future()
            future.set_exception(RuntimeError("the worker died"))

            swarm._on_worker_complete("a", future)  # must not raise

            assert swarm.busy == 0
            assert swarm.outcome_for("a").error is not None
        finally:
            swarm.close()


class TestEngineVerificationGate:
    """Phase 7.5: a task that ran commands is not completed without a zero-exit test receipt.

    The gate is the engine's, not a model's opinion. The distinction it turns on is that an
    inconclusive check -- no test file, a suite that could not be collected -- is the *absence* of
    evidence, and reading that as a pass is the false-success failure the gate exists to stop.
    """

    @staticmethod
    def _receipt(filename="test_a.py", returncode=0, verdict="passed"):
        return {"filename": filename, "returncode": returncode, "verdict": verdict}

    def test_a_node_that_ran_nothing_is_unaffected(self):
        """Without ``exec`` there is nothing to verify, so the gate stays out of the way."""
        assert verification_refusal({"tests": [], "verdict": "none"}, []) is None
        assert verification_refusal({"tests": [], "verdict": "none"}, ["fs", "ast"]) is None

    def test_an_exec_node_with_no_receipt_is_refused(self):
        refusal = verification_refusal({"tests": [], "verdict": "none"}, ["exec"])
        assert refusal is not None and "no test receipt" in refusal

    def test_an_exec_node_with_a_zero_exit_receipt_passes(self):
        verdict = {"tests": [self._receipt()], "verdict": "passed", "summary": "1 passed"}
        assert verification_refusal(verdict, ["exec", "fs"]) is None

    def test_an_exec_node_whose_only_receipts_failed_is_refused(self):
        verdict = {
            "tests": [self._receipt(returncode=1, verdict="failed")],
            "verdict": "failed", "summary": "1 failed",
        }
        refusal = verification_refusal(verdict, ["exec"])
        assert refusal is not None and "no test receipt exited 0" in refusal

    def test_one_failing_file_refuses_even_when_a_sibling_passed(self):
        verdict = {
            "tests": [self._receipt("t1.py", 0, "passed"), self._receipt("t2.py", 1, "failed")],
            "verdict": "failed", "summary": "1 passed, 1 failed",
        }
        assert verification_refusal(verdict, ["exec"]) is not None

    def test_an_uncollectable_suite_is_refused_not_read_as_a_pass(self):
        verdict = {
            "tests": [self._receipt(returncode=2, verdict="error")],
            "verdict": "error", "summary": "1 error",
        }
        assert verification_refusal(verdict, ["exec"]) is not None

    def test_the_receipt_is_written_with_the_status(self, workspace):
        store, build, _, _ = workspace
        build([("a", "pending", [])])

        store.update_task_status("PLAN", "a", "completed", verified=True)
        assert store.verified_state("PLAN", "a") is True

        store.update_task_status("PLAN", "a", "failed", verified=False)
        assert store.verified_state("PLAN", "a") is False

    def test_a_status_write_without_a_receipt_leaves_the_column_alone(self, workspace):
        store, build, _, _ = workspace
        build([("a", "pending", [])])
        store.update_task_status("PLAN", "a", "completed", verified=True)
        store.update_task_status("PLAN", "a", "completed", detail_note="again")
        assert store.verified_state("PLAN", "a") is True

    # -- through the engine harness ------------------------------------------------
    @staticmethod
    def _stored_artifact(store, task_id="a"):
        store.save_artifact("PLAN", task_id, {
            "plan_id": "PLAN", "task_id": task_id, "summary": "s",
            "estimated_impact": "i", "complexity_score": 1,
            "required_capabilities": ["exec"],
            "ast_targets": [{
                "file_path": "main.py", "symbol_name": "", "byte_range": [0, 0],
                "operation": "insert", "content": "x = 1\n",
            }],
        })

    def _run_execution_pass(self, store, verdict):
        """The real completion path, with only the I/O it does not own stubbed out."""
        from types import SimpleNamespace

        from orchestration.workflow import execution

        ctx = SimpleNamespace(
            emit_fn=lambda _event: None,
            stream_text=lambda *args, **kwargs: None,
            should_stop=lambda: False,
        )
        applied = SimpleNamespace(success=True, error="", applied=[1])
        with mock.patch.object(execution.executor, "execute_approved", return_value=applied), \
                mock.patch.object(execution, "run_task_tests", return_value=verdict), \
                mock.patch.object(execution, "load_plan_state", return_value={}), \
                mock.patch.object(execution, "compile_plan_json_to_markdown", return_value=""), \
                mock.patch.object(execution, "_compile_check", return_value=""), \
                mock.patch.object(execution, "_ingest_into_knowledge_graph", return_value=[]), \
                mock.patch.object(execution.time, "sleep", lambda *a, **k: None):
            execution.run_approved_artifact(ctx, {}, {}, {"planId": "PLAN", "taskId": "a"})
        return store.get_dag("PLAN").nodes["a"]

    def test_the_pass_refuses_an_unverified_completion(self, workspace):
        """The acceptance criterion: no receipt means no ``completed`` -- the node is re-queued."""
        store, build, _, _ = workspace
        build([("a", "in_progress", [])])
        store.record_assessment("PLAN", "a", 1, ["exec"])
        self._stored_artifact(store)

        node = self._run_execution_pass(store, {"tests": [], "verdict": "none", "summary": ""})

        # The engine intercepts the transition rather than completing (or failing) the work.
        assert node.status == "pending"
        assert store.verified_state("PLAN", "a") is False

        state = store.rejection_state("PLAN", "a")
        assert state["attempts"] == 1, "the refusal must consume one retry, like a human's"
        assert any("Task unverified" in note for note in state["feedback"])
        assert any("return exit_code 0" in note for note in state["feedback"])

    def test_a_refused_node_is_requeued_and_still_dispatchable(self, workspace):
        """Re-queued, not failed: the refusal consumes a retry and the node can run again."""
        store, build, _, _ = workspace
        build([("a", "in_progress", [])])
        store.record_assessment("PLAN", "a", 1, ["exec"])
        self._stored_artifact(store)

        self._run_execution_pass(store, {"tests": [], "verdict": "none", "summary": ""})

        connection = sqlite3.connect(store.path)
        connection.row_factory = sqlite3.Row
        try:
            executable = get_executable_node_ids(connection, "PLAN", max_retries=2)
        finally:
            connection.close()
        assert "a" in executable, "a refused node must go back to the queue"
        assert store.rejection_state("PLAN", "a")["attempts"] == 1

    def test_repeated_refusals_exhaust_the_retry_budget(self, workspace):
        """The retry is bounded: a node that never produces a receipt cannot spin forever."""
        store, build, _, _ = workspace
        build([("a", "in_progress", [])])
        store.record_assessment("PLAN", "a", 1, ["exec"])
        self._stored_artifact(store)

        self._run_execution_pass(store, {"tests": [], "verdict": "none", "summary": ""})
        self._run_execution_pass(store, {"tests": [], "verdict": "none", "summary": ""})

        assert store.rejection_state("PLAN", "a")["attempts"] == 2
        connection = sqlite3.connect(store.path)
        connection.row_factory = sqlite3.Row
        try:
            executable = get_executable_node_ids(connection, "PLAN", max_retries=1)
        finally:
            connection.close()
        assert "a" not in executable, "the retry budget must bound an unverifiable node"

    def test_the_pass_completes_a_verified_task_and_records_the_receipt(self, workspace):
        store, build, _, _ = workspace
        build([("a", "in_progress", [])])
        store.record_assessment("PLAN", "a", 1, ["exec"])
        self._stored_artifact(store)

        verdict = {
            "tests": [{"filename": "test_main.py", "returncode": 0, "verdict": "passed"}],
            "verdict": "passed", "summary": "1 passed",
        }
        node = self._run_execution_pass(store, verdict)

        assert node.status == "completed"
        assert store.verified_state("PLAN", "a") is True

    def test_a_task_that_ran_nothing_completes_without_a_receipt(self, workspace):
        """The gate is scoped to ``exec``: a planning-only node is not held to a test."""
        store, build, _, _ = workspace
        build([("a", "in_progress", [])])
        store.record_assessment("PLAN", "a", 1, ["fs"])
        self._stored_artifact(store)

        node = self._run_execution_pass(store, {"tests": [], "verdict": "none", "summary": ""})

        assert node.status == "completed"


def _approve_and_apply(node_id, workspace_dir):
    """The UI's half, end to end: approve, then run the Phase 3.5 execution pass.

    ``executor.execute_approved`` alone applies the artifact but records nothing -- the *pass* in
    ``orchestration.workflow.execution`` is what completes the task, which is what releases its
    children.
    """
    from types import SimpleNamespace

    from orchestration.mcp_session import MCPSessionContext
    from orchestration.workflow import execution
    from tools import execution_gate
    from tools.file_tools import load_plan_state

    execution_gate.approve_artifact(node_id, "PLAN")
    with MCPSessionContext(workspace_dir) as session:
        ctx = SimpleNamespace(
            emit_fn=lambda _event: None,
            stream_text=lambda *args, **kwargs: None,
            should_stop=lambda: False,
            plan_file="PLAN.md",
            mcp_session=session,
        )
        execution.run_approved_artifact(
            ctx, load_plan_state(), {}, {"taskId": node_id, "planId": "PLAN"}
        )
