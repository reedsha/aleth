"""System 2's Architect answers, and the model's thought process as UI text.

Two contracts are pinned, both about honesty:

* with no provider the answer seam yields ``used_llm=False``, so every administrative intent
  keeps the deterministic text it emitted before -- the offline event streams are a pinned
  contract and a keyless checkout must still run;
* with a provider, the administrative intents return the *model's* answer and its chain of
  thought, streamed into the Architect's card under its own log type. A button that used to
  narrate a script now reports what the model actually said.

A fake completer is injected, so this exercises the real call path without a network.

    .\\venv\\Scripts\\python.exe -m unittest tests.test_architect_reasoning -v
"""

import os
import unittest
from unittest import mock

from orchestration import system2
from orchestration.workflow import reasoning

FAKE_ENV = {
    "OPENAI_API_KEY": "test-key",
    "OPENAI_BASE_URL": "https://example.invalid/v1",
    "DEEPAGENTS_SYSTEM2": "1",
}


def _completion(text="A real answer.", reasoning_text="Step one.\nStep two."):
    return system2.Completion(
        text=text,
        model="openai:policy/test",
        prompt_tokens=11,
        completion_tokens=22,
        finish_reason="stop",
        reasoning=reasoning_text,
    )


class ReasoningFromTests(unittest.TestCase):
    def test_it_reads_reasoning_content(self):
        message = mock.Mock(reasoning_content="thinking", reasoning=None, reasoning_details=None)
        self.assertEqual(system2.reasoning_from(message), "thinking")

    def test_it_reads_a_plain_reasoning_field(self):
        message = mock.Mock(reasoning="inner monologue", reasoning_content=None, reasoning_details=None)
        self.assertEqual(system2.reasoning_from(message), "inner monologue")

    def test_it_reads_reasoning_details_parts(self):
        message = mock.Mock(
            reasoning_content=None,
            reasoning=None,
            reasoning_details=[{"text": "first"}, {"content": "second"}],
        )
        self.assertEqual(system2.reasoning_from(message), "first\nsecond")

    def test_a_plain_model_has_no_reasoning(self):
        message = mock.Mock(reasoning_content=None, reasoning=None, reasoning_details=None)
        self.assertEqual(system2.reasoning_from(message), "")
        self.assertEqual(system2.reasoning_from(None), "")


class ThoughtLinesTests(unittest.TestCase):
    def test_absent_reasoning_says_so(self):
        self.assertEqual(reasoning.thought_lines(""), ["(the model returned no visible reasoning)"])

    def test_it_caps_the_number_of_lines(self):
        lines = reasoning.thought_lines("\n".join(f"line {i}" for i in range(200)))
        self.assertLessEqual(len(lines), reasoning.MAX_THOUGHT_LINES + 1)
        self.assertEqual(lines[-1], "\u2026 (reasoning truncated)")

    def test_it_caps_total_characters(self):
        long_line = "x" * (reasoning.MAX_THOUGHT_CHARS + 10)
        lines = reasoning.thought_lines(f"{long_line}\nsecond")
        self.assertEqual(lines, ["\u2026 (reasoning truncated)"])

    def test_short_reasoning_passes_through_whole(self):
        self.assertEqual(reasoning.thought_lines("one\ntwo"), ["one", "two"])


class ArchitectAnswerSeamTests(unittest.TestCase):
    def test_disabled_system2_yields_no_answer(self):
        with mock.patch.dict(os.environ, {"DEEPAGENTS_SYSTEM2": "0"}, clear=False):
            answer = reasoning.architect_answer(prompt="anything")
        self.assertFalse(answer.used_llm)
        self.assertEqual(answer.text, "")

    def test_a_configured_call_returns_text_and_reasoning(self):
        with mock.patch.dict(os.environ, FAKE_ENV, clear=False):
            answer = reasoning.architect_answer(
                prompt="Recommend something.", completer=lambda **kw: _completion()
            )
        self.assertTrue(answer.used_llm)
        self.assertEqual(answer.text, "A real answer.")
        self.assertEqual(answer.reasoning, "Step one.\nStep two.")
        self.assertEqual(answer.model, "openai:policy/test")
        self.assertIn("11 in", answer.usage_line())

    def test_a_failed_call_degrades_to_no_answer(self):
        def boom(**kwargs):
            raise RuntimeError("network down")

        with mock.patch.dict(os.environ, FAKE_ENV, clear=False):
            answer = reasoning.architect_answer(prompt="x", completer=boom)
        self.assertFalse(answer.used_llm)
        self.assertIn("network down", answer.error)

    def test_an_empty_completion_is_not_used(self):
        with mock.patch.dict(os.environ, FAKE_ENV, clear=False):
            answer = reasoning.architect_answer(
                prompt="x", completer=lambda **kw: _completion(text="", reasoning_text="")
            )
        self.assertFalse(answer.used_llm)
        self.assertEqual(answer.error, "empty completion")

    def test_answer_lines_strip_bullets(self):
        answer = reasoning.ArchitectAnswer(text="- one\n* two\n\n three")
        self.assertEqual(answer.answer_lines(), ["one", "two", "three"])


