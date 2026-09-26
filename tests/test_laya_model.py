"""The model backend behind Laya's decision seam, pinned.

The checkpoint itself is ~0.8 GB of weights downloaded on first use, so nothing here
loads it. What is tested is everything around it: which engine the gate selects, that
the answer is asked in the shape the package documents, that the answer maps back onto
the engine's own vocabulary, and -- the property that matters most -- that every way
the model can disappoint ends in the word list rather than in a broken decision.

``FakeRouter`` stands in for ``laya.Router`` so the contract is asserted against a
recorded call rather than a live model.
"""

import ast
import contextlib
import io
import os
import pathlib
import unittest
from unittest import mock

from agents import laya
from agents import laya_model


def _boom(*args, **kwargs):
    raise AssertionError("this path must not touch the model runtime")


class FakeRouter:
    """Answers like ``laya.Router`` without a checkpoint."""

    def __init__(self, payload=None, error=None):
        self.payload = payload or {}
        self.error = error
        self.calls = []

    def predict(self, state, questions, model=None, **kwargs):
        self.calls.append({"state": state, "questions": questions, "model": model})
        if self.error is not None:
            raise self.error
        # The real payload: per-state answers keyed by question id, plus token usage and
        # the routing record. Only the answers are read, but the rest is here so the
        # adapter is tested against the real shape rather than a convenient subset.
        return {
            "model": "laya-rl-agent",
            "answers": self.payload,
            "usage": {"input_tokens": 64, "output_tokens": 0},
            "routing": {"model": "laya", "reason": "explicit model"},
        }


def _answers(intent="implementation", domain="api", confidence=0.91):
    """A payload shaped exactly like ``system_one``'s for two choice questions."""
    return {
        "intent": {
            "type": "choice",
            "choice": intent,
            "probabilities": {intent: confidence},
            "confidence": confidence,
            "answer_confidence": confidence,
            "action": {"act_probability": 0.5},
        },
        "domain": {
            "type": "choice",
            "choice": domain,
            "probabilities": {domain: confidence},
            "confidence": confidence,
            "answer_confidence": confidence,
            "action": {"act_probability": 0.5},
        },
    }


