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
        self._orig_plan_dir = workspace.get_plan_dir()
        self._tmp = tempfile.mkdtemp(prefix="normalize_gate_")
        # Point the *plan* directory at the temp dir first: the setters load the plan, and a
        # load while the pointer still named the developer's directory would create a state
        # database beside their roadmap.
        workspace.set_plan_dir(self._tmp)
        workspace.set_project_dir(self._tmp)
        self.addCleanup(workspace.set_project_dir, self._orig_project)
        self.addCleanup(workspace.set_plan_dir, self._orig_plan_dir)

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
            self._tmp, ".aleth_backups", "plan-normalize", "PLAN.md"
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


class _FakeConsoleEvents:
    """The one window event the service still subscribes to: ``closed``."""

    def __iadd__(self, handler):
        self.handler = handler
        return self


class _FakeConsoleWindow:
    """A window that is created with a URL, not fed markup."""

    def __init__(self):
        self.events = mock.Mock()
        self.events.closed = _FakeConsoleEvents()


class DetachedConsoleBridgeTests(unittest.TestCase):
    """The second window is a browser on the gateway, not an inline document.

    It used to be handed an ``html=`` string, which gave it an opaque origin: it could neither
    fetch the backlog nor open an ``EventSource``, so its lines had to be injected with
    ``evaluate_js``. It is now pointed at a page the gateway serves, and it reads the stream.
    """

    @classmethod
    def setUpClass(cls):
        try:
            import app
        except Exception as exc:  # pragma: no cover - only when credentials are absent
            raise unittest.SkipTest(f"app needs credentials: {exc}")
        cls.app = app

    def _api(self):
        api = self.app.EngineService()
        api._events = []
        api.emit_event = api._events.append
        api._api = mock.Mock(base_url="http://127.0.0.1:8765")
        return api

    def _open(self, api, backlog):
        window = _FakeConsoleWindow()
        created = threading.Event()
        seen = {}

        def fake_create(**kwargs):
            seen.update(kwargs)
            created.set()
            return window

        with mock.patch.object(self.app.webview, "create_window", side_effect=fake_create):
            result = api.open_console_window(backlog)
            self.assertTrue(result["success"])
            self.assertTrue(created.wait(2), "the window was never created")
        return seen

    def test_a_push_with_no_window_is_refused_not_raised(self):
        result = self._api().push_console_line("log", "anything")
        self.assertFalse(result["success"])

    def test_opening_points_the_window_at_the_console_page(self):
        api = self._api()
        seen = self._open(api, [{"kind": "cmd", "text": "hello world"}])

        # A URL on the gateway -- so the page has an origin it can stream from -- and not `html=`.
        self.assertEqual(seen.get("url"), "http://127.0.0.1:8765/console.html")
        self.assertNotIn("html", seen)
        self.assertEqual(api._events[-1]["type"], "console_detached")

        # The history travels through the backend, because the second window cannot read the
        # first window's memory.
        self.assertEqual(
            api.get_console_backlog()["lines"], [{"kind": "cmd", "text": "hello world"}]
        )

    def test_a_pushed_line_is_announced_on_the_bus(self):
        """The mirror is the stream now: one event, and the console page renders it."""
        api = self._api()
        self._open(api, [])

        self.assertEqual(api.push_console_line("tool", "later line")["success"], True)
        self.assertEqual(
            api._events[-1], {"type": "console_line", "kind": "tool", "text": "later line"}
        )

    def test_the_backlog_is_dropped_when_the_window_closes(self):
        api = self._api()
        self._open(api, [{"kind": "log", "text": "gone soon"}])
        api._forget_console_window()

        self.assertEqual(api.get_console_backlog()["lines"], [])
        self.assertFalse(api.push_console_line("log", "after close")["success"])

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
