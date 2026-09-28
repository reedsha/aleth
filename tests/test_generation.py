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


class TaskTargetPathTests(unittest.TestCase):
    def test_a_backticked_path_in_the_title_is_the_target(self):
        task = {"title": "Upgrade AST parser (`tools/plan_parser.py`) for schema extraction"}
        self.assertEqual(generation.task_target_path(task), "tools/plan_parser.py")

    def test_the_title_beats_a_recorded_deliverable(self):
        # `files` is also what the workflow records after a run, so a task executed by the
        # old template path carries main.py. The author's title is the intent.
        task = {
            "title": "Update Architect directives in `agents/architect.py`",
            "files": ["main.py", "test_main.py"],
        }
        self.assertEqual(generation.task_target_path(task), "agents/architect.py")

    def test_a_path_in_the_notes_is_used_when_the_title_has_none(self):
        task = {"title": "Harden the session handling", "details": ["Touch `api/session.py`"]}
        self.assertEqual(generation.task_target_path(task), "api/session.py")

    def test_prose_without_a_backticked_file_is_not_a_target(self):
        # `templates` and `runner.resolve_intent` are backticked identifiers, not paths.
        task = {"title": "Reuse the `templates` UI keyword vocabulary"}
        self.assertIsNone(generation.task_target_path(task))
        self.assertIsNone(generation.task_target_path({"title": "Route through `laya.classify`"}))

    def test_a_declared_deliverable_is_the_last_resort(self):
        self.assertEqual(
            generation.task_target_path({"title": "Unnamed work", "files": ["pkg/mod.py"]}),
            "pkg/mod.py",
        )

    def test_a_task_that_names_nothing_has_no_target(self):
        self.assertIsNone(generation.task_target_path({"title": "Project scaffolding"}))

    def test_a_windows_separator_is_normalised(self):
        task = {"title": "Fix `tools\\plan_parser.py`"}
        self.assertEqual(generation.task_target_path(task), "tools/plan_parser.py")

    def test_the_paired_test_sits_beside_the_deliverable(self):
        self.assertEqual(generation.paired_test_path("tools/plan_parser.py"), "tools/test_plan_parser.py")
        self.assertEqual(generation.paired_test_path("main.py"), "test_main.py")
        self.assertEqual(generation.paired_test_path("ui/view.html"), "ui/test_view.py")


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

    def test_image_data_reaches_the_completer_when_present(self):
        seen = {}

        def fake(**kwargs):
            seen.update(kwargs)
            return SimpleNamespace(text="print(1)", model="m")

        with mock.patch.object(system2, "is_enabled", return_value=True):
            generation.generate_deliverable(
                coder_id="coder-deep", task=PLAN["steps"][0], plan=PLAN,
                deliverable=self._deliverable(), plan_file="PLAN.md",
                completer=fake, image_data_urls=["data:image/png;base64,AAAA"],
            )
        self.assertEqual(seen["images"], ["data:image/png;base64,AAAA"])

    def test_no_images_kwarg_is_passed_when_there_are_none(self):
        # The text-only call must keep exactly the signature it had, so a completer written
        # before vision still works (the pinned offline event streams depend on it).
        seen = {}

        def fake(**kwargs):
            seen.update(kwargs)
            return SimpleNamespace(text="print(1)", model="m")

        with mock.patch.object(system2, "is_enabled", return_value=True):
            generation.generate_deliverable(
                coder_id="coder-deep", task=PLAN["steps"][0], plan=PLAN,
                deliverable=self._deliverable(), plan_file="PLAN.md", completer=fake,
            )
        self.assertNotIn("images", seen)

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


class VisionMessageTests(unittest.TestCase):
    """An attached image becomes a real vision part -- not only a named reference."""

    def _client(self, captured):
        class FakeCompletions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="file"))],
                    usage=None,
                )
        return SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))

    def test_an_image_is_attached_as_a_vision_part(self):
        captured = {}
        result = system2.complete(
            model="m", system="s", user="describe it", client=self._client(captured),
            images=["data:image/png;base64,AAAA"],
        )
        self.assertIsNotNone(result)
        content = captured["messages"][1]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "describe it"})
        self.assertEqual(content[1]["image_url"]["url"], "data:image/png;base64,AAAA")

    def test_no_images_keeps_the_plain_string_message(self):
        captured = {}
        system2.complete(model="m", system="s", user="plain", client=self._client(captured))
        self.assertEqual(captured["messages"][1]["content"], "plain")


if __name__ == "__main__":
    unittest.main()
