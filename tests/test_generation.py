"""Turning a plan slice into a deliverable: the prompt, and the fallback rules.

The workflow's structural contract is pinned elsewhere; these tests cover the part that
changed -- that the Coder is asked with a *slice* rather than the whole plan, that a real
call's answer is used and its cost is reported, and that every failure path falls back to
the template byte-for-byte so the offline behaviour is unchanged.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from orchestration.workflow import generation, templates
from orchestration import system2

PLAN = {
    "title": "Demo",
    "state_summary": {"bullets": ["Architecture: dual-brain orchestration."]},
    "steps": [
        {
            "id": "task-1",
            "section": "1. Build",
            "title": "Build the widget",
            "status": "pending",
            "tag": "BE",
            "details": ["Note that applies to the task"],
            "files": ["widget.py"],
            "sub_steps": [],
        },
        {
            "id": "task-2",
            "section": "2. Other",
            "title": "UNRELATED TASK THAT MUST NOT APPEAR",
            "status": "pending",
            "tag": "FE",
            "details": [],
            "files": [],
            "sub_steps": [],
        },
    ],
    "sections": [],
}


class BuildInstructionTests(unittest.TestCase):
    def test_it_names_the_target_file_and_language(self):
        text = generation.build_instruction("ctx", "widget.py", "Build the widget")
        self.assertIn("Target file: `widget.py` (Python)", text)
        self.assertIn("Return ONLY the file's contents", text)

    def test_html_tasks_are_labelled_html(self):
        self.assertIn("(HTML)", generation.build_instruction("", "ui_view.html", "t"))

    def test_an_empty_slice_still_states_the_task(self):
        text = generation.build_instruction("", "widget.py", "Build the widget")
        self.assertIn("Task: Build the widget", text)


class BuildSystemPromptTests(unittest.TestCase):
    def test_it_reuses_the_role_prompt_in_direct_mode(self):
        for coder_id in ("coder-deep", "coder-standard"):
            prompt = generation.build_system_prompt(coder_id, "PLAN.md")
            self.assertIn("Direct File Generation Mode", prompt)
            self.assertIn("Return exactly the complete contents", prompt)
            # The plan variable is resolved and the tool workflow is still present.
            self.assertNotIn("{{ACTIVE_PLAN_FILE}}", prompt)
            self.assertIn("PLAN.md", prompt)

    def test_coder_deep_and_standard_differ(self):
        self.assertNotEqual(
            generation.build_system_prompt("coder-deep", "PLAN.md"),
            generation.build_system_prompt("coder-standard", "PLAN.md"),
        )


class StripCodeFenceTests(unittest.TestCase):
    def test_a_fenced_answer_is_unwrapped(self):
        self.assertEqual(generation.strip_code_fence("```python\nprint(1)\n```"), "print(1)")

    def test_an_unfenced_answer_is_trimmed_only(self):
        self.assertEqual(generation.strip_code_fence("  print(1)\n"), "print(1)")


class GenerateDeliverableTests(unittest.TestCase):
    def _deliverable(self):
        return templates.default_engine("Build the widget")

    def test_disabled_falls_back_to_the_template(self):
        with mock.patch.object(system2, "is_enabled", return_value=False):
            result = generation.generate_deliverable(
                coder_id="coder-standard", task=PLAN["steps"][0], plan=PLAN,
                deliverable=self._deliverable(), plan_file="PLAN.md",
                completer=lambda **kw: (_ for _ in ()).throw(AssertionError("must not call")),
            )
        self.assertFalse(result.used_llm)
        self.assertEqual(result.code, self._deliverable().code)

    def test_a_real_answer_is_used_and_its_cost_reported(self):
        captured = {}

        def fake_completer(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                text="```python\nprint('generated')\n```",
                model="openai:policy/coder-deep-test",
                prompt_tokens=409,
                completion_tokens=20,
            )

        with mock.patch.object(system2, "is_enabled", return_value=True):
            result = generation.generate_deliverable(
                coder_id="coder-deep", task=PLAN["steps"][0], plan=PLAN,
                deliverable=self._deliverable(), plan_file="PLAN.md",
                completer=fake_completer,
            )

        self.assertTrue(result.used_llm)
        self.assertEqual(result.code, "print('generated')")
        self.assertEqual(result.prompt_tokens, 409)
        self.assertIn("System 2 call:", result.usage_line())

        # The prompt carries the target task's slice and not the rest of the plan.
        self.assertIn("Build the widget", captured["user"])
        self.assertIn("Architecture: dual-brain orchestration.", captured["user"])
        self.assertNotIn("UNRELATED TASK THAT MUST NOT APPEAR", captured["user"])
        # The route comes from model_routing and is passed through unresolved.
        self.assertEqual(captured["model"], "openai:policy/coder-deep-test")

    def test_a_missing_completion_falls_back(self):
        with mock.patch.object(system2, "is_enabled", return_value=True):
            result = generation.generate_deliverable(
                coder_id="coder-deep", task=PLAN["steps"][0], plan=PLAN,
                deliverable=self._deliverable(), plan_file="PLAN.md",
                completer=lambda **kw: None,
            )
        self.assertFalse(result.used_llm)
        self.assertEqual(result.code, self._deliverable().code)

    def test_an_empty_completion_falls_back(self):
        with mock.patch.object(system2, "is_enabled", return_value=True):
            result = generation.generate_deliverable(
                coder_id="coder-deep", task=PLAN["steps"][0], plan=PLAN,
                deliverable=self._deliverable(), plan_file="PLAN.md",
                completer=lambda **kw: SimpleNamespace(text="   ", model="m"),
            )
        self.assertFalse(result.used_llm)
        self.assertEqual(result.code, self._deliverable().code)

    def test_on_request_fires_only_when_a_call_is_made(self):
        seen = []

        def fake(**kwargs):
            return SimpleNamespace(text="x\n", model="m", prompt_tokens=1, completion_tokens=1)

        common = dict(
            coder_id="coder-standard", task=PLAN["steps"][0], plan=PLAN,
            deliverable=self._deliverable(), plan_file="PLAN.md", completer=fake,
        )

        with mock.patch.object(system2, "is_enabled", return_value=False):
            generation.generate_deliverable(on_request=lambda m, c: seen.append((m, c)), **common)
        self.assertEqual(seen, [])

        with mock.patch.object(system2, "is_enabled", return_value=True):
            generation.generate_deliverable(on_request=lambda m, c: seen.append((m, c)), **common)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "openai:policy/coder-standard-test")
        self.assertGreater(seen[0][1], 0)

    def test_a_truncated_answer_falls_back_instead_of_writing_a_partial_file(self):
        with mock.patch.object(system2, "is_enabled", return_value=True):
            result = generation.generate_deliverable(
                coder_id="coder-deep", task=PLAN["steps"][0], plan=PLAN,
                deliverable=self._deliverable(), plan_file="PLAN.md",
                completer=lambda **kw: SimpleNamespace(
                    text="<html><body>cut off here", model="m",
                    prompt_tokens=1, completion_tokens=8192, finish_reason="length",
                ),
            )
        self.assertFalse(result.used_llm)
        self.assertEqual(result.code, self._deliverable().code)

    def test_a_completer_that_raises_still_falls_back(self):
        def boom(**kwargs):
            raise RuntimeError("unexpected")

        with mock.patch.object(system2, "is_enabled", return_value=True):
            result = generation.generate_deliverable(
                coder_id="coder-deep", task=PLAN["steps"][0], plan=PLAN,
                deliverable=self._deliverable(), plan_file="PLAN.md",
                completer=boom,
            )
        self.assertFalse(result.used_llm)
        self.assertEqual(result.code, self._deliverable().code)


if __name__ == "__main__":
    unittest.main()
