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
from api.intents import Intent, IntentQueue, run_worker
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
        self.ledger.mark_running(running)
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


class _StubService:
    """The smallest service the operation table can call.

    ``acknowledge_intent`` goes through a real ledger, because the point of the test is that the
    gate *clears* -- a stub that only returns a number would prove nothing about the gate.
    """

    def __init__(self, ledger=None, payload=None):
        self._ledger = ledger
        self._payload = payload

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

        status, body = self._request("GET", "/api/intents")
        self.assertEqual(status, 200)
        self.assertEqual(body["pending_failure"]["intent_id"], "boom")
        self.assertEqual(body["pending_failure"]["error"], "the run died")
        self.assertEqual([row["intent_id"] for row in body["ledger"]], ["boom"])

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

        _, after = self._request("GET", "/api/intents")
        self.assertIsNone(after["pending_failure"])

    def test_boot_reconciles_an_intent_left_running_by_a_dead_engine(self):
        """The intent was written, then the process died. The next boot is what fails it."""
        intent = _intent("orphan")
        self.ledger.record(intent)
        self.ledger.mark_running(intent)

        # A fresh gateway over the same ledger is the restart.
        second = start_gateway(port=0, ledger=self.ledger)
        self.addCleanup(second.stop)
        connection = http.client.HTTPConnection("127.0.0.1", second.bound_port, timeout=10)
        try:
            connection.request("GET", "/api/intents")
            body = json.loads(connection.getresponse().read().decode("utf-8"))
        finally:
            connection.close()

        self.assertEqual(body["pending_failure"]["intent_id"], "orphan")
        self.assertIn("stopped before", body["pending_failure"]["error"])


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