class RecommendUsesTheModelTests(unittest.TestCase):
    """The button reports the model's answer, and its thought process, when one exists."""

    def _run(self, plan_state, completion):
        from orchestration.workflow import actions_admin
        from orchestration.workflow.context import WorkflowContext

        events = []
        ctx = WorkflowContext(
            emit_fn=events.append,
            stream_text=lambda agent, text, log_type="log", delay=0.0: events.append(
                {"type": "log", "agent": agent, "log_type": log_type, "text": text}
            ),
            should_stop=lambda: False,
            plan_file="PLAN.md",
        )
        with mock.patch.dict(os.environ, FAKE_ENV, clear=False), \
                mock.patch.object(system2, "complete", completion), \
                mock.patch.object(actions_admin.time, "sleep", lambda *a, **k: None):
            actions_admin.recommend_action(ctx, plan_state)
        return events

    def _plan(self):
        return {
            "title": "Demo",
            "steps": [
                {"id": "task-1", "title": "Done thing", "status": "completed", "tag": "API", "files": ["a.py"]},
                {"id": "task-2", "title": "Next thing", "status": "pending", "tag": "BE", "files": []},
            ],
        }

    def test_the_model_reasoning_reaches_the_card(self):
        events = self._run(
            self._plan(),
            lambda **kw: _completion(
                text="- Ship the endpoint\n- Add the migration", reasoning_text="Weighing two options."
            ),
        )
        reasoning_logs = [e for e in events if e.get("log_type") == "reasoning"]
        self.assertTrue(reasoning_logs, "no reasoning was streamed into the card")
        self.assertTrue(any("Weighing two options." in e["text"] for e in reasoning_logs))
        self.assertTrue(any(reasoning.THOUGHT_LABEL in e["text"] for e in reasoning_logs))

    def test_the_model_answer_becomes_the_proposals(self):
        events = self._run(
            self._plan(),
            lambda **kw: _completion(text="- Ship the endpoint\n- Add the migration"),
        )
        summary = [e for e in events if e["type"] == "architect_summary"][-1]["summary"]
        self.assertEqual(summary["proposals"], ["Ship the endpoint", "Add the migration"])
        self.assertTrue(any("System 2 call:" in d for d in summary["deliverables"]))
        self.assertTrue(any("Ship the endpoint" in e.get("text", "") for e in events if e["type"] == "log"))

    def test_without_a_provider_the_scripted_proposals_stand(self):
        with mock.patch.dict(os.environ, {"DEEPAGENTS_SYSTEM2": "0"}, clear=False):
            from orchestration.workflow import actions_admin
            from orchestration.workflow.context import WorkflowContext

            events = []
            ctx = WorkflowContext(
                emit_fn=events.append,
                stream_text=lambda *a, **k: None,
                should_stop=lambda: False,
                plan_file="PLAN.md",
            )
            with mock.patch.object(actions_admin.time, "sleep", lambda *a, **k: None):
                actions_admin.recommend_action(ctx, self._plan())

        summary = [e for e in events if e["type"] == "architect_summary"][-1]["summary"]
        self.assertEqual(
            summary["proposals"][0], "Execute immediate next task: 'Next thing'"
        )
        self.assertFalse(any("System 2 call:" in d for d in summary["deliverables"]))
        self.assertFalse([e for e in events if e.get("log_type") == "reasoning"])


if __name__ == "__main__":
    unittest.main()
