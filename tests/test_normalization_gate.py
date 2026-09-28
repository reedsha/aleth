"""The Normalization Gate: the AST shape check and the zero-token reformat.

An imported ``.md`` that carries neither ``##`` sections nor ``- [ ]`` milestones renders
as a blank plan tree with every action locked, which is the failure mode the gate exists
to catch. Two contracts are pinned here: the *check* is a pure shape test that never
writes, and the *reformat* is conservative -- it writes only when it can actually recover
a milestone, and it snapshots the original markdown first.

    .\\venv\\Scripts\\python.exe -m unittest tests.test_normalization_gate -v
"""

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from tools.plan_parser import check_plan_structure
from tools.plan_state import plan_structure_report
from tools import workspace

STRUCTURED = """# Project Plan: Demo

## 1. Setup
- [ ] Bootstrap the app
  - [ ] Add a health endpoint
- [x] Choose the framework

## 2. Core
- [ ] Build the API
"""

PROSE_WITH_LISTS = """# Weather service notes

This document describes the weather microservice we want to build.

- Bootstrap the FastAPI app
- Implement the forecast endpoint
- Write unit tests
"""

PROSE_ONLY = """# Just some notes

Here is a paragraph with no list at all.

And another paragraph.
"""


class CheckPlanStructureTests(unittest.TestCase):
    def test_a_structured_plan_passes(self):
        report = check_plan_structure(STRUCTURED)
        self.assertTrue(report["structured"])
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["counts"]["sections"], 2)
        # Both the milestone and its indented sub-step checkbox count.
        self.assertEqual(report["counts"]["milestones"], 4)
        self.assertTrue(report["counts"]["has_title"])

    def test_prose_with_lists_is_unstructured_but_reports_what_it_has(self):
        report = check_plan_structure(PROSE_WITH_LISTS)
        self.assertFalse(report["structured"])
        self.assertEqual(report["counts"]["milestones"], 0)
        self.assertEqual(report["counts"]["bullets"], 3)
        self.assertTrue(any("## Section" in issue for issue in report["issues"]))

    def test_prose_without_lists_is_unstructured(self):
        report = check_plan_structure(PROSE_ONLY)
        self.assertFalse(report["structured"])
        self.assertEqual(report["counts"]["bullets"], 0)

    def test_an_empty_document_is_unstructured(self):
        report = check_plan_structure("")
        self.assertFalse(report["structured"])
        self.assertIn("empty", report["summary"].lower())

    def test_sections_without_milestones_are_not_enough(self):
        report = check_plan_structure("# Title\n\n## Section\n\nJust prose under a heading.\n")
        self.assertFalse(report["structured"])
        self.assertTrue(any("checkbox" in issue.lower() for issue in report["issues"]))

    def test_milestones_without_a_title_are_not_enough(self):
        report = check_plan_structure("## Section\n- [ ] A milestone\n")
        self.assertFalse(report["structured"])
        self.assertTrue(any("# Title" in issue for issue in report["issues"]))

    def test_a_numbered_list_is_not_a_milestone(self):
        # Relaxed parsing can lift numbered items, but the strict shape needs checkboxes,
        # and the gate judges the strict shape only.
        report = check_plan_structure("# Title\n\n## Section\n\n1. First\n2. Second\n")
        self.assertFalse(report["structured"])
        self.assertEqual(report["counts"]["bullets"], 2)


class PlanStructureReportTests(unittest.TestCase):
    def test_it_checks_the_content_it_is_given(self):
        # Pure when a document is supplied: nothing on disk is read.
        report = plan_structure_report("## Section\n- [ ] Task\n")
        self.assertFalse(report["structured"])
        self.assertEqual(report["counts"]["milestones"], 1)

    def test_the_facade_re_exports_it(self):
        from tools.file_tools import check_plan_structure as facade_check
        from tools.file_tools import plan_structure_report as facade_report

        self.assertIs(facade_check, check_plan_structure)
        self.assertIs(facade_report, plan_structure_report)


