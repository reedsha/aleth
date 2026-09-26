"""Laya's decisions, pinned.

Laya is a pure function of its input, so it needs no workspace, no fixtures and no
event capture -- which is the point: the decision that used to be tangled into
``custom_action`` is now testable on its own.

These assertions are the contract a model backend must also satisfy. Swap the engine
behind ``classify`` and this file should stay green.
"""

import pathlib
import subprocess
import sys
import unittest

from agents import laya
from orchestration.workflow import templates
from tools import plan_parser
from tools.task_tags import UI_TAG


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

    def test_the_context_tag_marks_the_domain(self):
        verdict = laya.classify("do the needful", {"tag": UI_TAG})
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


class PlanDriftTests(unittest.TestCase):
    """The pre-flight that decides whether a reconciliation is worth running."""

    @staticmethod
    def _plan(status, files):
        return {"steps": [{"id": "task-1", "title": "T", "status": status, "files": files}]}

    def test_a_plan_whose_files_all_exist_is_unlikely_to_have_drifted(self):
        report = laya.plan_drift(self._plan("completed", ["main.py"]), ["main.py"])
        self.assertLess(report.probability, laya.DRIFT_THRESHOLD)
        self.assertEqual(report.completed_missing, 0)
        self.assertIn("matches", report.reasons[0])

    def test_a_completed_task_missing_its_deliverable_is_strong_evidence(self):
        report = laya.plan_drift(self._plan("completed", ["gone.py"]), [])
        self.assertGreaterEqual(report.probability, laya.DRIFT_THRESHOLD)
        self.assertEqual(report.completed_missing, 1)
        self.assertIn("gone.py", report.reasons[0])

    def test_a_pending_task_whose_file_exists_is_weaker_evidence(self):
        report = laya.plan_drift(self._plan("pending", ["main.py"]), ["main.py"])
        self.assertEqual(report.pending_existing, 1)
        self.assertGreater(report.probability, 0.0)
        self.assertLess(report.probability, laya.DRIFT_THRESHOLD)

    def test_files_no_task_claims_are_counted_but_weighed_lightly(self):
        report = laya.plan_drift(self._plan("completed", ["main.py"]), ["main.py", "stray.py"])
        self.assertEqual(report.untracked, 1)
        self.assertLess(report.probability, laya.DRIFT_THRESHOLD)

    def test_the_probability_is_capped_at_one(self):
        plan = {"steps": [
            {"id": f"task-{i}", "title": "T", "status": "completed", "files": [f"gone{i}.py"]}
            for i in range(6)
        ]}
        report = laya.plan_drift(plan, [f"stray{i}.py" for i in range(10)])
        self.assertEqual(report.probability, 1.0)

    def test_it_reads_the_nested_shape_too(self):
        plan = {"sections": [{"title": "S", "tasks": [
            {"id": "task-1", "title": "T", "status": "completed", "files": ["gone.py"]}
        ]}]}
        self.assertEqual(laya.plan_drift(plan, []).completed_missing, 1)

    def test_a_plan_with_no_declared_files_is_not_treated_as_drifted(self):
        report = laya.plan_drift(self._plan("pending", []), [])
        self.assertEqual(report.probability, 0.0)

    def test_backslashes_are_normalised(self):
        report = laya.plan_drift(self._plan("completed", ["tools\\x.py"]), ["tools/x.py"])
        self.assertEqual(report.completed_missing, 0)


