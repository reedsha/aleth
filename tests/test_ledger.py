"""The Living Behavioral Ledger: the inline log and the milestone wrap-up.

Two halves of one round-trip are pinned here, because a mismatch between them is the
failure this design is most exposed to: the *compiler* writes a ``🟢 Behavioral Log:`` line
beneath a finished checkbox and the *parser* reads it back. If either stopped recognising
the other's spelling, the log would silently re-accumulate as an ordinary detail bullet on
every save -- the same class of bug the ``Files:`` line once had.

The wrap-up half is deterministic plan bookkeeping (no model, no tokens): a finished
milestone becomes one core fact in the ``## 🌍 Global State Summary`` that every later
prompt is anchored on.

    .\\venv\\Scripts\\python.exe -m unittest tests.test_ledger -v
"""

import unittest

from tools.plan_parser import (
    BEHAVIORAL_LOG_PREFIX,
    compile_plan_json_to_markdown,
    parse_markdown_to_plan_dict,
)
from orchestration.workflow import ledger
from orchestration.workflow.context import task_slice


def _log(text):
    return f"{BEHAVIORAL_LOG_PREFIX} {text}"


class ParserExtractionTests(unittest.TestCase):
    def test_a_log_beneath_a_task_is_extracted_not_left_in_details(self):
        content = (
            "# Plan\n\n## 1. S\n"
            "- [x] Build the API\n"
            f"  - {_log('added app/api.py with the forecast route.')}\n"
            "- [ ] Later\n"
        )
        plan = parse_markdown_to_plan_dict(content, "PLAN.md")
        task = plan["steps"][0]
        self.assertEqual(task["behavioral_log"], ["added app/api.py with the forecast route."])
        self.assertEqual(task["details"], [])

    def test_a_log_beneath_a_sub_step_belongs_to_the_sub_step(self):
        content = (
            "# Plan\n\n## 1. S\n"
            "- [ ] Parent\n"
            "  - [x] Child step\n"
            f"    - {_log('did the child thing')}\n"
        )
        plan = parse_markdown_to_plan_dict(content, "PLAN.md")
        task = plan["steps"][0]
        self.assertEqual(task["behavioral_log"], [])
        self.assertEqual(task["sub_steps"][0]["behavioral_log"], ["did the child thing"])
        self.assertEqual(task["sub_steps"][0]["details"], [])

    def test_an_ordinary_detail_is_still_a_detail(self):
        content = "# Plan\n\n## 1. S\n- [ ] Task\n  - just a note\n"
        plan = parse_markdown_to_plan_dict(content, "PLAN.md")
        self.assertEqual(plan["steps"][0]["details"], ["just a note"])
        self.assertEqual(plan["steps"][0]["behavioral_log"], [])

    def test_an_empty_log_marker_is_ignored(self):
        content = f"# Plan\n\n## 1. S\n- [x] Task\n  - {BEHAVIORAL_LOG_PREFIX}\n"
        plan = parse_markdown_to_plan_dict(content, "PLAN.md")
        self.assertEqual(plan["steps"][0]["behavioral_log"], [])


class RoundTripTests(unittest.TestCase):
    def _plan_with_logs(self):
        content = (
            "# Demo\n\n## 1. Setup\n"
            "- [x] Build the API\n"
            f"  - {_log('added app/api.py')}\n"
            "  - [x] Sub one\n"
            f"    - {_log('sub work done')}\n"
            "  - a plain note\n"
            "- [ ] Later\n"
        )
        return parse_markdown_to_plan_dict(content, "PLAN.md")

    def test_the_log_survives_a_compile_and_reparse(self):
        plan = self._plan_with_logs()
        before = plan["steps"][0]["behavioral_log"]
        self.assertEqual(before, ["added app/api.py"])

        reparsed = parse_markdown_to_plan_dict(compile_plan_json_to_markdown(plan), "PLAN.md")
        task = reparsed["steps"][0]
        self.assertEqual(task["behavioral_log"], before)
        self.assertEqual(task["details"], ["a plain note"])
        self.assertEqual(task["sub_steps"][0]["behavioral_log"], ["sub work done"])

    def test_compiling_twice_is_stable(self):
        once = compile_plan_json_to_markdown(self._plan_with_logs())
        twice = compile_plan_json_to_markdown(
            parse_markdown_to_plan_dict(once, "PLAN.md")
        )
        self.assertEqual(once, twice)


class RecordBehavioralLogTests(unittest.TestCase):
    def test_it_appends_and_de_duplicates(self):
        entry = {}
        ledger.record_behavioral_log(entry, "did a thing")
        ledger.record_behavioral_log(entry, "did a thing")
        ledger.record_behavioral_log(entry, "did another")
        self.assertEqual(entry["behavioral_log"], ["did a thing", "did another"])

    def test_blank_messages_are_ignored(self):
        entry = {}
        ledger.record_behavioral_log(entry, "   ")
        self.assertEqual(entry.get("behavioral_log", []), [])


class WrapUpMilestoneTests(unittest.TestCase):
    def _task(self, **overrides):
        task = {
            "id": "task-1",
            "title": "Build the API",
            "status": "completed",
            "tag": "API",
            "files": ["app/api.py", "tests/test_api.py"],
            "details": [],
        }
        task.update(overrides)
        return task

    def test_it_creates_the_summary_and_adds_one_fact(self):
        plan = {}
        fact = ledger.wrap_up_milestone(plan, self._task())
        self.assertIn("Build the API", fact)
        self.assertIn("[API]", fact)
        self.assertIn("app/api.py", fact)
        self.assertEqual(plan["state_summary"]["bullets"], [fact])

    def test_it_is_idempotent(self):
        plan = {}
        fact = ledger.wrap_up_milestone(plan, self._task())
        ledger.wrap_up_milestone(plan, self._task())
        self.assertEqual(plan["state_summary"]["bullets"], [fact])

    def test_it_appends_to_an_existing_summary(self):
        plan = {"state_summary": {"title": "Global State Summary", "bullets": ["existing fact"]}}
        ledger.wrap_up_milestone(plan, self._task())
        self.assertEqual(plan["state_summary"]["bullets"], ["existing fact", ledger.milestone_fact(self._task())])

    def test_a_long_fact_is_truncated_to_one_line(self):
        fact = ledger.milestone_fact(self._task(title="x" * 500))
        self.assertLessEqual(len(fact), ledger.MAX_FACT_CHARS)
        self.assertTrue(fact.endswith("\u2026"))

    def test_a_task_without_a_file_still_yields_a_fact(self):
        fact = ledger.milestone_fact(self._task(files=[]))
        self.assertIn("Build the API", fact)
        self.assertNotIn("delivered", fact)


class TaskSliceLedgerTests(unittest.TestCase):
    def test_the_slice_carries_the_ledger(self):
        plan = {
            "title": "Demo",
            "state_summary": {"title": "S", "bullets": ["one fact"]},
            "steps": [
                {
                    "id": "task-1",
                    "section": "1. S",
                    "title": "Build the API",
                    "status": "completed",
                    "behavioral_log": ["added app/api.py"],
                    "details": [],
                    "files": [],
                    "sub_steps": [],
                }
            ],
        }
        text = task_slice(plan, "task-1")
        self.assertIn(f"- {BEHAVIORAL_LOG_PREFIX} added app/api.py", text)


if __name__ == "__main__":
    unittest.main()
