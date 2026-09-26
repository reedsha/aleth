"""Offline System 1 plan tagging, pinned.

Two contracts matter here and both are about not damaging a plan a human wrote:

* a ``dry_run`` must not write, whatever it finds;
* an explicit ``[UI]`` the author typed must survive an engine that disagrees, because a
  re-derivation pass is not entitled to delete a fact a person asserted.
"""

import unittest
from unittest import mock

from orchestration import plan_tagging
from tools.plan_parser import explicit_ui_titles


class _Verdict:
    def __init__(self, domain: str):
        self.domain = domain


def _classifier(mapping):
    """A stand-in engine: exact titles in ``mapping`` win, everything else is general."""

    def classify(title: str) -> _Verdict:
        return _Verdict(mapping.get(title, "GENERAL"))

    return classify


def _plan(tasks):
    return {"sections": [{"title": "Section", "tasks": tasks}]}


def _task(task_id, title, is_ui=False, sub_steps=None, tag=None):
    task = {
        "id": task_id,
        "title": title,
        "status": "pending",
        "is_ui": is_ui,
        "details": [],
        "files": [],
    }
    # Only when a tag is claimed: a plan written before the vocabulary existed carries no
    # ``tag`` key at all, which is a state worth testing as much as the tagged one.
    if tag is not None:
        task["tag"] = tag
    if sub_steps is not None:
        task["sub_steps"] = sub_steps
    return task


