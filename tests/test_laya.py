"""Laya's decisions, pinned.

Laya is a pure function of its input, so it needs no workspace, no fixtures and no
event capture -- which is the point: the decision that used to be tangled into
``custom_action`` is now testable on its own.

These assertions are the contract a model backend must also satisfy. Swap the engine
behind ``classify`` and this file should stay green.
"""

import pathlib
import unittest

from agents import laya
from orchestration.workflow import templates


class GatekeeperIntentTests(unittest.TestCase):
    """The two directives the characterization suite already pins must not move."""

    def test_the_pinned_analytical_prompt_stays_administrative(self):
        verdict = laya.classify("explain the architecture")
        self.assertEqual(verdict.intent, laya.INTENT_ADMIN)

    def test_the_pinned_coding_prompt_stays_a_delegation(self):
        verdict = laya.classify("add a login endpoint")
        self.assertEqual(verdict.intent, laya.INTENT_CODE)

    def test_every_legacy_admin_keyword_still_bypasses_the_coder(self):
        # The nine substrings the substring scan used, plus "analyse"/"summarise" style
        # spellings the vocabulary now also covers.
        legacy = [
            "analyze", "plan", "roadmap", "recommend", "how does",
            "what is", "status", "explain", "review", "audit",
        ]
        for keyword in legacy:
            with self.subTest(keyword=keyword):
                verdict = laya.classify(f"{keyword} the current situation")
                self.assertEqual(verdict.intent, laya.INTENT_ADMIN, keyword)


class GatekeeperEdgeTests(unittest.TestCase):
    """Where the heuristic is deliberately sharper than the substring scan."""

    def test_a_question_about_a_file_stays_administrative(self):
        self.assertEqual(laya.classify("what does main.py do?").intent, laya.INTENT_ADMIN)

    def test_a_polite_request_to_write_code_is_still_code(self):
        self.assertEqual(
            laya.classify("can you add a login endpoint?").intent, laya.INTENT_CODE
        )

    def test_naming_the_plan_file_is_always_administrative(self):
        for text in ("update plan.json", "add a task to PLAN.md", "update the roadmap"):
            with self.subTest(text=text):
                self.assertEqual(laya.classify(text).intent, laya.INTENT_ADMIN, text)

    def test_a_source_file_with_an_action_is_code(self):
        self.assertEqual(
            laya.classify("fix the login bug in main.py").intent, laya.INTENT_CODE
        )

    def test_the_leading_verb_decides_when_both_signals_appear(self):
        self.assertEqual(
            laya.classify("implement the parser and explain it").intent, laya.INTENT_CODE
        )
        self.assertEqual(
            laya.classify("review and fix the login bug").intent, laya.INTENT_ADMIN
        )

    def test_an_unrecognised_directive_falls_through_to_delegation(self):
        verdict = laya.classify("the button does not work")
        self.assertEqual(verdict.intent, laya.INTENT_CODE)
        self.assertLess(verdict.confidence, 0.5)

    def test_an_empty_directive_does_not_raise(self):
        verdict = laya.classify("")
        self.assertEqual(verdict.intent, laya.INTENT_CODE)
        self.assertEqual(verdict.domain, laya.DOMAIN_GENERAL)


class VerdictShapeTests(unittest.TestCase):
    def test_every_verdict_carries_its_evidence(self):
        verdict = laya.classify("add a login endpoint")
        self.assertTrue(verdict.reasons)
        self.assertGreater(verdict.confidence, 0.0)
        self.assertLessEqual(verdict.confidence, 1.0)

    def test_classify_is_pure_and_repeatable(self):
        self.assertEqual(
            laya.classify("explain the architecture"),
            laya.classify("explain the architecture"),
        )

    def test_the_context_flag_marks_the_domain(self):
        verdict = laya.classify("do the needful", {"is_ui": True})
        self.assertEqual(verdict.domain, laya.DOMAIN_UI)


