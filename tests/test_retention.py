"""Phase 22: the bound on the engine's own state.

Two properties, and they are the two ways a single-machine engine can grow without limit: an
append-only ledger with no end, and a shadow workspace nothing will ever collect.

These drive the real ``sweep_state`` -- the function ``main.py`` runs at boot -- rather than its
pieces, because the *ordering* inside it is the part that carries the safety argument. An intent
whose engine died has to be terminal before anything is judged against it, or its shadow is
spared on this boot and only collected on the next one.
"""

import contextlib
import dataclasses
import io
import os
import shutil
import sqlite3
import tempfile
import unittest

from storage import intents, retention, telemetry
from storage.intents import IntentLedger
from tools import staging


def _write(root: str, name: str, text: str) -> None:
    path = os.path.join(root, name)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


@dataclasses.dataclass
class _Intent:
    """What the ledger needs of an intent, and nothing else."""

    id: str
    action_type: str = "custom"
    message: str = "do the thing"
    enqueued_at: float = 0.0
    status: str = "queued"
    error: str = ""


class RetentionTestCase(unittest.TestCase):
    """A state database, a host tree, and a clean staging base, per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aleth_retention_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.host = tempfile.mkdtemp(prefix="aleth_retention_host_")
        self.addCleanup(shutil.rmtree, self.host, ignore_errors=True)
        self.db_path = os.path.join(self.tmp, "aleth_state.db")
        _write(self.host, "pkg/mod.py", "value = 1\n")
        # A shadow left by an earlier test would be judged by this one's sweep.
        staging.remove_tree(staging.staging_base())
        self.addCleanup(shutil.rmtree, staging.staging_base(), ignore_errors=True)

    def _build_ledgers(self):
        """The real DDL, on a bare connection: ``prune_ledgers`` never creates a table."""
        connection = sqlite3.connect(self.db_path)
        try:
            telemetry.apply_telemetry_schema(connection)
            intents.apply_intent_schema(connection)
        finally:
            connection.close()


class LedgerPruningTests(RetentionTestCase):
    def test_every_ledger_is_trimmed_to_the_last_rows(self):
        self._build_ledgers()
        for index in range(6):
            telemetry.record(self.db_path, execution_id=f"exec-{index}")
        for index in range(4):
            telemetry.record_routing_decision(self.db_path, route="coder", task_id=f"t-{index}")
        ledger = IntentLedger(self.db_path)
        for index in range(5):
            ledger.record(_Intent(f"intent-{index}"))

        removed = retention.prune_ledgers(self.db_path, keep=3)

        self.assertEqual(removed["execution_telemetry"], 3)
        self.assertEqual(removed["routing_decisions"], 1)
        self.assertEqual(removed["intent_ledger"], 2)
        self.assertEqual(telemetry.count(self.db_path), 3)
        self.assertEqual(len(telemetry.recent_routing_decisions(self.db_path, limit=50)), 3)
        self.assertEqual(len(ledger.recent(limit=50)), 3)

    def test_a_ledger_below_its_bound_loses_nothing(self):
        self._build_ledgers()
        for index in range(3):
            telemetry.record(self.db_path, execution_id=f"exec-{index}")

        removed = retention.prune_ledgers(self.db_path, keep=1000)

        self.assertEqual(removed["execution_telemetry"], 0)
        self.assertEqual(telemetry.count(self.db_path), 3)

    def test_a_ledger_that_was_never_created_is_skipped(self):
        """Nothing has ever run, so there is nothing to bound -- and nothing to create either."""
        sqlite3.connect(self.db_path).close()
        self.assertEqual(retention.prune_ledgers(self.db_path, keep=10), {})


class IntentStatusQueryTests(RetentionTestCase):
    def test_ids_with_status_answers_only_what_was_asked(self):
        ledger = IntentLedger(self.db_path)
        ledger.record(_Intent("i-queued"))
        running = _Intent("i-running")
        ledger.record(running)
        self.assertTrue(ledger.claim(running))
        finished = _Intent("i-done")
        ledger.record(finished)
        finished.status = "completed"
        ledger.settle(finished)

        self.assertEqual(ledger.ids_with_status("queued"), frozenset({"i-queued"}))
        self.assertEqual(
            ledger.ids_with_status("running", "completed"), frozenset({"i-running", "i-done"})
        )
        # No statuses is no question: an empty predicate must not become "everything".
        self.assertEqual(ledger.ids_with_status(), frozenset())


class SweepStateTests(RetentionTestCase):
    """The boot sweep: fail the unfinished, trim the ledgers, collect the dead shadows."""

    def test_a_crashed_intent_is_failed_and_its_shadow_collected(self):
        ledger = IntentLedger(self.db_path)
        crashed = _Intent("i-crashed")
        ledger.record(crashed)
        self.assertTrue(ledger.claim(crashed))  # written, and then the engine stopped
        shadow = staging.create_staging(self.host, intent_id="i-crashed")

        report = retention.sweep_state(self.db_path)

        self.assertEqual(report["reconciled"], 1)
        self.assertEqual(report["purged_stagings"], [shadow.staging_id])
        self.assertFalse(os.path.exists(shadow.path))
        self.assertEqual(ledger.recent(1)[0]["status"], "failed")
        # The host is not the shadow's to touch, on any path.
        with open(os.path.join(self.host, "pkg", "mod.py"), "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "value = 1\n")

    def test_a_finished_intent_keeps_its_shadow_for_review(self):
        ledger = IntentLedger(self.db_path)
        finished = _Intent("i-finished")
        ledger.record(finished)
        self.assertTrue(ledger.claim(finished))
        finished.status = "completed"
        ledger.settle(finished)
        shadow = staging.create_staging(self.host, intent_id="i-finished")

        report = retention.sweep_state(self.db_path)

        self.assertEqual(report["reconciled"], 0)
        self.assertEqual(report["purged_stagings"], [])
        self.assertTrue(os.path.isdir(shadow.path))

    def test_a_project_that_never_ran_collects_its_stray_shadows(self):
        """No ledger means no intent can own a shadow, so every shadow on disk is residue."""
        shadow = staging.create_staging(self.host, intent_id="i-unknown")

        report = retention.sweep_state(self.db_path)

        self.assertEqual(report["reconciled"], 0)
        self.assertEqual(report["pruned"], {})
        self.assertEqual(report["purged_stagings"], [shadow.staging_id])
        self.assertFalse(os.path.exists(shadow.path))
        # The sweep tidy up an absent state database; it does not bring one into being.
        self.assertFalse(os.path.isfile(self.db_path))

    def test_the_sweep_says_what_it_collected(self):
        """A silent collector hides the crash that produced the garbage."""
        staging.create_staging(self.host, intent_id="i-reported")

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            retention.sweep_state(self.db_path)

        self.assertIn("[State] boot sweep", buffer.getvalue())
        self.assertIn("1 orphaned shadow(s) removed", buffer.getvalue())

    def test_a_sweep_that_cannot_finish_reports_instead_of_raising(self):
        """A boot that cannot tidy is still a boot."""
        from unittest import mock

        self._build_ledgers()
        with mock.patch.object(retention, "prune_ledgers", side_effect=OSError("disk gone")):
            report = retention.sweep_state(self.db_path)

        self.assertIn("disk gone", report["skipped"])
        self.assertEqual(report["purged_stagings"], [])


if __name__ == "__main__":
    unittest.main()