class RetagPlanTests(unittest.TestCase):
    def _retag(self, plan, classify, dry_run=False, explicit=None):
        """Runs the pass against a fake plan, with the writer captured rather than used."""
        with mock.patch.object(plan_tagging, "load_plan_state", return_value=plan), \
                mock.patch.object(plan_tagging, "save_plan_state") as save:
            result = plan_tagging.retag_plan(
                dry_run=dry_run, classify=classify, explicit=explicit if explicit is not None else {}
            )
        return result, save

    def test_a_title_the_engine_calls_ui_is_tagged(self):
        plan = _plan([_task("task-1", "build the expander panel")])
        result, save = self._retag(plan, _classifier({"build the expander panel": "UI"}))
        self.assertTrue(plan["sections"][0]["tasks"][0]["is_ui"])
        self.assertEqual(result["ui_after"], 1)
        self.assertEqual(len(result["changed"]), 1)
        save.assert_called_once()

    def test_a_dry_run_reports_without_writing(self):
        plan = _plan([_task("task-1", "build the expander panel")])
        result, save = self._retag(
            plan, _classifier({"build the expander panel": "UI"}), dry_run=True
        )
        save.assert_not_called()
        self.assertFalse(result["written"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(len(result["changed"]), 1)

    def test_nothing_is_written_when_nothing_changed(self):
        plan = _plan([_task("task-1", "add a login endpoint", is_ui=False)])
        result, save = self._retag(plan, _classifier({}))
        save.assert_not_called()
        self.assertEqual(result["changed"], [])
        self.assertFalse(result["written"])

    def test_an_explicit_tag_survives_an_engine_that_disagrees(self):
        # The engine says general; the author wrote [UI]. The author wins.
        plan = _plan([_task("task-1", "retire the resize seam", is_ui=True, tag="FE")])
        result, save = self._retag(
            plan,
            _classifier({}),  # general for everything
            explicit={"retire the resize seam": "FE"},
        )
        self.assertTrue(plan["sections"][0]["tasks"][0]["is_ui"])
        self.assertEqual(result["kept_explicit"], 1)
        self.assertEqual(result["changed"], [])
        save.assert_not_called()

    def test_an_explicit_non_ui_tag_survives_too(self):
        # The gap that made this wider guard necessary: an author's [CI/CD] used to be
        # re-derived like an inference and deleted when the engine said something else.
        plan = _plan([_task("task-1", "wire the pipeline", tag="CI/CD")])
        result, save = self._retag(
            plan,
            _classifier({}),  # general for everything
            explicit={"wire the pipeline": "CI/CD"},
        )
        task = plan["sections"][0]["tasks"][0]
        self.assertEqual(task["tag"], "CI/CD")
        self.assertFalse(task["is_ui"])
        self.assertEqual(result["kept_explicit"], 1)
        save.assert_not_called()

    def test_an_inferred_false_positive_is_cleared(self):
        # Marked UI by the word list, but no author tag: re-derivation may drop it.
        plan = _plan([_task("task-1", "remove code-level diff inspection views", is_ui=True)])
        result, save = self._retag(plan, _classifier({}))
        self.assertFalse(plan["sections"][0]["tasks"][0]["is_ui"])
        self.assertEqual(result["ui_before"], 1)
        self.assertEqual(result["ui_after"], 0)
        save.assert_called_once()

    def test_a_plan_without_tag_keys_gains_them(self):
        # A plan written before the vocabulary existed has no ``tag`` on any task, so
        # persisting one is a real change even when is_ui does not move -- and comparing
        # only is_ui was exactly the bug that made every non-UI tag vanish unwritten.
        plan = _plan([_task("task-1", "add a login endpoint")])
        result, save = self._retag(plan, _classifier({"add a login endpoint": "API"}))
        self.assertEqual(plan["sections"][0]["tasks"][0]["tag"], "API")
        self.assertFalse(plan["sections"][0]["tasks"][0]["is_ui"])
        self.assertEqual(len(result["changed"]), 1)
        self.assertEqual(result["changed"][0]["tag"], "API")
        save.assert_called_once()

    def test_sub_steps_are_decided_separately_from_their_task(self):
        plan = _plan(
            [
                _task(
                    "task-1",
                    "dual-view workbench",
                    sub_steps=[
                        _task("task-1-sub-1", "build the plan tree view"),
                        _task("task-1-sub-2", "add a view toggle"),
                    ],
                )
            ]
        )
        result, _ = self._retag(
            plan, _classifier({"build the plan tree view": "UI", "add a view toggle": "CORE"})
        )
        subs = plan["sections"][0]["tasks"][0]["sub_steps"]
        self.assertFalse(plan["sections"][0]["tasks"][0]["is_ui"])
        self.assertTrue(subs[0]["is_ui"])
        self.assertFalse(subs[1]["is_ui"])
        self.assertEqual(result["total"], 3)

    def test_the_engine_that_answered_is_reported(self):
        plan = _plan([_task("task-1", "anything")])
        result, _ = self._retag(plan, _classifier({}))
        self.assertIn(result["engine"], {"model", "heuristic"})


class ExplicitUiTitlesTests(unittest.TestCase):
    """The parser helper the pass leans on to tell an author's tag from an inferred one."""

    def test_it_finds_only_literally_tagged_titles(self):
        content = "\n".join(
            [
                "# Plan",
                "- [ ] [UI] Build the expander panel",
                "- [ ] Add a login endpoint",
                "  - [ ] [UI] Nest a view toggle",
                "  - [x] Add a route",
                "- [x] [UI] Retire the resize seam",
            ]
        )
        self.assertEqual(
            explicit_ui_titles(content),
            {"Build the expander panel", "Nest a view toggle", "Retire the resize seam"},
        )

    def test_a_backticked_or_midsentence_mention_is_not_a_tag(self):
        # The bug class the parser already guards against (see HANDOFF #10): a task that
        # merely *mentions* [UI] in prose has not earned the tag.
        content = "- [ ] Wire `[UI]` task tagging into the parser\n- [ ] [UI] Real one\n"
        self.assertEqual(explicit_ui_titles(content), {"Real one"})

    def test_an_empty_plan_has_no_explicit_titles(self):
        self.assertEqual(explicit_ui_titles(""), set())
        self.assertEqual(explicit_ui_titles("# Plan\n\nNothing here.\n"), set())


if __name__ == "__main__":
    unittest.main()
