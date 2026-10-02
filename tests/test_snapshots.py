"""Phase 35: the rewind. A shadow's snapshots, and the ledger that follows them.

Two halves of one guarantee. The *files* are rewound by the shadow's own git repository, and the
*bill* by the per-step rows in the intent ledger -- because a run restored to step 12 must not keep
paying for steps 13-15, and the agent loop must not be handed history for files that no longer
exist.
"""

import os
import shutil
import tempfile
import unittest

from storage.intents import ABORTED_BY_SYSTEM, PAUSED, IntentLedger
from tools import snapshots


def _write(root, name, text):
    path = os.path.join(root, name)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return path


def _read(root, name):
    with open(os.path.join(root, name), "r", encoding="utf-8", newline="") as handle:
        return handle.read()


class SnapshotRepositoryTests(unittest.TestCase):
    """The shadow's own repository: created at birth, committed per step, reverted exactly."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="aleth_snap_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        _write(self.root, "pkg/mod.py", "value = 1\n")

    def test_a_fresh_shadow_is_initialised_at_step_zero(self):
        self.assertTrue(snapshots.init_snapshots(self.root))

        self.assertTrue(snapshots.is_repository(self.root))
        found = snapshots.steps(self.root)
        self.assertEqual([entry["step"] for entry in found], [0])
        # Step 0 is the workspace as handed to the agent, so the tree is not changed by creating it.
        self.assertEqual(_read(self.root, "pkg/mod.py"), "value = 1\n")

    def test_initialising_twice_is_harmless(self):
        self.assertTrue(snapshots.init_snapshots(self.root))
        self.assertFalse(snapshots.init_snapshots(self.root))
        self.assertEqual(len(snapshots.steps(self.root)), 1)

    def test_a_step_that_changes_nothing_is_not_committed(self):
        snapshots.init_snapshots(self.root)

        self.assertFalse(snapshots.has_changes(self.root))
        self.assertEqual(snapshots.commit_step(self.root, 1, "nothing"), "")
        self.assertEqual([entry["step"] for entry in snapshots.steps(self.root)], [0])

    def test_a_step_that_changes_a_file_is_committed_and_tagged(self):
        snapshots.init_snapshots(self.root)

        _write(self.root, "pkg/mod.py", "value = 2\n")
        sha = snapshots.commit_step(self.root, 1, "step 1: write_file")

        self.assertTrue(sha)
        steps = snapshots.steps(self.root)
        self.assertEqual([entry["step"] for entry in steps], [0, 1])
        self.assertIn("write_file", steps[1]["message"])

    def test_the_step_counter_is_monotonic_for_the_shadow(self):
        """Numbering is per *shadow*, not per pass: the autonomous loop drives several planner
        passes over one shadow, so per-pass numbering would let pass two overwrite pass one."""
        snapshots.init_snapshots(self.root)
        self.assertEqual(snapshots.next_step(self.root), 1)

        _write(self.root, "a.py", "a = 1\n")
        snapshots.commit_step(self.root, 1, "one")
        _write(self.root, "b.py", "b = 1\n")
        snapshots.commit_step(self.root, 2, "two")

        self.assertEqual(snapshots.next_step(self.root), 3)

    def test_a_revert_restores_modified_added_and_deleted_files(self):
        snapshots.init_snapshots(self.root)
        _write(self.root, "pkg/mod.py", "value = 2\n")
        _write(self.root, "added.py", "added = True\n")
        snapshots.commit_step(self.root, 1, "one")

        # Step 2 does the damage: rewrites a file, adds another, deletes one.
        _write(self.root, "pkg/mod.py", "value = 'corrupted'\n")
        _write(self.root, "junk.py", "junk = True\n")
        os.remove(os.path.join(self.root, "added.py"))
        snapshots.commit_step(self.root, 2, "two")

        outcome = snapshots.revert_to_step(self.root, 1)

        self.assertEqual(outcome["step"], 1)
        self.assertEqual(_read(self.root, "pkg/mod.py"), "value = 2\n")
        self.assertEqual(_read(self.root, "added.py"), "added = True\n")
        self.assertFalse(os.path.exists(os.path.join(self.root, "junk.py")))

    def test_a_step_with_no_snapshot_resolves_to_the_state_as_of_that_step(self):
        """A step that changed nothing has no tree of its own, so it resolves to the newest
        snapshot at or before it -- which *is* the state as of that step."""
        snapshots.init_snapshots(self.root)
        _write(self.root, "pkg/mod.py", "value = 2\n")
        snapshots.commit_step(self.root, 1, "one")
        # Steps 2 and 3 changed nothing, so there is no snapshot at 2 or 3.
        _write(self.root, "pkg/mod.py", "value = 3\n")
        snapshots.commit_step(self.root, 4, "four")

        self.assertEqual(snapshots.latest_step_at_or_before(self.root, 3), 1)
        self.assertEqual(snapshots.revert_to_step(self.root, 3)["step"], 1)
        self.assertEqual(_read(self.root, "pkg/mod.py"), "value = 2\n")

    def test_a_revert_cleans_files_the_agent_never_committed(self):
        snapshots.init_snapshots(self.root)
        _write(self.root, "pkg/mod.py", "value = 2\n")
        snapshots.commit_step(self.root, 1, "one")

        # Written, then the run was interrupted before the next snapshot.
        _write(self.root, "half_written.py", "partial = True\n")

        snapshots.revert_to_step(self.root, 1)

        self.assertFalse(os.path.exists(os.path.join(self.root, "half_written.py")))

    def test_a_workspace_without_snapshots_refuses_rather_than_guessing(self):
        with self.assertRaises(snapshots.SnapshotError):
            snapshots.revert_to_step(self.root, 1)

    def test_a_step_before_the_first_snapshot_refuses(self):
        snapshots.init_snapshots(self.root)
        self.assertIsNone(snapshots.latest_step_at_or_before(self.root, -1))
        with self.assertRaises(snapshots.SnapshotError):
            snapshots.revert_to_step(self.root, -1)


class StepSpendTests(unittest.TestCase):
    """The per-step bill, and the truncation that makes a rewind cost what the run still owes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aleth_stepspend_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ledger = IntentLedger(os.path.join(self.tmp, "state.db"))
        self._record()

    def _record(self, intent_id="i1"):
        from api.intents import Intent

        intent = Intent(id=intent_id, action_type="custom", message="go",
                        action_params={}, enqueued_at=0.0)
        self.ledger.record(intent)
        return intent

    def test_a_step_accumulates_and_the_cumulative_follows(self):
        self.ledger.record_step_spend("i1", 1, prompt=100, completion=10)
        self.ledger.record_step_spend("i1", 1, prompt=50, completion=5)
        self.ledger.record_step_spend("i1", 2, prompt=200, completion=20)

        self.assertEqual(self.ledger.token_totals("i1"), (350, 35))

    def test_truncating_forgets_the_later_steps_and_recomputes_the_total(self):
        self.ledger.record_step_spend("i1", 1, prompt=100, completion=10)
        self.ledger.record_step_spend("i1", 2, prompt=200, completion=20)
        self.ledger.record_step_spend("i1", 3, prompt=300, completion=30)

        totals = self.ledger.truncate_to_step("i1", 1)

        self.assertEqual(totals, (100, 10))
        self.assertEqual(self.ledger.token_totals("i1"), (100, 10))
        # ...and the truncation is durable, not only a return value.
        self.assertEqual(self.ledger.truncate_to_step("i1", 9), (100, 10))

    def test_truncating_an_intent_with_no_step_rows_leaves_the_bill_alone(self):
        """The rows are the authority. Inventing a total from an empty ledger would under-count a
        real bill, so the cumulative is returned unchanged instead."""
        self.ledger.add_tokens("i1", prompt=7, completion=3)

        self.assertEqual(self.ledger.truncate_to_step("i1", 0), (7, 3))


