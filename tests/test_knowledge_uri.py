"""Phase 5 Batch 1: the ``kernel://`` address scheme and the knowledge-graph schema.

    .\\venv\\Scripts\\python.exe -m pytest tests/test_knowledge_uri.py -n0 -q

The URI tests are deliberately heavy on *refusals*. A URI is a primary key, so a parser that
accepts a malformed address does not produce a formatting bug -- it produces a second entity
for something that already exists, which is the failure this module exists to prevent.
"""

import os
import sqlite3
import tempfile
import unittest

from storage import knowledge_uri as kuri
from storage.db import PlanStore, reset_stores


class SymbolUriTests(unittest.TestCase):
    def test_a_symbol_uri_round_trips(self):
        uri = kuri.make_symbol_uri("tools/ast_tools.py", "edit_ast_node")
        self.assertEqual(uri, "kernel://symbol/tools/ast_tools.py:edit_ast_node")

        parsed = kuri.parse_uri(uri)
        self.assertEqual(parsed.scheme, "kernel")
        self.assertEqual(parsed.entity_type, "symbol")
        self.assertEqual(parsed.path, "tools/ast_tools.py")
        self.assertEqual(parsed.symbol_name, "edit_ast_node")
        self.assertEqual(parsed.raw_uri, uri)
        self.assertTrue(parsed.is_symbol)

    def test_a_qualified_symbol_name_is_accepted(self):
        parsed = kuri.parse_uri("kernel://symbol/app/models.py:Greeter.bye")
        self.assertEqual(parsed.symbol_name, "Greeter.bye")
        self.assertEqual(parsed.path, "app/models.py")

    def test_the_last_colon_separates_path_from_symbol(self):
        """A Windows-shaped path must not be split at its drive colon."""
        parsed = kuri.parse_uri("kernel://symbol/pkg/mod.py:alpha")
        self.assertEqual(parsed.path, "pkg/mod.py")
        self.assertEqual(parsed.symbol_name, "alpha")

    def test_a_symbol_uri_without_a_symbol_is_refused(self):
        with self.assertRaises(kuri.InvalidKnowledgeURIError):
            kuri.parse_uri("kernel://symbol/tools/ast_tools.py")

    def test_a_symbol_uri_without_a_path_is_refused(self):
        with self.assertRaises(kuri.InvalidKnowledgeURIError):
            kuri.parse_uri("kernel://symbol/:edit_ast_node")

    def test_a_symbol_uri_with_a_bad_symbol_name_is_refused(self):
        for candidate in ("kernel://symbol/a.py:1bad", "kernel://symbol/a.py:a-b", "kernel://symbol/a.py:a b"):
            with self.assertRaises(kuri.InvalidKnowledgeURIError, msg=candidate):
                kuri.parse_uri(candidate)


class RuleTaskFileUriTests(unittest.TestCase):
    def test_a_rule_uri_round_trips(self):
        uri = kuri.make_rule_uri("chokehold_refuse_overwrite")
        self.assertEqual(uri, "kernel://rule/chokehold_refuse_overwrite")
        parsed = kuri.parse_uri(uri)
        self.assertEqual(parsed.entity_type, "rule")
        self.assertEqual(parsed.path, "chokehold_refuse_overwrite")
        self.assertIsNone(parsed.symbol_name)

    def test_a_task_uri_round_trips(self):
        uri = kuri.make_task_uri("task-104")
        self.assertEqual(uri, "kernel://task/task-104")
        self.assertEqual(kuri.parse_uri(uri).path, "task-104")

    def test_a_file_uri_round_trips(self):
        uri = kuri.make_file_uri("orchestration/runner.py")
        self.assertEqual(uri, "kernel://file/orchestration/runner.py")
        parsed = kuri.parse_uri(uri)
        self.assertEqual(parsed.entity_type, "file")
        self.assertEqual(parsed.path, "orchestration/runner.py")
        self.assertIsNone(parsed.symbol_name)

    def test_a_rule_or_task_id_is_one_segment_not_a_path(self):
        for candidate in ("kernel://rule/a/b", "kernel://task/a/b"):
            with self.assertRaises(kuri.InvalidKnowledgeURIError, msg=candidate):
                kuri.parse_uri(candidate)


class DeterminismTests(unittest.TestCase):
    def test_backslashes_normalise_to_the_same_uri(self):
        """The same file must not fork the graph by platform."""
        self.assertEqual(
            kuri.make_file_uri("tools\\ast_tools.py"),
            kuri.make_file_uri("tools/ast_tools.py"),
        )
        self.assertEqual(
            kuri.make_symbol_uri("tools\\ast_tools.py", "edit_ast_node"),
            kuri.make_symbol_uri("tools/ast_tools.py", "edit_ast_node"),
        )

    def test_a_leading_dot_slash_is_refused_rather_than_normalised(self):
        """One spelling per entity: the fix is to refuse, not to guess which spelling was meant.

        Stripping a leading ``./`` would make the parser accept two spellings of one key, which
        is the duplicate-entity failure this module exists to prevent.
        """
        with self.assertRaises(kuri.InvalidKnowledgeURIError):
            kuri.make_file_uri("./main.py")


