"""Phase 42: the shutdown flag and the bounded drain it drives.

The daemon's death sequence is a contract with the loops it owns: one event says "stop starting
work", every loop reads it at a boundary of its own choosing, and the drain runs each component's
own teardown without one failure taking the others down. These pin the mechanism without a socket,
a thread or a signal in the way.
"""

import threading
import unittest

from tools import lifecycle


class ShutdownFlagTests(unittest.TestCase):
    """One process-wide event, flipped once."""

    def setUp(self):
        lifecycle.reset()
        self.addCleanup(lifecycle.reset)

    def test_the_flag_starts_clear(self):
        self.assertFalse(lifecycle.is_shutting_down())
        self.assertFalse(lifecycle.shutdown_event().is_set())

    def test_begin_shutdown_flips_it_exactly_once(self):
        self.assertTrue(lifecycle.begin_shutdown())
        self.assertTrue(lifecycle.is_shutting_down())
        self.assertFalse(
            lifecycle.begin_shutdown(), "a second signal must not claim to own the shutdown"
        )

    def test_the_event_is_the_same_object_every_loop_reads(self):
        event = lifecycle.shutdown_event()
        lifecycle.begin_shutdown()
        # The flag is the event: a loop that waits on the object sees what a loop that polls sees.
        self.assertTrue(event.is_set())


class DrainTests(unittest.TestCase):
    """The drain: bounded, ordered, and never fatal."""

    def setUp(self):
        lifecycle.reset()
        self.addCleanup(lifecycle.reset)

    def test_drains_run_newest_first(self):
        order = []
        lifecycle.register_drain(lambda: order.append("first"))
        lifecycle.register_drain(lambda: order.append("second"))

        lifecycle.drain()

        # The last component started is the first stopped.
        self.assertEqual(order, ["second", "first"])

    def test_a_failing_drain_does_not_stop_the_others(self):
        ran = []

        def boom():
            raise RuntimeError("this component could not tidy")

        lifecycle.register_drain(lambda: ran.append("kept"))
        lifecycle.register_drain(boom)

        lifecycle.drain()  # must not raise

        self.assertEqual(ran, ["kept"])

    def test_registering_the_same_drain_twice_runs_it_once(self):
        calls = []

        def stop():
            calls.append(1)

        lifecycle.register_drain(stop)
        lifecycle.register_drain(stop)

        lifecycle.drain()

        self.assertEqual(len(calls), 1)

    def test_unregister_is_safe_when_never_registered(self):
        lifecycle.unregister_drain(lambda: None)  # must not raise

    def test_a_drain_can_unregister_itself_mid_drain(self):
        """A stopped server forgets its own registration; the snapshot keeps the loop sound."""
        order = []

        def first():
            order.append("first")
            lifecycle.unregister_drain(first)

        lifecycle.register_drain(first)
        lifecycle.register_drain(lambda: order.append("second"))

        lifecycle.drain()
        lifecycle.drain()  # the second pass must not re-run the unregistered one

        self.assertEqual(order, ["second", "first", "second"])

    def test_an_empty_drain_is_immediate(self):
        self.assertTrue(lifecycle.drain(timeout=5))

    def test_a_drain_within_its_budget_reports_success(self):
        ran = []
        lifecycle.register_drain(lambda: ran.append(1))

        self.assertTrue(lifecycle.drain(timeout=5))
        self.assertEqual(ran, [1])

    def test_a_drain_that_outlives_its_budget_reports_a_timeout(self):
        """Phase 43: a loop blocked on a long call cannot hold the caller forever."""
        release = threading.Event()
        self.addCleanup(release.set)
        lifecycle.register_drain(lambda: release.wait(5))

        self.assertFalse(lifecycle.drain(timeout=0.1))

    def test_reset_clears_both_the_flag_and_the_registry(self):
        calls = []
        lifecycle.register_drain(lambda: calls.append(1))
        lifecycle.begin_shutdown()

        lifecycle.reset()

        self.assertFalse(lifecycle.is_shutting_down())
        lifecycle.drain()
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
