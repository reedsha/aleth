"""Phase 7.5: the skill registry -- storage, the single-query match, and the injection point.

A skill is *data*: the registry is a table, so a playbook can be added or edited without a code
change. These tests hold the three halves of that claim: the schema and its idempotent seed, the
one-query match against a node's declaration, and the point where the Markdown becomes part of a
system prompt.
"""

import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from storage.db import DEFAULT_SKILLS, apply_skills_schema, get_store, reset_stores
from tools import workspace


class SkillRegistryTestCase(unittest.TestCase):
    """A real store on a throwaway plan directory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aleth_skills_")
        self._orig = (workspace.PLAN_DIR, workspace.PROJECT_DIR, workspace.ACTIVE_PLAN_FILE)
        workspace.PLAN_DIR = self.tmp
        workspace.PROJECT_DIR = self.tmp
        workspace.ACTIVE_PLAN_FILE = "PLAN.md"
        workspace.publish_state_root()
        reset_stores()
        self.addCleanup(self._restore)

    def _restore(self):
        workspace.PLAN_DIR, workspace.PROJECT_DIR, workspace.ACTIVE_PLAN_FILE = self._orig
        reset_stores()
        shutil.rmtree(self.tmp, ignore_errors=True)


class SkillStorageTests(SkillRegistryTestCase):
    def test_a_fresh_store_has_the_registry_and_the_built_in_skill(self):
        store = get_store()
        with sqlite3.connect(store.path) as connection:
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            rows = connection.execute(
                "SELECT id, target_capabilities FROM skills"
            ).fetchall()
        self.assertIn("skills", tables)
        self.assertEqual(rows, [("TDD_Execution_Skill", '["exec", "fs"]')])

    def test_the_seed_is_idempotent(self):
        """A second construction must not duplicate or overwrite the registry."""
        get_store()
        reset_stores()
        store = get_store()
        with sqlite3.connect(store.path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
        self.assertEqual(count, len(DEFAULT_SKILLS))

    def test_the_verified_column_is_present(self):
        """Phase 7.5 adds the engine's receipt to an existing table, not only a fresh one."""
        store = get_store()
        with sqlite3.connect(store.path) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        self.assertIn("verified", columns)

    def test_apply_skills_schema_builds_the_real_table_on_a_caller_connection(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        apply_skills_schema(connection)
        rows = connection.execute("SELECT id FROM skills").fetchall()
        self.assertEqual([row["id"] for row in rows], [skill[0] for skill in DEFAULT_SKILLS])

    def test_apply_skills_schema_can_skip_the_seed(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        apply_skills_schema(connection, seed=False)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM skills").fetchone()[0], 0)


class _CountingConnection:
    """A connection that counts statements, so "one query" can be asserted, not assumed."""

    def __init__(self, connection):
        self._connection = connection
        self.calls = 0

    def execute(self, sql, parameters=()):
        self.calls += 1
        return self._connection.execute(sql, parameters)


class SkillRetrievalTests(SkillRegistryTestCase):
    def _block(self, capabilities):
        from orchestration import retriever

        return retriever.skills_for_capabilities(capabilities)

    def test_the_declaration_selects_the_matching_skill(self):
        self.assertIn("Test-Driven Execution", self._block(["exec"]))
        # Either capability in the target set matches: the skill is declared for both.
        self.assertIn("Test-Driven Execution", self._block(["fs"]))

    def test_an_empty_declaration_selects_nothing(self):
        """The diet is enforced: a node that declared nothing gets no playbook."""
        self.assertEqual(self._block([]), "")
        self.assertEqual(self._block(None), "")

    def test_a_declaration_that_matches_no_skill_selects_nothing(self):
        self.assertEqual(self._block(["ast"]), "")

    def test_the_match_is_exactly_one_query(self):
        """No N+1: the intersection is made by SQLite in a single statement."""
        from orchestration import retriever

        connection = sqlite3.connect(get_store().path)
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        counted = _CountingConnection(connection)

        block = retriever.skills_for_capabilities(["exec", "fs"], connection=counted)

        self.assertIn("Test-Driven Execution", block)
        self.assertEqual(counted.calls, 1, "the skill match must be one query, not N+1")

    def test_the_match_reads_the_json_array_not_a_substring(self):
        """``exec`` must not match ``executive``: the comparison is over JSON values."""
        store = get_store()
        with sqlite3.connect(store.path) as connection:
            connection.execute(
                "INSERT INTO skills (id, name, target_capabilities, markdown_content)"
                " VALUES ('Decoy', 'Decoy', ?, '## Decoy')",
                ('["executive"]',),
            )
        self.assertNotIn("Decoy", self._block(["exec"]))


class SkillInjectionTests(unittest.TestCase):
    """The transport composes the prompt; it does not look skills up."""

    def test_no_skills_leaves_the_prompt_untouched(self):
        from orchestration.system2 import compose_system_prompt

        self.assertEqual(compose_system_prompt("ROLE", ""), "ROLE")
        self.assertEqual(compose_system_prompt("ROLE", "   "), "ROLE")

    def test_the_skills_are_prepended_with_a_heading(self):
        from orchestration.system2 import compose_system_prompt

        composed = compose_system_prompt("ROLE", "## Skill\n\nbody")
        self.assertTrue(composed.startswith("## Operating Procedures (skills)"))
        self.assertIn("## Skill", composed)
        self.assertTrue(composed.endswith("ROLE"))

    def test_the_single_shot_call_sends_the_composed_prompt(self):
        from orchestration import system2

        sent = {}

        class _Completions:
            @staticmethod
            def create(**kwargs):
                sent.update(kwargs)
                raise RuntimeError("stop after capturing the payload")

        class _Client:
            def __init__(self, **_kwargs):
                self.chat = mock.Mock(completions=_Completions())

        system2.complete(
            model="openai:x", system="ROLE", user="hi", client=_Client(),
            skills="## Skill\n\nbody",
        )
        self.assertIn("## Operating Procedures (skills)", sent["messages"][0]["content"])
        self.assertIn("ROLE", sent["messages"][0]["content"])

    def test_the_tool_call_sends_the_composed_prompt(self):
        from orchestration import system2

        sent = {}

        class _Completions:
            @staticmethod
            def create(**kwargs):
                sent.update(kwargs)
                raise RuntimeError("stop after capturing the payload")

        class _Client:
            def __init__(self, **_kwargs):
                self.chat = mock.Mock(completions=_Completions())

        system2.complete_with_tools(
            model="openai:x", system="ROLE", messages=[{"role": "user", "content": "hi"}],
            tools=[], client=_Client(), skills="## Skill\n\nbody",
        )
        self.assertIn("## Operating Procedures (skills)", sent["messages"][0]["content"])
        self.assertIn("ROLE", sent["messages"][0]["content"])


class PlannerSkillTests(SkillRegistryTestCase):
    def test_the_planner_hands_the_selected_skills_to_the_transport(self):
        from orchestration.workflow import planner

        seen = {}

        class _Completion:
            text = (
                '{"plan_id":"PLAN","task_id":"task-1","summary":"s","estimated_impact":"i",'
                '"complexity_score":1,"required_capabilities":[],"ast_targets":[]}'
            )
            tool_calls: list = []

        def fake_complete_with_tools(*, model, system, messages, tools,
                                     base_url=None, api_key=None, skills=""):
            seen["skills"] = skills
            return _Completion()

        class _Session:
            def get_bound_tools(self, role):
                return []

        with mock.patch("orchestration.system2.is_enabled", return_value=True), \
                mock.patch("orchestration.system2.complete_with_tools", fake_complete_with_tools):
            planner.plan_task(
                {"id": "task-1", "title": "x", "files": []},
                plan_id="PLAN", workspace_dir=self.tmp, session=_Session(),
                model="openai:x", capabilities=["exec"],
            )

        self.assertIn("Test-Driven Execution", seen["skills"])

    def test_a_node_that_declared_nothing_is_sent_no_skills(self):
        from orchestration.workflow import planner

        seen = {}

        class _Completion:
            text = (
                '{"plan_id":"PLAN","task_id":"task-1","summary":"s","estimated_impact":"i",'
                '"complexity_score":1,"required_capabilities":[],"ast_targets":[]}'
            )
            tool_calls: list = []

        def fake_complete_with_tools(*, model, system, messages, tools,
                                     base_url=None, api_key=None, skills=""):
            seen["skills"] = skills
            return _Completion()

        class _Session:
            def get_bound_tools(self, role):
                return []

        with mock.patch("orchestration.system2.is_enabled", return_value=True), \
                mock.patch("orchestration.system2.complete_with_tools", fake_complete_with_tools):
            planner.plan_task(
                {"id": "task-1", "title": "x", "files": []},
                plan_id="PLAN", workspace_dir=self.tmp, session=_Session(),
                model="openai:x", capabilities=[],
            )

        self.assertEqual(seen["skills"], "")


if __name__ == "__main__":
    unittest.main()