def _ctx(events):
    from orchestration.workflow.context import WorkflowContext

    return WorkflowContext(
        emit_fn=events.append,
        stream_text=lambda *args, **kwargs: None,
        should_stop=lambda: False,
        plan_file="PLAN.md",
    )


def _status_of(events):
    summaries = [e for e in events if e.get("type") == "architect_summary"]
    return summaries[-1]["summary"]["status"] if summaries else None


class NormalizeActionTests(unittest.TestCase):
    """The administrative-bypass reformat. Imports agents, so it skips without a key."""

    @classmethod
    def setUpClass(cls):
        try:
            from orchestration.workflow import actions_admin
        except Exception as exc:  # pragma: no cover - only when credentials are absent
            raise unittest.SkipTest(f"actions_admin needs credentials: {exc}")
        cls.actions_admin = actions_admin

    def setUp(self):
        self._orig_project = workspace.get_project_dir()
        self._tmp = tempfile.mkdtemp(prefix="normalize_gate_")
        workspace.set_project_dir(self._tmp)
        self.addCleanup(workspace.set_project_dir, self._orig_project)

    def _run(self, markdown):
        events = []
        with mock.patch.object(self.actions_admin, "read_plan_markdown", return_value=markdown), \
                mock.patch.object(self.actions_admin, "save_plan_state", side_effect=lambda p: p) as save:
            self.actions_admin.normalize_plan_action(_ctx(events), {}, {})
        return events, save

    def test_an_already_structured_plan_is_left_untouched(self):
        events, save = self._run(STRUCTURED)
        save.assert_not_called()
        self.assertNotIn("plan_updated", [e.get("type") for e in events])
        self.assertEqual(_status_of(events), "Already Structured")
        self.assertEqual(events[-1]["type"], "workflow_complete")

    def test_an_unstructured_plan_is_reformatted_and_snapshotted(self):
        events, save = self._run(PROSE_WITH_LISTS)
        save.assert_called_once()
        self.assertIn("plan_updated", [e.get("type") for e in events])
        self.assertEqual(_status_of(events), "Normalized")

        backup = os.path.join(
            self._tmp, ".deepagents_backups", "plan-normalize", "PLAN.md"
        )
        self.assertTrue(os.path.isfile(backup))
        with open(backup, encoding="utf-8") as f:
            self.assertEqual(f.read(), PROSE_WITH_LISTS)

        plan_updated = [e for e in events if e.get("type") == "plan_updated"][-1]
        self.assertIn("- [ ]", plan_updated["content"])

    def test_a_document_with_nothing_to_lift_is_not_emptied(self):
        events, save = self._run(PROSE_ONLY)
        save.assert_not_called()
        self.assertNotIn("plan_updated", [e.get("type") for e in events])
        self.assertEqual(_status_of(events), "Needs Manual Structure")


class RunnerDispatchTests(unittest.TestCase):
    """`normalize` is a real intent the runner routes, not just a bridge call."""

    @classmethod
    def setUpClass(cls):
        try:
            from orchestration.workflow import runner
        except Exception as exc:  # pragma: no cover - only when credentials are absent
            raise unittest.SkipTest(f"runner needs credentials: {exc}")
        cls.runner = runner

    def test_the_intent_tag_resolves(self):
        # The tag is detected only for a generic ``custom`` action, from the message body.
        self.assertEqual(
            self.runner.resolve_intent("custom", "normalize it [ACTION: NORMALIZE_PLAN]"),
            "normalize",
        )
        self.assertEqual(self.runner.resolve_intent("normalize", "anything"), "normalize")

    def test_the_runner_dispatches_normalize_to_the_admin_action(self):
        from orchestration.workflow import actions_admin

        events = []
        with mock.patch.object(self.runner, "load_plan_state", return_value={}), \
                mock.patch.object(actions_admin, "normalize_plan_action") as normalize:
            self.runner.run_agent_workflow(
                coder_agents={},
                stop_event=threading.Event(),
                user_message="normalize the plan",
                emit_fn=events.append,
                action_type="normalize",
            )
        normalize.assert_called_once()
        self.assertIn("workflow_started", [e.get("type") for e in events])