class DomainTaggingTests(unittest.TestCase):
    def test_the_domain_vocabulary(self):
        cases = {
            "Dashboard view": laya.DOMAIN_UI,
            "add a weather API endpoint": laya.DOMAIN_API,
            "migrate the database schema": laya.DOMAIN_DB,
            "write unit tests with pytest": laya.DOMAIN_TESTS,
            "update the readme": laya.DOMAIN_DOCS,
            "refactor the core engine": laya.DOMAIN_CORE,
            "do the thing": laya.DOMAIN_GENERAL,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(laya.classify(text).domain, expected, text)


class UiInferenceTests(unittest.TestCase):
    """Laya's UI inference must agree with the selector it shares a vocabulary with."""

    def test_ui_inference_matches_the_template_selector(self):
        titles = [
            "Dashboard view",
            "Build a weather API",
            "Design the interface",
            "Code review",
            "Refactor the scoreboard",
            "Project scaffolding and runtime dependencies",
        ]
        for title in titles:
            with self.subTest(title=title):
                selects_ui = templates.select(title, False).filename == "ui_view.html"
                self.assertEqual(laya.inferred_ui(title), selects_ui, title)

    def test_the_ui_vocabulary_is_shared_not_duplicated(self):
        self.assertIs(laya.UI_KEYWORD_RE, templates.UI_KEYWORD_RE)

    def test_an_explicit_tag_beats_the_vocabulary(self):
        self.assertTrue(laya.inferred_ui("refactor the parser", is_ui=True))

    def test_a_word_that_merely_contains_a_keyword_is_not_ui(self):
        self.assertFalse(laya.inferred_ui("Build a weather API"))
        self.assertFalse(laya.inferred_ui("Code review"))


class CoderRoutingTests(unittest.TestCase):
    """The legacy routing rule is reproduced exactly, quirks included."""

    def test_the_scaffolds_first_pending_task_stays_on_the_standard_coder(self):
        self.assertEqual(
            laya.route_coder("Project scaffolding and runtime dependencies"),
            laya.CODER_STANDARD,
        )

    def test_the_scaffolds_core_task_stays_on_the_deep_coder(self):
        self.assertEqual(
            laya.route_coder("Build core domain models and application logic"),
            laya.CODER_DEEP,
        )

    def test_an_explicit_ui_tag_routes_deep(self):
        self.assertEqual(laya.route_coder("Do the thing", is_ui=True), laya.CODER_DEEP)

    def test_the_legacy_substring_quirk_is_documented_not_fixed_here(self):
        # "score" contains "core", so the legacy test routes this deep. Preserved on
        # purpose: changing it moves delegations, which is a separate decision.
        self.assertEqual(laya.route_coder("Refactor the scoreboard"), laya.CODER_DEEP)


class FreeFormRoutingTests(unittest.TestCase):
    """The Gatekeeper branches have no task title, so they route by domain."""

    def test_the_pinned_custom_prompt_still_reaches_the_deep_coder(self):
        verdict = laya.classify("add a login endpoint")
        self.assertEqual(laya.coder_for_domain(verdict.domain), laya.CODER_DEEP)

    def test_boilerplate_domains_reach_the_standard_coder(self):
        for text in ("write unit tests with pytest", "update the readme"):
            with self.subTest(text=text):
                verdict = laya.classify(text)
                self.assertEqual(
                    laya.coder_for_domain(verdict.domain), laya.CODER_STANDARD, text
                )

    def test_an_unclassified_directive_defaults_to_the_deep_coder(self):
        # Mirrors fix_bug's pinned description, which must stay on coder-deep.
        verdict = laya.classify("IndexError in run()")
        self.assertEqual(laya.coder_for_domain(verdict.domain), laya.CODER_DEEP)


class DependencyTests(unittest.TestCase):
    def test_laya_imports_no_model_runtime(self):
        source = pathlib.Path(laya.__file__).read_text(encoding="utf-8")
        for forbidden in ("import torch", "import transformers", "import onnxruntime"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