class RollbackLedgerTests(unittest.TestCase):
    """The rewind target is a ledger fact, because the loop is another thread (and process)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aleth_rollback_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ledger = IntentLedger(os.path.join(self.tmp, "state.db"))

    def _intent(self, intent_id, *, claim=True):
        from api.intents import Intent

        intent = Intent(id=intent_id, action_type="custom", message="go",
                        action_params={}, enqueued_at=0.0)
        self.ledger.record(intent)
        if claim:
            self.assertTrue(self.ledger.claim(intent))
        return intent

    def test_a_rollback_is_queued_only_for_a_paused_intent(self):
        self._intent("i-run")

        self.assertFalse(self.ledger.request_rollback("i-run", 3))

        self.ledger.interrupt("i-run", "hold")
        self.assertTrue(self.ledger.request_rollback("i-run", 3))

    def test_the_target_is_drained_exactly_once(self):
        self._intent("i-drain")
        self.ledger.interrupt("i-drain", "hold")
        self.ledger.request_rollback("i-drain", 4)

        self.assertEqual(self.ledger.drain_rollback("i-drain"), 4)
        self.assertEqual(self.ledger.drain_rollback("i-drain"), 0)

    def test_an_intent_with_no_rollback_queued_drains_zero(self):
        self._intent("i-none")
        self.assertEqual(self.ledger.drain_rollback("i-none"), 0)


class SystemAbortTests(unittest.TestCase):
    """Phase 36: the engine's own shutdown is a terminal state, and it is its own status."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aleth_abort_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ledger = IntentLedger(os.path.join(self.tmp, "state.db"))

    def _intent(self, intent_id, *, claim=True):
        from api.intents import Intent

        intent = Intent(id=intent_id, action_type="custom", message="go",
                        action_params={}, enqueued_at=0.0)
        self.ledger.record(intent)
        if claim:
            self.assertTrue(self.ledger.claim(intent))
        return intent

    def test_a_running_intent_is_marked_aborted_by_the_system(self):
        self._intent("i-live")

        self.assertEqual(self.ledger.abort_running("the engine received SIGTERM"), 1)

        self.assertEqual(self.ledger.status_of("i-live"), ABORTED_BY_SYSTEM)
        pending = self.ledger.pending_failure()
        self.assertIsNotNone(pending)
        self.assertEqual(pending["intent_id"], "i-live")
        self.assertIn("SIGTERM", pending["error"])

    def test_a_settled_intent_is_not_rewritten(self):
        """The engine's own terminal write always wins: a shutdown must not relabel a finished run."""
        self._intent("i-done")
        intent = self._intent("i-done", claim=False)
        intent.status, intent.error = "completed", ""
        self.ledger.settle(intent)

        self.assertEqual(self.ledger.abort_running("the engine received SIGTERM"), 0)
        self.assertEqual(self.ledger.status_of("i-done"), "completed")

    def test_a_paused_intent_is_left_for_the_next_boot(self):
        """A hold is not a run in flight: it is waiting for a person, and a shutdown does not
        resolve it. The boot reconcile is what fails it, with its own reason."""
        self._intent("i-held")
        self.ledger.interrupt("i-held", "hold")

        self.assertEqual(self.ledger.abort_running("the engine received SIGTERM"), 0)
        self.assertEqual(self.ledger.status_of("i-held"), PAUSED)


if __name__ == "__main__":
    unittest.main()