class GateTests(unittest.TestCase):
    """``LAYA_BACKEND`` selects the engine, and a typo must not select the wrong one."""

    def test_the_gate_is_off_when_the_variable_is_unset(self):
        self.assertFalse(laya_model.Resolver.from_env(environ={}, load=_boom).enabled)

    def test_the_gate_is_off_at_the_heuristic_value(self):
        resolver = laya_model.Resolver.from_env(
            environ={laya_model.ENV_BACKEND: "heuristic"}, load=_boom
        )
        self.assertFalse(resolver.enabled)

    def test_the_gate_reads_the_model_value_regardless_of_case_and_padding(self):
        resolver = laya_model.Resolver.from_env(
            environ={laya_model.ENV_BACKEND: "  Model  "}, load=_boom
        )
        self.assertTrue(resolver.enabled)

    def test_an_unknown_value_is_reported_and_read_as_the_heuristic(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            resolver = laya_model.Resolver.from_env(
                environ={laya_model.ENV_BACKEND: "modernbert"}, load=_boom
            )
        self.assertFalse(resolver.enabled)
        self.assertIn("[Laya]", stream.getvalue())
        self.assertIn("modernbert", stream.getvalue())

    def test_the_module_seam_defaults_to_the_word_list(self):
        router = FakeRouter(payload=_answers())
        with mock.patch.object(laya_model, "_load_router", lambda: router):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(laya_model.ENV_BACKEND, None)
                laya_model.reset()
                self.addCleanup(laya_model.reset)
                verdict = laya_model.classify("explain the architecture")

        self.assertEqual(verdict, laya.classify("explain the architecture"))
        self.assertEqual(router.calls, [], "the checkpoint must not be consulted")

    def test_the_module_seam_takes_the_checkpoint_when_the_gate_selects_it(self):
        router = FakeRouter(payload=_answers(intent="analysis", domain="docs"))
        with mock.patch.object(laya_model, "_load_router", lambda: router):
            with mock.patch.dict(os.environ, {laya_model.ENV_BACKEND: "model"}):
                laya_model.reset()
                self.addCleanup(laya_model.reset)
                verdict = laya_model.classify("what does main.py do?")

        self.assertEqual(verdict.intent, laya.INTENT_ADMIN)
        self.assertEqual(verdict.domain, laya.DOMAIN_DOCS)
        self.assertEqual(verdict.tag, "DOCS")
        self.assertEqual(len(router.calls), 1)


class QuestionContractTests(unittest.TestCase):
    """The questions are the wire format with the package; these pin the shape."""

    def setUp(self):
        self.router = FakeRouter(payload=_answers())
        self.resolver = laya_model.Resolver(True, lambda: self.router)
        self.resolver.classify("add a login endpoint")

    def _call(self):
        self.assertEqual(len(self.router.calls), 1, "one directive, one forward pass")
        return self.router.calls[0]

    def test_the_questions_are_choice_questions_under_the_documented_keys(self):
        questions = self._call()["questions"]
        self.assertEqual(set(questions), {"intent", "domain"})
        for spec in questions.values():
            self.assertEqual(spec["type"], "choice")
            self.assertTrue(spec["instructions"].strip())
            self.assertGreaterEqual(len(spec["criteria"]), 2)

    def test_the_criteria_key_the_domain_vocabulary(self):
        # The coarse domains, not the twenty-five tags: see questions() for the measured
        # reason the tag vocabulary makes a single-label choice ambiguous.
        criteria = self._call()["questions"]["domain"]["criteria"]
        self.assertIn("ui", criteria)
        self.assertIn("general", criteria)
        self.assertTrue(all(text.strip() for text in criteria.values()))

    def test_the_state_carries_the_key_the_instructions_address(self):
        self.assertEqual(self._call()["state"], {"request": "add a login endpoint"})

    def test_the_checkpoint_is_pinned_rather_than_left_to_the_router(self):
        self.assertEqual(self._call()["model"], laya_model.DEFAULT_MODEL)

    def test_the_pinned_checkpoint_is_a_name_the_router_accepts(self):
        # ``Router.predict`` rejects anything outside its own registry, and a rejected
        # name is swallowed by the word-list fallback -- so naming the Hugging Face
        # repository id here would leave the model backend silently never running. That
        # was the shipped state until it was measured against the checkpoint.
        self.assertIn(
            laya_model.DEFAULT_MODEL, {"english", "multilingual", "typed-decisions"}
        )

    def test_an_empty_directive_is_still_a_well_formed_request(self):
        laya_model.Resolver(True, lambda: self.router).classify("")
        self.assertEqual(self.router.calls[-1]["state"], {"request": ""})


class MappingTests(unittest.TestCase):
    """The checkpoint's labels are the engine's vocabulary, or the answer is refused."""

    def _verdict(self, payload, text="add a login endpoint"):
        router = FakeRouter(payload=payload)
        return laya_model.Resolver(True, lambda: router).classify(text)

    def test_an_answer_becomes_a_verdict_with_its_evidence(self):
        verdict = self._verdict(_answers(intent="implementation", domain="api", confidence=0.91))
        self.assertEqual(verdict.intent, laya.INTENT_CODE)
        self.assertEqual(verdict.domain, laya.DOMAIN_API)
        self.assertEqual(verdict.tag, "API")
        self.assertAlmostEqual(verdict.confidence, 0.91)
        self.assertIn("checkpoint", verdict.reasons[0])

    def test_the_coder_is_derived_from_the_domain_the_model_reported(self):
        self.assertEqual(
            self._verdict(_answers(domain="api")).coder, laya.CODER_DEEP
        )
        self.assertEqual(
            self._verdict(_answers(domain="tests")).coder, laya.CODER_STANDARD
        )

    def test_every_criteria_key_is_mapped_onto_a_distinct_domain_and_tag(self):
        criteria = laya_model.questions()["domain"]["criteria"]
        domains = {}
        for label in criteria:
            with self.subTest(label=label):
                verdict = self._verdict(_answers(domain=label))
                self.assertIn(f"domain={label!r}", verdict.reasons[0])
                domains[label] = (verdict.domain, verdict.tag)

        self.assertEqual(
            len(set(domains.values())),
            len(domains),
            f"two labels share a domain or tag: {domains}",
        )
        # The one domain with no tag of its own is the explicit "nothing fits" answer.
        self.assertIsNone(dict(domains.values())[laya.DOMAIN_GENERAL])

    def test_every_intent_label_is_mapped(self):
        intents = {}
        for label in laya_model.questions()["intent"]["criteria"]:
            with self.subTest(label=label):
                verdict = self._verdict(_answers(intent=label))
                self.assertIn(f"intent={label!r}", verdict.reasons[0])
                intents[label] = verdict.intent

        self.assertEqual(len(set(intents.values())), len(intents), intents)


class FallbackTests(unittest.TestCase):
    """Every way the model can disappoint must end in the word list.

    These are the tests that make enabling the checkpoint safe: the failure mode of an
    absent, broken or merely inaccurate model is the engine that shipped before it.
    """

    _TEXT = "add a login endpoint"

    def _heuristic(self):
        return laya.classify(self._TEXT)

    def _verdict(self, payload=None, error=None):
        router = FakeRouter(payload=payload, error=error)
        resolver = laya_model.Resolver(True, lambda: router)
        with contextlib.redirect_stdout(io.StringIO()):
            return resolver.classify(self._TEXT), resolver

    def test_a_disabled_resolver_never_loads_the_checkpoint(self):
        resolver = laya_model.Resolver(False, _boom)
        self.assertEqual(resolver.classify(self._TEXT), self._heuristic())

    def test_a_checkpoint_that_will_not_load_leaves_the_word_list_in_place(self):
        with contextlib.redirect_stdout(io.StringIO()) as stream:
            resolver = laya_model.Resolver(True, _boom)
            first = resolver.classify(self._TEXT)
            second = resolver.classify(self._TEXT)

        self.assertEqual(first, self._heuristic())
        self.assertEqual(second, self._heuristic())
        self.assertIsNone(resolver.backend())
        self.assertIsInstance(resolver.failure, AssertionError)
        self.assertEqual(
            stream.getvalue().count("[Laya]"),
            1,
            "a load failure is reported once, not once per directive",
        )

    def test_a_failing_inference_leaves_the_decision_standing(self):
        verdict, _ = self._verdict(error=RuntimeError("the checkpoint timed out"))
        self.assertEqual(verdict, self._heuristic())

    def test_an_off_contract_answer_falls_back_rather_than_guessing(self):
        payloads = (
            {},  # nothing answered at all
            {"intent": {"type": "choice", "choice": "refactor"},
             "domain": {"type": "choice", "choice": "api"}},  # label outside the criteria
            {"intent": {"type": "score", "score": 2.0},
             "domain": {"type": "choice", "choice": "api"}},  # the wrong question type
            {"intent": {"type": "choice"},
             "domain": {"type": "choice", "choice": "api"}},  # no choice recorded
            {"intent": {"type": "choice", "choice": "analysis"}},  # domain unanswered
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                verdict, _ = self._verdict(payload=payload)
                self.assertEqual(verdict, self._heuristic())

    def test_an_explicit_ui_tag_never_reaches_the_checkpoint(self):
        router = FakeRouter(payload=_answers(intent="analysis", domain="general"))
        resolver = laya_model.Resolver(True, lambda: router)
        verdict = resolver.classify("do the needful", {"tag": laya.UI_TAG})

        self.assertEqual(verdict.tag, laya.UI_TAG)
        self.assertEqual(verdict.domain, laya.DOMAIN_UI)
        self.assertEqual(router.calls, [])


class LoadingTests(unittest.TestCase):
    """The checkpoint is constructed at most once, and only when it is selected."""

    def test_the_checkpoint_is_constructed_once_however_many_decisions_arrive(self):
        loads = []

        def load():
            loads.append(1)
            return FakeRouter(payload=_answers())

        resolver = laya_model.Resolver(True, load)
        for _ in range(5):
            resolver.classify("add a login endpoint")

        self.assertEqual(len(loads), 1)

    def test_warm_up_does_not_touch_the_package_without_the_gate(self):
        with mock.patch.object(laya_model, "_load_router", _boom):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(laya_model.ENV_BACKEND, None)
                laya_model.reset()
                self.addCleanup(laya_model.reset)
                laya_model.warm_up()  # raises through _boom if it loads anything

    def test_the_engine_can_still_be_imported_without_the_package(self):
        # The dependency runs one way only, so a machine with no `laya` installed can
        # still import the engine, the workflow and the plan tools.
        source = pathlib.Path(laya_model.__file__).read_text(encoding="utf-8")
        roots = []
        for node in ast.parse(source).body:
            if isinstance(node, ast.Import):
                roots.extend(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                roots.append((node.module or "").split(".")[0])

        forbidden = {"torch", "transformers", "laya", "onnxruntime", "numpy"}
        self.assertEqual(
            forbidden & set(roots),
            set(),
            "a model runtime is imported at module scope, so importing this module "
            "costs it even when the gate is off",
        )


if __name__ == "__main__":
    unittest.main()
