"""Phase 45: the schema migration engine.

A linear sequence of raw SQL patches, applied in order, each inside one ``BEGIN EXCLUSIVE``
transaction that also records the new version -- so a failed patch leaves behind neither its
statements nor its number, and a half-migrated database cannot exist. These drive the real runner
against real databases, including the upgrade path the engine exists for: a database written before
``cost_cents``.
"""

import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from storage import migrations


def _tables(connection):
    return {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _columns(connection, table):
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


class SequenceTests(unittest.TestCase):
    """The ordering is the contract; a hole in it is a guess about schema order."""

    def test_the_real_sequence_is_contiguous_from_one(self):
        files = migrations.migration_files()

        self.assertEqual([version for version, _ in files], list(range(1, len(files) + 1)))
        self.assertGreaterEqual(len(files), 2, "the cost_cents patch must be present")

    def test_a_gap_in_the_sequence_is_refused(self):
        with tempfile.TemporaryDirectory() as base:
            for name in ("001_a.sql", "003_c.sql"):
                open(os.path.join(base, name), "w", encoding="utf-8").close()

            with self.assertRaises(migrations.MigrationError):
                migrations.migration_files(base)

    def test_a_duplicate_version_is_refused(self):
        with tempfile.TemporaryDirectory() as base:
            for name in ("001_a.sql", "001_b.sql"):
                open(os.path.join(base, name), "w", encoding="utf-8").close()

            with self.assertRaises(migrations.MigrationError):
                migrations.migration_files(base)

    def test_a_non_patch_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as base:
            open(os.path.join(base, "001_a.sql"), "w", encoding="utf-8").close()
            open(os.path.join(base, "README.md"), "w", encoding="utf-8").close()

            self.assertEqual(len(migrations.migration_files(base)), 1)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aleth_migrate_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_path = os.path.join(self.tmp, "state.db")

    def _baseline_dir(self):
        """A directory holding only ``001``, so a database can be built at version 1."""
        base = os.path.join(self.tmp, "baseline")
        os.makedirs(base, exist_ok=True)
        shutil.copy(
            os.path.join(migrations.MIGRATIONS_DIR, "001_initial_schema.sql"),
            os.path.join(base, "001_initial_schema.sql"),
        )
        return base

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def test_a_database_without_the_version_table_is_at_zero(self):
        connection = self._connect()
        try:
            self.assertEqual(migrations.current_version(connection), 0)
        finally:
            connection.close()

    def test_a_fresh_database_is_brought_to_the_newest_version(self):
        version = migrations.migrate_path(self.db_path)

        self.assertEqual(version, migrations.migration_files()[-1][0])
        connection = self._connect()
        try:
            tables = _tables(connection)
            self.assertIn(migrations.VERSION_TABLE, tables)
            # The consolidated baseline really did create every owner's tables.
            for table in ("plans", "tasks", "artifacts", "knowledge_entities", "skills",
                          "intent_ledger", "intent_step_spend", "execution_telemetry",
                          "routing_decisions", "agent_faults"):
                self.assertIn(table, tables, table)
            self.assertIn("cost_micros", _columns(connection, "intent_step_spend"))
            self.assertNotIn("cost_cents", _columns(connection, "intent_step_spend"))
        finally:
            connection.close()

    def test_migrating_twice_is_a_no_op(self):
        first = migrations.migrate_path(self.db_path)

        self.assertEqual(migrations.migrate_path(self.db_path), first)

    def test_an_existing_database_is_upgraded_not_recreated(self):
        """The whole point: a database written before the cost column gets it, not a crash."""
        self.assertEqual(migrations.migrate_path(self.db_path, directory=self._baseline_dir()), 1)

        connection = self._connect()
        try:
            self.assertNotIn(
                "cost_micros", _columns(connection, "intent_step_spend"),
                "the baseline must predate the patch",
            )
        finally:
            connection.close()

        self.assertEqual(migrations.migrate_path(self.db_path), 3)

        connection = self._connect()
        try:
            columns = _columns(connection, "intent_step_spend")
            self.assertIn("cost_micros", columns)
            self.assertNotIn("cost_cents", columns)
        finally:
            connection.close()

    def test_the_upgrade_backfills_the_rows_that_predate_the_column(self):
        migrations.migrate_path(self.db_path, directory=self._baseline_dir())
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO intent_step_spend"
                " (intent_id, step, prompt_tokens, completion_tokens, recorded_at)"
                " VALUES ('i1', 1, 1000, 1000, 0.0)"
            )
            connection.commit()
        finally:
            connection.close()

        migrations.migrate_path(self.db_path)

        connection = self._connect()
        try:
            cost = connection.execute(
                "SELECT cost_micros FROM intent_step_spend WHERE intent_id = 'i1' AND step = 1"
            ).fetchone()[0]
        finally:
            connection.close()
        # 1000 prompt at 2,500 micros/1K + 1000 completion at 10,000 micros/1K = 12,500 micros
        # (1.25 cents), carried through the float column by 002 and frozen as an integer by 003.
        self.assertEqual(int(cost), 12500)

    def test_the_float_cost_column_is_converted_to_micros(self):
        """Phase 46: a v2 database's REAL cents become exact integer micros, and the float is gone."""
        v2 = os.path.join(self.tmp, "v2")
        os.makedirs(v2, exist_ok=True)
        for name in ("001_initial_schema.sql", "002_add_cost_cents.sql"):
            shutil.copy(os.path.join(migrations.MIGRATIONS_DIR, name), os.path.join(v2, name))
        migrations.migrate_path(self.db_path, directory=v2)

        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO intent_step_spend"
                " (intent_id, step, prompt_tokens, completion_tokens, cost_cents, recorded_at)"
                " VALUES ('i1', 1, 1000, 1000, 1.25, 0.0)"
            )
            connection.commit()
        finally:
            connection.close()

        self.assertEqual(migrations.migrate_path(self.db_path), 3)

        connection = self._connect()
        try:
            columns = _columns(connection, "intent_step_spend")
            self.assertIn("cost_micros", columns)
            self.assertNotIn("cost_cents", columns)
            micros = connection.execute(
                "SELECT cost_micros FROM intent_step_spend WHERE intent_id = 'i1' AND step = 1"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(int(micros), 12500)

    def test_a_failing_patch_rolls_back_and_does_not_advance_the_version(self):
        """A patch that fails must leave *nothing*: not its statements, not its number."""
        bad = os.path.join(self.tmp, "bad")
        os.makedirs(bad, exist_ok=True)
        shutil.copy(
            os.path.join(migrations.MIGRATIONS_DIR, "001_initial_schema.sql"),
            os.path.join(bad, "001_initial_schema.sql"),
        )
        with open(os.path.join(bad, "002_bad.sql"), "w", encoding="utf-8") as handle:
            # The first statement succeeds and *must* be rolled back with the second's failure.
            handle.write("CREATE TABLE boom (x INTEGER);\nTHIS IS NOT SQL;\n")

        with self.assertRaises(migrations.MigrationError):
            migrations.migrate_path(self.db_path, directory=bad)

        connection = self._connect()
        try:
            self.assertNotIn("boom", _tables(connection), "the failed patch was not rolled back")
            self.assertEqual(migrations.current_version(connection), 1)
        finally:
            connection.close()

    def test_migrate_on_boot_refuses_when_a_patch_fails(self):
        bad = os.path.join(self.tmp, "badboot")
        os.makedirs(bad, exist_ok=True)
        with open(os.path.join(bad, "001_bad.sql"), "w", encoding="utf-8") as handle:
            handle.write("NOT SQL AT ALL;\n")

        with mock.patch.object(migrations, "MIGRATIONS_DIR", bad):
            with self.assertRaises(migrations.MigrationError):
                migrations.migrate_on_boot(self.db_path)


if __name__ == "__main__":
    unittest.main()
