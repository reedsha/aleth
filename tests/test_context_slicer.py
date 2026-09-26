"""Line-Anchored Context Slicer.

The slicer exists so the Coder loop carries a prompt built from the plan's standing
context plus only the lines of the plan that concern the task being worked on, instead
of re-sending PLAN.md and its derived plan.json every turn. These tests pin the two
properties that make that safe: the slice holds everything about *this* task (nothing
truncated), and nothing about any other task or the machine state.

    .\\venv\\Scripts\\python.exe -m unittest tests.test_context_slicer -v
"""

import os
import unittest

from orchestration.workflow.context import (
    slice_task_context,
    state_summary_block,
    task_slice,
)


def _task(
    task_id,
    title,
    status="pending",
    section="1. General",
    is_ui=False,
    sub_steps=None,
    details=None,
    files=None,
    tag="__absent__",
):
    task = {
        "id": task_id,
        "section": section,
        "title": title,
        "status": status,
        "is_ui": is_ui,
        "details": list(details or []),
        "files": list(files or []),
        "sub_steps": list(sub_steps or []),
    }
    # ``tag`` is absent on a plan written before the vocabulary existed, which is a state
    # worth testing as much as the tagged one -- hence a sentinel rather than None.
    if tag != "__absent__":
        task["tag"] = tag
    return task


def _sub_step(sub_id, title, status="pending", is_ui=False, details=None, files=None, tag="__absent__"):
    sub = {
        "id": sub_id,
        "title": title,
        "status": status,
        "is_ui": is_ui,
        "details": list(details or []),
        "files": list(files or []),
    }
    if tag != "__absent__":
        sub["tag"] = tag
    return sub


def _plan(tasks, summary=None, title="Demo Roadmap"):
    plan = {
        "version": "1.0",
        "plan_file": "PLAN.md",
        "title": title,
        "updated_at": 1234567890.0,
        "state_summary": summary,
        "sections": [{"id": "sec-1", "title": "1. General", "tasks": tasks}],
        "steps": tasks,
        "metrics": {"total_tasks": len(tasks), "progress_percent": 0},
    }
    return plan


class StateSummaryBlockTests(unittest.TestCase):
    def test_it_includes_every_bullet(self):
        summary = {"title": "Global State Summary", "bullets": ["First fact", "Second fact", "Third fact"]}
        block = state_summary_block(_plan([], summary=summary))
        for bullet in summary["bullets"]:
            self.assertIn(bullet, block)
        self.assertIn("Demo Roadmap", block)

    def test_no_summary_is_an_empty_block(self):
        self.assertEqual(state_summary_block(_plan([], summary=None)), "")
        self.assertEqual(state_summary_block(_plan([], summary={"title": "x", "bullets": []})), "")