class DetachedConsoleHtmlTests(unittest.TestCase):
    """The second window's document is built here, so its escaping is pinned."""

    @classmethod
    def setUpClass(cls):
        try:
            import app
        except Exception as exc:  # pragma: no cover - only when credentials are absent
            raise unittest.SkipTest(f"app needs credentials: {exc}")
        cls.app = app

    def test_a_line_is_escaped_into_the_backlog(self):
        html = self.app._console_window_html(
            [{"kind": "cmd", "text": "<script>alert(1)</script>"}]
        )
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_the_kind_is_restricted_to_a_class_name(self):
        self.assertEqual(self.app._console_kind("tool"), "tool")
        self.assertEqual(self.app._console_kind('x" onload="y'), "xonloady")
        self.assertEqual(self.app._console_kind(None), "log")

    def test_non_dict_backlog_entries_are_ignored(self):
        html = self.app._console_window_html(["nonsense", None, 7])
        self.assertIn('id="stream"', html)


class _FakeConsoleEvents:
    """The two window events the bridge uses: ``loaded`` and ``closed``."""

    def __init__(self):
        self._loaded = threading.Event()

    def is_set(self):
        return self._loaded.is_set()

    def __iadd__(self, handler):
        # Registering the loaded handler stands in for the document having loaded, which
        # is what the real event means; the bridge then flushes and pushes directly.
        self._loaded.set()
        self.handler = handler
        return self


class _FakeConsoleWindow:
    def __init__(self):
        self.events = mock.Mock()
        self.events.loaded = _FakeConsoleEvents()
        self.events.closed = _FakeConsoleEvents()
        self.calls = []

    def evaluate_js(self, code):
        self.calls.append(code)


class DetachedConsoleBridgeTests(unittest.TestCase):
    """The second-window plumbing, with pywebview's window creation stubbed out."""

    @classmethod
    def setUpClass(cls):
        try:
            import app
        except Exception as exc:  # pragma: no cover - only when credentials are absent
            raise unittest.SkipTest(f"app needs credentials: {exc}")
        cls.app = app

    def _api(self):
        api = self.app.BridgeAPI()
        api._events = []
        api.emit_event = api._events.append
        return api

    def test_a_push_with_no_window_is_refused_not_raised(self):
        result = self._api().push_console_line("log", "anything")
        self.assertFalse(result["success"])

    def test_opening_seeds_the_backlog_and_mirrors_later_lines(self):
        api = self._api()
        window = _FakeConsoleWindow()
        created = threading.Event()
        seen = {}

        def fake_create(**kwargs):
            seen["html"] = kwargs.get("html", "")
            created.set()
            return window

        with mock.patch.object(self.app.webview, "create_window", side_effect=fake_create):
            result = api.open_console_window([{"kind": "cmd", "text": "hello world"}])
            self.assertTrue(result["success"])
            self.assertTrue(created.wait(2))

        self.assertIn("hello world", seen["html"])
        self.assertEqual(api._events[-1]["type"], "console_detached")

        api.push_console_line("tool", "later line")
        self.assertTrue(any("later line" in call for call in window.calls))

    def test_a_failed_open_is_announced_rather_than_raised(self):
        api = self._api()
        with mock.patch.object(
            self.app.webview, "create_window", side_effect=RuntimeError("no gui")
        ):
            api.open_console_window([])
            for _ in range(100):
                if api._events:
                    break
                time.sleep(0.02)

        self.assertTrue(api._events)
        self.assertEqual(api._events[0]["type"], "console_detach_failed")


if __name__ == "__main__":
    unittest.main()
