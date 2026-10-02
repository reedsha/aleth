"""Phase 17: the intent ledger, and the idempotency it exists to guarantee.

The question these tests answer is not "does the queue work" -- it is **what happens when the
engine dies mid-run**, and what the user is told afterwards. Three properties:

* an intent is written **before** it can run, so a process that dies mid-run leaves a record
  rather than nothing;
* the next boot reconciles every unfinished intent to ``failed`` -- deterministically, and
  **without retrying it**; and
* a failure is terminal and *gates* new work until the user acknowledges it, in the ledger rather
  than in the UI, so a reload cannot forget it.
"""

import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest

from api.gateway import Gateway, Request
from api.intents import Intent, IntentQueue, RunDeferred, run_worker
from api.schemas import IntentRequest
from api.server import start_gateway
from storage.intents import IntentLedger


def _intent(intent_id="i1", **kwargs):
    return Intent(
        id=intent_id,
        action_type=kwargs.get("action_type", "custom"),
        message=kwargs.get("message", "do the thing"),
        action_params={},
        enqueued_at=time.time(),
    )


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aleth_intents_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ledger = IntentLedger(os.path.join(self.tmp, "state.db"))

    def _row(self, intent_id):
        return next(
            (row for row in self.ledger.recent(50) if row["intent_id"] == intent_id), None
        )

    def test_an_accepted_intent_is_written_before_it_can_run(self):
        intent = _intent()
        self.ledger.record(intent)
        row = self._row("i1")
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["message"], "do the thing")
        self.assertEqual(row["acknowledged_at"], 0)

    def test_a_settled_intent_carries_its_terminal_state_and_reason(self):
        intent = _intent()
        self.ledger.record(intent)
        intent.status = "failed"
        intent.error = "the container was OOM-killed"
        self.ledger.settle(intent)

        row = self._row("i1")
        self.assertEqual(row["status"], "failed")
        self.assertIn("OOM-killed", row["error"])

    def test_reconcile_fails_every_unfinished_intent_and_does_not_retry_them(self):
        """The engine died mid-run: nothing wrote a terminal state, so boot writes one."""
        queued, running, done = _intent("q"), _intent("r"), _intent("d")
        for intent in (queued, running, done):
            self.ledger.record(intent)
        self.assertTrue(self.ledger.claim(running))
        done.status = "completed"
        self.ledger.settle(done)

        self.assertEqual(self.ledger.reconcile("the engine stopped before this run finished"), 2)
        self.assertEqual(self._row("q")["status"], "failed")
        self.assertEqual(self._row("r")["status"], "failed")
        self.assertEqual(self._row("d")["status"], "completed")
        self.assertIn("stopped", self._row("q")["error"])
        # Idempotent: a second boot finds nothing left to reconcile.
        self.assertEqual(self.ledger.reconcile("again"), 0)

    def test_only_an_unacknowledged_failure_gates(self):
        done, stopped, failed = _intent("d"), _intent("s"), _intent("f")
        for intent in (done, stopped, failed):
            self.ledger.record(intent)
        done.status, stopped.status, failed.status = "completed", "stopped", "failed"
        failed.error = "it broke"
        for intent in (done, stopped, failed):
            self.ledger.settle(intent)

        pending = self.ledger.pending_failure()
        self.assertIsNotNone(pending)
        self.assertEqual(pending["intent_id"], "f")

        self.assertEqual(self.ledger.acknowledge("f"), 1)
        self.assertIsNone(self.ledger.pending_failure())
        # Acknowledging twice is not an error, and does not resurrect the gate.
        self.assertEqual(self.ledger.acknowledge("f"), 0)

    def test_acknowledging_everything_clears_the_gate_without_an_id(self):
        for name in ("a", "b"):
            intent = _intent(name)
            self.ledger.record(intent)
            intent.status, intent.error = "failed", "broke"
            self.ledger.settle(intent)
        self.assertEqual(self.ledger.acknowledge(), 2)
        self.assertIsNone(self.ledger.pending_failure())


class QueueIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aleth_queue_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ledger = IntentLedger(os.path.join(self.tmp, "state.db"))

    def test_the_queue_writes_every_transition_through_to_the_ledger(self):
        queue = IntentQueue(capacity=4, ledger=self.ledger)
        intent = queue.submit(IntentRequest(action_type="custom", message="go"))

        self.assertEqual(self.ledger.recent(1)[0]["status"], "queued")
        taken = queue.take(timeout=0.1)
        self.assertEqual(taken.id, intent.id)
        self.assertEqual(self.ledger.recent(1)[0]["status"], "running")

        queue.settle(taken)
        self.assertEqual(self.ledger.recent(1)[0]["status"], "completed")

    def test_a_failed_intent_is_never_re_queued(self):
        """The engine does not retry on the user's behalf: one run, one answer."""
        queue = IntentQueue(capacity=4, ledger=self.ledger)
        stop = threading.Event()

        def handler(intent):
            raise RuntimeError("the container was OOM-killed")

        queue.submit(IntentRequest(action_type="custom", message="go"))
        worker = threading.Thread(target=run_worker, args=(queue, handler, stop), daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and queue.status().failed < 1:
                time.sleep(0.02)
        finally:
            stop.set()
            queue.wake()
            worker.join(timeout=5)

        status = queue.status()
        self.assertEqual(status.failed, 1)
        self.assertEqual(status.depth, 0)
        # Terminal: nothing to take, and the ledger says why.
        self.assertIsNone(queue.take(timeout=0.05))
        row = self.ledger.recent(1)[0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("OOM-killed", row["error"])
        self.assertEqual(self.ledger.reconcile("boot"), 0, "a settled intent is not reconciled")

    def test_a_queue_without_a_ledger_still_works(self):
        queue = IntentQueue(capacity=2)
        queue.submit(IntentRequest(action_type="custom", message="go"))
        taken = queue.take(timeout=0.1)
        queue.settle(taken)
        self.assertEqual(queue.status().completed, 1)

    def test_a_stopped_run_is_terminal_but_not_a_failure(self):
        queue = IntentQueue(capacity=2, ledger=self.ledger)
        queue.submit(IntentRequest(action_type="custom", message="go"))
        taken = queue.take(timeout=0.1)
        taken.status = "stopped"  # the service sets this from the run's own terminal event
        queue.settle(taken)

        self.assertEqual(self.ledger.recent(1)[0]["status"], "stopped")
        self.assertIsNone(self.ledger.pending_failure())


class ProjectExecutionMutexTests(unittest.TestCase):
    """Phase 25: one intent runs per project, and a second is a wait -- never a second run.

    The mutex is the **ledger's**, not the queue's: the queue is in memory and dies with the
    process, while the ledger is shared by every engine on the project. The database is already
    per project -- its path is ``<state_dir>/<project_id>/aleth_state.db`` -- so "nothing else is
    running in this ledger" is the whole of "nothing else is running in this project", with no
    project column and no second lock file to keep in step.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aleth_mutex_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_path = os.path.join(self.tmp, "state.db")
        self.ledger = IntentLedger(self.db_path)

    def _statuses(self):
        return {row["intent_id"]: row["status"] for row in self.ledger.recent(20)}

    def test_a_second_intent_cannot_claim_a_project_that_is_executing(self):
        first, second = _intent("first"), _intent("second")
        for intent in (first, second):
            self.ledger.record(intent)

        self.assertTrue(self.ledger.claim(first))
        self.assertFalse(self.ledger.claim(second))

        self.assertEqual(self._statuses(), {"first": "running", "second": "queued"})

    def test_a_second_engine_over_the_same_project_cannot_claim_either(self):
        """Two processes, one project: the ledger is the only thing they share."""
        other = IntentLedger(self.db_path)
        first, second = _intent("first"), _intent("second")
        for intent in (first, second):
            other.record(intent)

        self.assertTrue(other.claim(first))
        # A second handle is a second engine as far as SQLite is concerned.
        self.assertFalse(self.ledger.claim(second))

    def test_the_slot_frees_when_the_holder_settles(self):
        first, second = _intent("first"), _intent("second")
        for intent in (first, second):
            self.ledger.record(intent)
        self.assertTrue(self.ledger.claim(first))
        self.assertFalse(self.ledger.claim(second))

        first.status = "completed"
        self.ledger.settle(first)

        self.assertTrue(self.ledger.claim(second))

    def test_a_released_slot_is_not_an_outcome(self):
        intent = _intent("i1")
        self.ledger.record(intent)
        self.assertTrue(self.ledger.claim(intent))

        self.ledger.release(intent)

        self.assertEqual(self.ledger.recent(1)[0]["status"], "queued")
        self.assertIsNone(self.ledger.pending_failure())
        # ...and the slot is free again, so the same intent can take it.
        self.assertTrue(self.ledger.claim(intent))

    def test_a_claim_cannot_be_taken_twice(self):
        intent = _intent("i1")
        self.ledger.record(intent)
        self.assertTrue(self.ledger.claim(intent))
        self.assertFalse(self.ledger.claim(intent), "a repeated claim must not re-take the slot")

    def test_the_queue_leaves_a_blocked_intent_queued_at_the_head(self):
        queue = IntentQueue(capacity=4, ledger=self.ledger)
        first = queue.submit(IntentRequest(action_type="custom", message="first"))
        queue.submit(IntentRequest(action_type="custom", message="second"))

        # Another engine holds the project's slot for the head of this queue.
        self.assertTrue(self.ledger.claim(first))

        self.assertIsNone(queue.take(timeout=0.05))
        self.assertEqual(queue.depth, 2, "nothing may be popped while the slot is held")
        self.assertEqual(self._statuses()[first.id], "running")

        # Handing the slot back lets the head through, in order.
        self.ledger.release(first)
        taken = queue.take(timeout=0.05)
        self.assertEqual(taken.id, first.id)
        self.assertEqual(queue.depth, 1)

    def test_a_deferred_intent_is_re_queued_rather_than_failed(self):
        """A busy engine is a reason to wait, not an outcome to record."""
        queue = IntentQueue(capacity=4, ledger=self.ledger)
        stop = threading.Event()
        attempts = {"count": 0}

        def handler(intent):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RunDeferred("a run is already in progress")
            intent.status = "completed"

        queue.submit(IntentRequest(action_type="custom", message="go"))
        worker = threading.Thread(target=run_worker, args=(queue, handler, stop), daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and queue.status().completed < 1:
                time.sleep(0.02)
        finally:
            stop.set()
            queue.wake()
            worker.join(timeout=5)

        status = queue.status()
        self.assertEqual(status.completed, 1)
        self.assertEqual(status.failed, 0, "a deferral must not be recorded as a failure")
        self.assertEqual(attempts["count"], 2, "the intent must be retried, not dropped")
        self.assertIsNone(self.ledger.pending_failure())


class _StubService:
    """The smallest service the intent operations can call.

    The queue mechanics live in ``api.intents`` (one implementation for the service and for a
    stub); this holds the ledger the test supplies and the queue the gateway injects, so the queue
    state the test asserts on and the ledger the gate is read from are the same two objects the
    production service uses.
    """

    def __init__(self, ledger=None, payload=None):
        self._ledger = ledger
        self._payload = payload
        self._queue = None

    def attach_intent_queue(self, queue):
        self._queue = queue

    def get_intent_status(self):
        from api.intents import intent_status

        return intent_status(self._queue, self._ledger).model_dump()

    def submit_intent(self, action_type="custom", message="", action_params=None):
        from api.intents import submit_intent

        return submit_intent(self._queue, action_type=action_type, message=message,
                             action_params=action_params)

    def acknowledge_intent(self, intent_id=None):
        if self._payload is not None:
            return self._payload
        return {"success": True, "acknowledged": int(self._ledger.acknowledge(intent_id))}


class GatewayLedgerTests(unittest.TestCase):
    """The ledger over HTTP, which is how the UI reads the gate."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aleth_gw_ledger_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ledger = IntentLedger(os.path.join(self.tmp, "state.db"))
        self.server = start_gateway(
            port=0, ledger=self.ledger, service=_StubService(ledger=self.ledger)
        )
        self.addCleanup(self.server.stop)

    def _request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.bound_port, timeout=10)
        try:
            headers = {"Content-Type": "application/json"} if body is not None else {}
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

    def test_a_failed_intent_is_reported_as_the_gate(self):
        intent = _intent("boom")
        self.ledger.record(intent)
        intent.status, intent.error = "failed", "the run died"
        self.ledger.settle(intent)

        status, body = self._request("GET", "/api/intent/status")
        self.assertEqual(status, 200)
        data = body["data"]
        self.assertEqual(data["pending_failure"]["intent_id"], "boom")
        self.assertEqual(data["pending_failure"]["error"], "the run died")
        self.assertEqual([row["intent_id"] for row in data["ledger"]], ["boom"])

    def test_acknowledging_over_http_clears_the_gate(self):
        intent = _intent("boom")
        self.ledger.record(intent)
        intent.status, intent.error = "failed", "the run died"
        self.ledger.settle(intent)

        status, body = self._request(
            "POST", "/api/intent/acknowledge", json.dumps({"intent_id": "boom"})
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["acknowledged"], 1)

        _, after = self._request("GET", "/api/intent/status")
        self.assertIsNone(after["data"]["pending_failure"])

    def test_boot_reconciles_an_intent_left_running_by_a_dead_engine(self):
        """The intent was written, then the process died. The next boot is what fails it."""
        intent = _intent("orphan")
        self.ledger.record(intent)
        self.assertTrue(self.ledger.claim(intent))

        # A fresh gateway over the same ledger is the restart.
        second = start_gateway(port=0, ledger=self.ledger, service=_StubService(ledger=self.ledger))
        self.addCleanup(second.stop)
        connection = http.client.HTTPConnection("127.0.0.1", second.bound_port, timeout=10)
        try:
            connection.request("GET", "/api/intent/status")
            body = json.loads(connection.getresponse().read().decode("utf-8"))
        finally:
            connection.close()

        self.assertEqual(body["data"]["pending_failure"]["intent_id"], "orphan")
        self.assertIn("stopped before", body["data"]["pending_failure"]["error"])


class ContractGuardTests(unittest.TestCase):
    """A service answer that breaks the envelope is refused, not forwarded."""

    def test_a_bare_error_without_success_is_a_server_fault(self):
        class Broken:
            def acknowledge_intent(self, intent_id=None):
                return {"error": "I failed but did not say so"}

        gateway = Gateway(service=Broken())
        response = gateway.dispatch(
            Request(method="POST", path="/api/intent/acknowledge", body=b"{}")
        )
        self.assertEqual(response.status, 500)
        self.assertIn("response contract", response.body.decode("utf-8"))

    def test_a_proper_refusal_is_delivered_as_data(self):
        class Refusing:
            def acknowledge_intent(self, intent_id=None):
                return {"success": False, "error": "no ledger is attached"}

        gateway = Gateway(service=Refusing())
        response = gateway.dispatch(
            Request(method="POST", path="/api/intent/acknowledge", body=b"{}")
        )
        self.assertEqual(response.status, 200)
        body = json.loads(response.body.decode("utf-8"))
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"]["error"], "no ledger is attached")


if __name__ == "__main__":
    unittest.main()