class RefusalTests(unittest.TestCase):
    def test_a_missing_scheme_is_refused(self):
        for candidate in ("symbol/a.py:alpha", "/kernel://file/a.py", "kernel:/file/a.py", ""):
            with self.assertRaises(kuri.InvalidKnowledgeURIError, msg=candidate):
                kuri.parse_uri(candidate)

    def test_an_uppercase_scheme_is_refused(self):
        """``urlparse`` lowercases a scheme; two spellings of one key is what we must not have."""
        with self.assertRaises(kuri.InvalidKnowledgeURIError):
            kuri.parse_uri("KERNEL://file/main.py")

    def test_an_unknown_entity_type_is_refused(self):
        for candidate in ("kernel://widget/a.py", "kernel://symbol:80/a.py", "kernel:///a.py"):
            with self.assertRaises(kuri.InvalidKnowledgeURIError, msg=candidate):
                kuri.parse_uri(candidate)

    def test_a_query_a_fragment_or_params_are_refused(self):
        for candidate in (
            "kernel://file/a.py?x=1",
            "kernel://file/a.py#frag",
            "kernel://file/a.py;p=1",
        ):
            with self.assertRaises(kuri.InvalidKnowledgeURIError, msg=candidate):
                kuri.parse_uri(candidate)

    def test_a_traversal_is_refused_in_both_factories_and_the_parser(self):
        for candidate in ("kernel://file/../secret.py", "kernel://file/a/../../secret.py"):
            with self.assertRaises(kuri.InvalidKnowledgeURIError, msg=candidate):
                kuri.parse_uri(candidate)
        with self.assertRaises(kuri.InvalidKnowledgeURIError):
            kuri.make_file_uri("../secret.py")
        with self.assertRaises(kuri.InvalidKnowledgeURIError):
            kuri.make_symbol_uri("../secret.py", "alpha")

    def test_an_absolute_path_is_refused_in_both_factories_and_the_parser(self):
        with self.assertRaises(kuri.InvalidKnowledgeURIError):
            kuri.make_file_uri("/etc/passwd")
        with self.assertRaises(kuri.InvalidKnowledgeURIError):
            kuri.parse_uri("kernel://file//etc/passwd")

    def test_whitespace_in_a_path_is_refused(self):
        with self.assertRaises(kuri.InvalidKnowledgeURIError):
            kuri.make_file_uri("my file.py")
        with self.assertRaises(kuri.InvalidKnowledgeURIError):
            kuri.parse_uri("kernel://file/my%20file.py")


class KnowledgeSchemaTests(unittest.TestCase):
    """The two graph tables: constraints, the composite key, and the cascading delete."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = PlanStore(os.path.join(self._tmp.name, "state.db"))

    def _connection(self):
        connection = sqlite3.connect(self.store.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(connection.close)
        return connection

    def _seed(self, connection):
        connection.execute(
            "INSERT INTO knowledge_entities (id, name, type) VALUES (?, ?, ?)",
            ("kernel://file/app.py", "app.py", "file"),
        )
        connection.execute(
            "INSERT INTO knowledge_entities (id, name, type) VALUES (?, ?, ?)",
            ("kernel://symbol/app.py:main", "main", "symbol"),
        )
        connection.execute(
            "INSERT INTO knowledge_synapses (source_id, target_id, relation_type) VALUES (?, ?, ?)",
            ("kernel://symbol/app.py:main", "kernel://file/app.py", "modifies"),
        )

    def test_both_tables_and_indexes_exist(self):
        connection = self._connection()
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        indexes = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        self.assertIn("knowledge_entities", tables)
        self.assertIn("knowledge_synapses", tables)
        self.assertIn("idx_synapses_source", indexes)
        self.assertIn("idx_synapses_target", indexes)

    def test_foreign_keys_are_enforced_on_a_fresh_connection(self):
        connection = self._connection()
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_a_synapse_to_a_missing_entity_is_refused(self):
        connection = self._connection()
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO knowledge_synapses (source_id, target_id, relation_type) VALUES (?, ?, ?)",
                ("kernel://file/ghost.py", "kernel://file/also-ghost.py", "calls"),
            )

    def test_deleting_an_entity_cascades_to_its_synapses(self):
        connection = self._connection()
        self._seed(connection)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM knowledge_synapses").fetchone()[0], 1
        )

        connection.execute(
            "DELETE FROM knowledge_entities WHERE id = ?", ("kernel://file/app.py",)
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM knowledge_synapses").fetchone()[0], 0
        )

    def test_the_edge_triple_is_the_identity(self):
        """The same relation asserted twice is one row, not a duplicate edge."""
        connection = self._connection()
        self._seed(connection)
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO knowledge_synapses (source_id, target_id, relation_type) VALUES (?, ?, ?)",
                ("kernel://symbol/app.py:main", "kernel://file/app.py", "modifies"),
            )

    def test_an_unknown_entity_type_is_refused_by_the_check_constraint(self):
        connection = self._connection()
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO knowledge_entities (id, name, type) VALUES (?, ?, ?)",
                ("kernel://widget/x", "x", "widget"),
            )

    def test_an_unknown_relation_type_is_refused_by_the_check_constraint(self):
        connection = self._connection()
        self._seed(connection)
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO knowledge_synapses (source_id, target_id, relation_type) VALUES (?, ?, ?)",
                ("kernel://file/app.py", "kernel://symbol/app.py:main", "obliterates"),
            )

    def test_a_synapse_defaults_its_weight_and_timestamp(self):
        connection = self._connection()
        self._seed(connection)
        row = connection.execute(
            "SELECT weight, created_at FROM knowledge_synapses"
        ).fetchone()
        self.assertEqual(row["weight"], 1.0)
        self.assertTrue(str(row["created_at"]).strip())


class UriAndSchemaAgreeTests(unittest.TestCase):
    """The parser and the schema must not disagree about what an entity is."""

    def test_the_factory_kinds_match_the_check_constraint(self):
        self.assertEqual(
            kuri.ENTITY_TYPES, frozenset({"symbol", "rule", "task", "file"})
        )


if __name__ == "__main__":
    unittest.main()
