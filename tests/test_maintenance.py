"""Phase 46: state pruning and storage reclamation.

Two things a daemon that runs for months cannot leave unbounded: the *age* of the rows it keeps
(the heavy ``context_blob`` column grows every step) and the *space* a delete leaves behind (SQLite
does not shrink a file, and the WAL grows until something checkpoints it). These drive the sweeper
against real databases and a real shadow directory.
"""

import os
import shutil
import sqlite3
import tempfile
import time
import unittest

from storage import maintenance, migrations
from storage.intents import IntentLedger


def _write(root, name, text):
    path = os.path.join(root, name)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


class _LedgerCase(unittest.TestCase):
    def setUp(self):
        from storage.connection import STATE_ROOT_ENV
        from tools import workspace

        self.tmp = tempfile.mkdtemp(prefix="aleth_maint_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # A **private** workspace, so this test's state directory -- and therefore the staging base a
        # shadow lands in -- is its own. The real base is shared and mutable: ``test_staging`` wipes
        # it in ``setUp`` and ``test_retention`` sweeps it, so a shadow created there can be
        # collected by another worker before this test looks at it. Isolating the pointer is what
        # makes these tests deterministic under ``-n auto``.
        self._saved = (workspace.PLAN_DIR, workspace.PROJECT_DIR, os.environ.get(STATE_ROOT_ENV))
        workspace.PLAN_DIR = self.tmp
        workspace.PROJECT_DIR = self.tmp
        workspace.publish_state_root()
        self._state_dir = workspace.state_dir()
        self.addCleanup(self._restore_workspace)
        self.db_path = os.path.join(self.tmp, "state.db")
        self.ledger = IntentLedger(self.db_path)

    def _restore_workspace(self):
        from storage.connection import STATE_ROOT_ENV
        from tools import staging, workspace

        staging.remove_tree(staging.staging_base())
        shutil.rmtree(self._state_dir, ignore_errors=True)
        workspace.PLAN_DIR, workspace.PROJECT_DIR = self._saved[0], self._saved[1]
        root = self._saved[2]
        if root is None:
            os.environ.pop(STATE_ROOT_ENV, None)
        else:
            os.environ[STATE_ROOT_ENV] = root

    def _intent(self, intent_id, status):
        from api.intents import Intent

        intent = Intent(id=intent_id, action_type="custom", message="go",
                        action_params={}, enqueued_at=0.0)
        self.ledger.record(intent)
        self.assertTrue(self.ledger.claim(intent))
        if status != "running":
            intent.status = status
            self.ledger.settle(intent)
        return intent

    @staticmethod
    def _far_future():
        """A clock far enough ahead that a 30-day TTL has certainly elapsed."""
        return time.time() + 100 * 86400


class PurgeTests(_LedgerCase):
    def test_an_old_terminal_intent_is_purged_with_its_step_rows(self):
        self._intent("old-done", "completed")
        self._intent("old-failed", "failed")
        self._intent("live", "running")
        self.ledger.record_step_spend("old-done", 1, prompt=1000, completion=0)
        self.ledger.record_step_context("old-done", 1, "a heavy blob")
        self.ledger.record_step_spend("live", 1, prompt=1000, completion=0)

        report = maintenance.purge_expired(
            self.db_path, ttl_days=30, now=self._far_future()
        )

        self.assertEqual(report["intents"], 2)
        self.assertEqual(report["steps"], 1)
        self.assertEqual(self.ledger.status_of("old-done"), "")
        self.assertEqual(self.ledger.status_of("live"), "running")
        self.assertEqual(self.ledger.step_high_water("old-done"), 0)
        self.assertEqual(self.ledger.step_high_water("live"), 1)

    def test_a_recent_terminal_intent_is_kept(self):
        self._intent("recent", "completed")

        report = maintenance.purge_expired(self.db_path, ttl_days=30)

        self.assertEqual(report["intents"], 0)
        self.assertEqual(self.ledger.status_of("recent"), "completed")

    def test_a_live_intent_is_never_purged(self):
        """The TTL is about age, and a run in flight has not finished ageing -- it has not finished."""
        self._intent("running-now", "running")

        report = maintenance.purge_expired(self.db_path, ttl_days=0, now=self._far_future())

        self.assertEqual(report["intents"], 0)
        self.assertEqual(self.ledger.status_of("running-now"), "running")

    def test_a_purged_intent_takes_its_fault_rows_with_it(self):
        from storage import telemetry

        self._intent("old-failed", "failed")
        telemetry.record_agent_fault(
            self.db_path, intent_id="old-failed", kind="STEP_LIMIT_EXCEEDED"
        )

        report = maintenance.purge_expired(
            self.db_path, ttl_days=30, now=self._far_future()
        )

        self.assertEqual(report["faults"], 1)
        self.assertEqual(telemetry.agent_faults(self.db_path, intent_id="old-failed"), [])

    def test_an_expired_intent_takes_its_shadow_with_it(self):
        from tools import staging

        host = tempfile.mkdtemp(prefix="aleth_maint_host_")
        self.addCleanup(shutil.rmtree, host, ignore_errors=True)
        _write(host, "mod.py", "x = 1\n")
        shadow = staging.create_staging(host, intent_id="old-shadow")
        self.addCleanup(staging.remove_tree, shadow.path)
        self.assertTrue(os.path.isdir(shadow.path))
        self._intent("old-shadow", "completed")

        report = maintenance.purge_expired(
            self.db_path, ttl_days=30, now=self._far_future()
        )

        self.assertEqual(report["staging"], 1)
        self.assertFalse(os.path.isdir(shadow.path), "the shadow was left on disk")

    def test_a_missing_database_is_not_a_fault(self):
        report = maintenance.purge_expired(os.path.join(self.tmp, "absent.db"))

        self.assertEqual(report, {"intents": 0, "steps": 0, "faults": 0, "staging": 0})


class ReclaimTests(_LedgerCase):
    def test_a_fresh_database_uses_incremental_auto_vacuum(self):
        connection = sqlite3.connect(self.db_path)
        try:
            mode = int(connection.execute("PRAGMA auto_vacuum").fetchone()[0])
        finally:
            connection.close()

        # 2 is INCREMENTAL: the sweeper can release pages without a full VACUUM.
        self.assertEqual(mode, 2)

    def test_the_wal_is_truncated(self):
        # The WAL is removed when the last connection closes, so the writer stays open: this is
        # also the realistic case -- the sweeper runs while the daemon is holding the database.
        writer = sqlite3.connect(self.db_path)
        try:
            for step in range(1, 300):
                writer.execute(
                    "INSERT INTO intent_step_spend"
                    " (intent_id, step, prompt_tokens, completion_tokens, context_blob, recorded_at)"
                    " VALUES ('i1', ?, 0, 0, ?, 0.0)",
                    (step, "a" * 512),
                )
            writer.commit()
            wal = self.db_path + "-wal"
            self.assertTrue(os.path.isfile(wal), "the WAL should exist while a writer is open")
            self.assertGreater(os.path.getsize(wal), 0)

            report = maintenance.reclaim(self.db_path)

            self.assertIsNotNone(report["checkpoint"])
            size = os.path.getsize(wal) if os.path.isfile(wal) else 0
        finally:
            writer.close()
        self.assertEqual(size, 0, "TRUNCATE must empty the log, not merely checkpoint it")

    def test_reclaim_on_a_missing_database_is_not_a_fault(self):
        self.assertEqual(
            maintenance.reclaim(os.path.join(self.tmp, "absent.db"))["checkpoint"], None
        )


class SweeperTests(_LedgerCase):
    def test_run_maintenance_purges_and_reclaims_in_one_pass(self):
        self._intent("old", "completed")
        self.ledger.record_step_context("old", 1, "blob")

        report = maintenance.run_maintenance(
            self.db_path, ttl_days=30, now=self._far_future()
        )

        self.assertEqual(report["purged"]["intents"], 1)
        self.assertIsNotNone(report["reclaimed"]["checkpoint"])

    def test_the_thread_runs_a_pass_and_stops(self):
        thread = maintenance.MaintenanceThread(self.db_path, interval=60.0)
        thread.start()
        try:
            deadline = time.monotonic() + 10.0
            while thread.runs < 1 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertGreaterEqual(thread.runs, 1)
            self.assertIsNotNone(thread.last_report)
        finally:
            started = time.monotonic()
            thread.stop(timeout=5.0)
        self.assertLess(time.monotonic() - started, 5.0, "stop must not wait out the interval")

    def test_starting_twice_yields_one_thread(self):
        first = maintenance.start_maintenance(self.db_path)
        self.addCleanup(maintenance.stop_maintenance)
        second = maintenance.start_maintenance(self.db_path)

        self.assertIs(first, second)


if __name__ == "__main__":
    unittest.main()