class TaskSliceTests(unittest.TestCase):
    def _plan_with_rich_task(self):
        rich = _task(
            "task-1",
            "Wire the context slicer",
            status="in_progress",
            section="2. Context Engine",
            details=["Keep the functions pure", "Never truncate a task"],
            files=["orchestration/workflow/context.py", "tests/test_context_slicer.py"],
            sub_steps=[
                _sub_step("task-1-sub-1", "Render the standing summary", status="completed"),
                _sub_step("task-1-sub-2", "Render the target line slice", status="pending", is_ui=True),
            ],
        )
        other = _task("task-2", "An unrelated task title", section="3. Other")
        return _plan([rich, other], summary={"title": "Global State Summary", "bullets": ["One fact"]})

    def test_a_slice_carries_the_task_and_nothing_else(self):
        plan = self._plan_with_rich_task()
        slice_text = task_slice(plan, "task-1")

        # The task's own line: section, title, status and tag carrier.
        self.assertIn("2. Context Engine", slice_text)
        self.assertIn("Wire the context slicer", slice_text)
        self.assertIn("[-]", slice_text)  # in_progress
        # Its sub-steps with their statuses.
        self.assertIn("Render the standing summary", slice_text)
        self.assertIn("[x]", slice_text)  # completed sub-step
        self.assertIn("Render the target line slice", slice_text)
        self.assertIn("[ ]", slice_text)  # pending sub-step
        self.assertIn("[UI]", slice_text)  # the inferred UI flag survives
        # Its detail notes and declared deliverables.
        self.assertIn("Keep the functions pure", slice_text)
        self.assertIn("Never truncate a task", slice_text)
        self.assertIn("orchestration/workflow/context.py", slice_text)
        self.assertIn("tests/test_context_slicer.py", slice_text)

    def test_a_slice_excludes_other_tasks_and_machine_state(self):
        plan = self._plan_with_rich_task()
        slice_text = task_slice(plan, "task-1")
        self.assertNotIn("An unrelated task title", slice_text)
        self.assertNotIn("3. Other", slice_text)
        self.assertNotIn("metrics", slice_text)
        self.assertNotIn("updated_at", slice_text)
        self.assertNotIn("version", slice_text)
        self.assertNotIn("1234567890", slice_text)

    def test_lookup_by_id_and_by_title_agree(self):
        plan = self._plan_with_rich_task()
        self.assertEqual(task_slice(plan, "task-1"), task_slice(plan, "Wire the context slicer"))

    def test_an_unknown_task_is_an_empty_slice_that_does_not_raise(self):
        plan = self._plan_with_rich_task()
        self.assertEqual(task_slice(plan, "task-999"), "")
        self.assertEqual(task_slice(plan, "no such title"), "")

    def test_a_bare_task_still_produces_a_usable_slice(self):
        plan = _plan([_task("task-1", "Sparse but real")])
        slice_text = task_slice(plan, "task-1")
        self.assertIn("Sparse but real", slice_text)
        self.assertIn("1. General", slice_text)

    def test_an_untagged_task_and_a_tagged_task_both_work(self):
        untagged = _task("task-1", "No tag here")
        tagged = _task("task-2", "Tagged here", tag="AUTH")
        plan = _plan([untagged, tagged])

        untagged_slice = task_slice(plan, "task-1")
        self.assertIn("No tag here", untagged_slice)
        self.assertNotIn("[AUTH]", untagged_slice)

        tagged_slice = task_slice(plan, "task-2")
        self.assertIn("[AUTH]", tagged_slice)

    def test_every_detail_note_survives_without_truncation(self):
        details = [f"note number {n}" for n in range(30)]
        plan = _plan([_task("task-1", "Long task", details=details)])
        slice_text = task_slice(plan, "task-1")
        for note in details:
            self.assertIn(note, slice_text)
        self.assertNotIn("...", slice_text)

    def test_slice_task_context_is_the_summary_then_the_slice(self):
        plan = self._plan_with_rich_task()
        summary = state_summary_block(plan)
        self.assertTrue(summary)
        self.assertEqual(
            slice_task_context(plan, "task-1"),
            summary + "\n\n" + task_slice(plan, "task-1"),
        )


class RealPlanSliceTests(unittest.TestCase):
    def test_the_slice_is_far_smaller_than_the_whole_plan(self):
        # Reads the real workspace plan through the normal loader. This is the contract the
        # slicer exists for: the full documents are ~11,755 tokens and the slice is ~150.
        from tools.plan_state import load_plan_state
        from tools.workspace import get_project_dir

        plan = load_plan_state()
        steps = plan.get("steps") or []
        if not steps:
            self.skipTest("the workspace plan carries no steps to slice")

        task_id = steps[0].get("id") or steps[0].get("title")
        slice_text = slice_task_context(plan, task_id)

        base_dir = get_project_dir()
        plan_md_name = plan.get("plan_file") or "PLAN.md"
        with open(os.path.join(base_dir, plan_md_name), encoding="utf-8") as f:
            plan_md = f.read()
        with open(os.path.join(base_dir, "plan.json"), encoding="utf-8") as f:
            plan_json = f.read()

        full = len(plan_md) + len(plan_json)
        self.assertLess(
            len(slice_text),
            full / 10,
            f"slice was {len(slice_text)} chars against a full dump of {full} chars",
        )


if __name__ == "__main__":
    unittest.main()
