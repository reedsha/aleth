"""Phase 27: the run's correlation id, and the crash report that carries it.

The property under test is not "does a global hold a string" -- it is that an unhandled exception
arrives with the identity of the run it happened in. A naked traceback in a busy engine is a stack
nobody can join to the work that caused it, and this module is the fix.
"""

import contextlib
import io
import sys
import unittest

from tools.run_context import get_current_intent, install_exception_logging, set_current_intent


class RunContextTests(unittest.TestCase):
    def tearDown(self):
        set_current_intent(None)

    def test_the_current_intent_round_trips_and_clears(self):
        self.assertEqual(set_current_intent("i-1"), "i-1")
        self.assertEqual(get_current_intent(), "i-1")
        set_current_intent(None)
        self.assertEqual(get_current_intent(), "")
        # A blank or missing id is "idle", never the string "None".
        self.assertEqual(set_current_intent(None), "")
        self.assertEqual(get_current_intent(), "")

    def test_two_threads_hold_different_intents(self):
        """The bug this exists to prevent: thread B's id filing thread A's crash.

        With a module global the last writer wins, so a crash in A is reported under B's intent --
        logs that are worse than absent, because they are confidently wrong. The barriers make the
        interleaving deterministic: both threads have set their value before either reads.
        """
        import threading

        barrier = threading.Barrier(2)
        seen = {}

        def worker(intent_id):
            set_current_intent(intent_id)
            barrier.wait()  # both have set...
            barrier.wait()  # ...and neither sets again before the read
            seen[intent_id] = get_current_intent()

        threads = [threading.Thread(target=worker, args=(name,)) for name in ("i-a", "i-b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(seen, {"i-a": "i-a", "i-b": "i-b"})

    def test_a_new_thread_does_not_inherit_the_intent(self):
        """A fresh thread is idle, not whatever its parent happened to be running."""
        import threading

        set_current_intent("i-parent")
        seen = {}

        def worker():
            seen["value"] = get_current_intent()

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=10)

        self.assertEqual(seen["value"], "")
        self.assertEqual(get_current_intent(), "i-parent", "the parent's context is untouched")

    def test_an_unhandled_exception_is_reported_with_its_intent(self):
        """The correlation id rides the *same line* as the failure, not a line above it."""
        original = sys.excepthook
        self.addCleanup(setattr, sys, "excepthook", original)
        set_current_intent("i-crash")
        install_exception_logging()

        try:
            raise RuntimeError("boom")
        except RuntimeError:
            exc_type, exc, tb = sys.exc_info()

        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            sys.excepthook(exc_type, exc, tb)

        logged = captured.getvalue()
        self.assertIn("intent=i-crash", logged)
        self.assertIn("RuntimeError", logged)
        self.assertIn("boom", logged)
        # ...and the stack is still there: this adds context, it does not replace the traceback.
        self.assertIn("test_an_unhandled_exception_is_reported_with_its_intent", logged)

    def test_an_idle_process_says_so_rather_than_printing_a_blank(self):
        original = sys.excepthook
        self.addCleanup(setattr, sys, "excepthook", original)
        set_current_intent(None)
        install_exception_logging()

        try:
            raise ValueError("no run")
        except ValueError:
            exc_type, exc, tb = sys.exc_info()

        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            sys.excepthook(exc_type, exc, tb)

        self.assertIn("intent=<none>", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