class UiInferenceTests(unittest.TestCase):
    """Laya's UI inference must agree with the selector it shares a vocabulary with."""

    def test_the_selector_follows_the_parser_stamp(self):
        # UI wording is decided once, by the parser, and recorded as the tag; the
        # selector then reads that tag. This pins both halves: what the wording rule
        # decides, and that the selector honours the stamp it produces.
        ui_titles = [
            "Dashboard view",
            "Design the interface",
            "Dashboard views",
            "Public interfaces",
        ]
        plain_titles = [
            "Build a weather API",
            "Code review",
            "Refactor the scoreboard",
            "Project scaffolding and runtime dependencies",
        ]
        for title in ui_titles:
            with self.subTest(title=title):
                self.assertTrue(laya.inferred_ui(title), title)
                self.assertEqual(templates.select(title, UI_TAG).filename, "ui_view.html", title)
        for title in plain_titles:
            with self.subTest(title=title):
                self.assertFalse(laya.inferred_ui(title), title)
                self.assertNotEqual(templates.select(title, None).filename, "ui_view.html", title)

    def test_the_ui_vocabulary_is_shared_not_duplicated(self):
        self.assertIs(laya.UI_KEYWORD_RE, templates.UI_KEYWORD_RE)

    def test_an_explicit_tag_beats_the_vocabulary(self):
        # The wording alone says no; the tag is what makes it UI, and the parser honours
        # the tag before the wording rule ever runs.
        self.assertFalse(laya.inferred_ui("refactor the parser"))
        task = self._one_task("## 1. Build\n- [ ] [FE] Refactor the parser\n")
        self.assertEqual(task["tag"], UI_TAG)

    def test_a_word_that_merely_contains_a_keyword_is_not_ui(self):
        self.assertFalse(laya.inferred_ui("Build a weather API"))
        self.assertFalse(laya.inferred_ui("Code review"))

    def test_a_title_that_only_mentions_the_tag_is_not_ui(self):
        # The parser's own roadmap task: the tag it talks about is metadata, not a UI
        # keyword, so mentioning it must not tag the task that describes it.
        self.assertFalse(
            laya.inferred_ui("Wire zero-token `[UI]` task tagging into the parser")
        )

    def test_mentioning_the_tag_does_not_mask_a_real_ui_keyword(self):
        self.assertTrue(laya.inferred_ui("Add the [UI] tag to the dashboard view"))

    def test_a_backticked_source_path_still_marks_a_ui_task(self):
        # Guards the fix against over-reach: treating every code span as inert would
        # untag the tasks whose only signal is a `ui/js/...` path, and so move their
        # delegations. Only the tag literal itself is metadata.
        self.assertTrue(laya.inferred_ui("Redesign the right panel (`ui/js/sidebar.js`)"))

    def _one_task(self, markdown):
        parsed = plan_parser.parse_markdown_to_plan_dict(markdown, "PLAN.md")
        return parsed["sections"][0]["tasks"][0]

    def test_the_parser_stamps_the_inferred_tag_during_hydration(self):
        """The tree and the delegation path must not disagree about a UI task."""
        task = self._one_task("## 1. Build\n- [ ] Build the dashboard view\n")
        self.assertEqual(task["tag"], UI_TAG)

    def test_the_parser_leaves_a_plain_task_alone(self):
        task = self._one_task("## 1. Build\n- [ ] Build the plan parser\n")
        self.assertIsNone(task["tag"])

    def test_the_parser_does_not_duplicate_an_explicit_tag(self):
        task = self._one_task("## 1. Build\n- [ ] [UI] Wire the backend\n")
        self.assertEqual(task["tag"], UI_TAG)
        self.assertEqual(task["title"], "Wire the backend")

    def test_the_parser_does_not_tag_a_task_that_describes_the_tag(self):
        task = self._one_task("## 1. Build\n- [ ] Wire zero-token `[UI]` tagging\n")
        self.assertIsNone(task["tag"])
        self.assertEqual(task["title"], "Wire zero-token `[UI]` tagging")

    def test_an_inferred_tag_is_written_out_and_survives_a_compile_cycle(self):
        """The first save promotes the guess to an explicit tag, and stays put after.

        The compiler emits ``[FE] `` for a UI task, so a second pass reads a tag where
        the first pass had only wording. The two passes must agree, or the plan file
        would rewrite itself on every save.
        """
        source = "## 1. Build\n- [ ] Build the dashboard view\n"
        once = plan_parser.compile_plan_json_to_markdown(
            plan_parser.parse_markdown_to_plan_dict(source, "PLAN.md")
        )
        self.assertIn("[FE] Build the dashboard view", once)
        twice = plan_parser.compile_plan_json_to_markdown(
            plan_parser.parse_markdown_to_plan_dict(once, "PLAN.md")
        )
        self.assertEqual(once, twice)


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
        self.assertEqual(laya.route_coder("Do the thing", tag=UI_TAG), laya.CODER_DEEP)

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


class ImportIsolationTests(unittest.TestCase):
    """The decision engine must import on its own, in any order.

    A full ``discover`` run starts with ``test_characterization``, which imports
    ``registry`` and so warms ``sys.modules`` before this module is touched. An import
    cycle here is therefore invisible to the suite while
    ``python -m unittest tests.test_laya`` fails outright. A subprocess is the only honest
    way to pin it: it reproduces the cold cache without mutating this process's
    ``sys.modules`` (which would hand the rest of the run duplicate module objects).
    """

    REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

    def _imports_cleanly(self, statement: str) -> None:
        proc = subprocess.run(
            [sys.executable, "-c", statement],
            cwd=self.REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ok", proc.stdout)

    def test_laya_imports_with_a_cold_module_cache(self):
        self._imports_cleanly("import agents.laya; print('ok')")

    def test_plan_parser_imports_with_a_cold_module_cache(self):
        # The same cycle entered from the parser's side rather than from Laya's, which is
        # how it bit: ``tools.plan_parser`` lazily imports ``agents.laya.inferred_ui``.
        self._imports_cleanly(
            "from tools.plan_parser import parse_markdown_to_plan_dict; print('ok')"
        )


if __name__ == "__main__":
    unittest.main()
