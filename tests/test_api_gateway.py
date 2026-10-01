"""Phase 14: the API gateway -- routing, the intent queue, the stream, and the boundary.

Four things are proven here, and they are the four things the frontend depends on:

* the **contract** -- every route answers with the shape ``api.schemas`` declares, an unknown
  path is a 404, a known path with the wrong verb is a 405, and a malformed intent is a 400
  rather than a silently-ignored field;
* the **queue** -- an accepted intent waits for the orchestrator instead of executing, and the
  loop that drains it survives a handler that raises;
* the **stream** -- an event emitted on the bus reaches an SSE client, and a client that cannot
  keep up loses frames rather than stalling the emitter;
* the **boundary** -- the socket is loopback, a foreign ``Host`` is refused (DNS rebinding), and
  a cross-site ``Origin`` is refused on the one state-changing route.

The routing tests build a :class:`~api.gateway.Request` directly: the contract is asserted
without a socket, so a failure names the handler rather than a port.
"""

import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest

from api.events import EventHub, sse_frame
from api.gateway import Gateway, Request, StreamResponse
from api.intents import IntentQueue, IntentQueueFull, run_worker
from api.schemas import IntentRequest
from api.server import (
    ApiServer,
    host_allowed,
    is_loopback_address,
    loopback_origins,
    origin_allowed,
    start_gateway,
)
from storage import telemetry
from storage.db import PlanDAG, TaskNode, get_store, reset_stores
from tools import workspace

_LOG_EVENT = {"type": "log", "agent": "architect", "log_type": "thinking", "text": "hello"}


class GatewayTestCase(unittest.TestCase):
    """A real store on a throwaway plan directory, so the reads are the production reads."""

    def setUp(self):
        from storage.db import reset_stores as _reset

        self.tmp = tempfile.mkdtemp(prefix="aleth_api_")
        self._orig = (workspace.PLAN_DIR, workspace.PROJECT_DIR, workspace.ACTIVE_PLAN_FILE)
        workspace.PLAN_DIR = self.tmp
        workspace.PROJECT_DIR = self.tmp
        workspace.ACTIVE_PLAN_FILE = "PLAN.md"
        _reset()
        self.store = get_store()
        self.addCleanup(self._restore)

    def _restore(self):
        reset_stores()
        workspace.PLAN_DIR, workspace.PROJECT_DIR, workspace.ACTIVE_PLAN_FILE = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_plan(self, status="pending"):
        self.store.save_dag(
            PlanDAG(
                plan_id="PLAN", title="Api probe", plan_file="PLAN.md", order=["a"],
                nodes={"a": TaskNode(id="a", plan_id="PLAN", title="A task", status=status)},
            ),
            sync_edges=True,
        )

    def _seed_receipt(self, execution_id="exec-1", outcome="OOM_KILLED"):
        connection = self.store._connect()
        try:
            telemetry.apply_telemetry_schema(connection)
        finally:
            connection.close()
        telemetry.record(
            self.store.path, execution_id=execution_id, session_id="s1",
            target_tool="execute_command", exit_code=137, duration_ms=5,
            stdout_hash="aa", stderr_hash="bb", outcome=outcome,
        )


class RoutingTests(GatewayTestCase):
    """The contract, asserted without a socket."""

    def setUp(self):
        super().setUp()
        self.gateway = Gateway()

    def _get(self, path, query=None):
        return self.gateway.dispatch(Request(method="GET", path=path, query=query or {}))

    def _post(self, path, payload):
        body = json.dumps(payload).encode("utf-8")
        return self.gateway.dispatch(Request(method="POST", path=path, body=body))

    def test_health_reports_the_gateway_and_its_plan(self):
        response = self._get("/api/health")
        self.assertEqual(response.status, 200)
        body = json.loads(response.body)
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["loopback_only"])
        self.assertEqual(body["active_plan_id"], "PLAN")

    def test_the_active_plan_is_served_with_its_tasks_and_counters(self):
        self._seed_plan(status="completed")
        body = json.loads(self._get("/api/plan").body)
        self.assertEqual(body["plan_id"], "PLAN")
        self.assertEqual([task["id"] for task in body["tasks"]], ["a"])
        self.assertEqual(body["tasks"][0]["status"], "completed")
        self.assertEqual(body["metrics"]["completed_tasks"], 1)
        self.assertEqual(body["metrics"]["progress_percent"], 100)

    def test_a_plan_the_store_does_not_hold_is_a_404(self):
        self.assertEqual(self._get("/api/plan").status, 404)
        self.assertEqual(self._get("/api/plan/NOPE").status, 404)

    def test_the_ledger_is_served_newest_first_and_bounded(self):
        self._seed_receipt("exec-1")
        self._seed_receipt("exec-2", outcome="")
        body = json.loads(self._get("/api/telemetry", {"limit": ["1"]}).body)
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["receipts"][0]["execution_id"], "exec-2")

    def test_a_bad_limit_is_refused_rather_than_guessed(self):
        self.assertEqual(self._get("/api/telemetry", {"limit": ["many"]}).status, 400)

    def test_one_receipt_is_served_by_execution_id(self):
        self._seed_receipt("exec-9")
        body = json.loads(self._get("/api/telemetry/exec-9").body)
        self.assertEqual(body["outcome"], "OOM_KILLED")
        self.assertEqual(self._get("/api/telemetry/nope").status, 404)

    def test_an_unknown_path_is_a_404_and_a_wrong_verb_is_a_405(self):
        self.assertEqual(self._get("/api/nothing").status, 404)
        self.assertEqual(self._post("/api/plan", {}).status, 405)

    def test_the_event_route_streams(self):
        self.assertIsInstance(self._get("/api/events"), StreamResponse)

    def test_a_valid_intent_is_queued_and_not_executed(self):
        response = self._post("/api/intent/execute", {"action_type": "next_step", "message": "go"})
        self.assertEqual(response.status, 202)
        body = json.loads(response.body)
        self.assertEqual(body["status"], "queued")
        self.assertTrue(body["intent_id"])
        # Queued, and still queued: nothing has run.
        self.assertEqual(self.gateway.queue.depth, 1)
        self.assertEqual(self.gateway.queue.status().completed, 0)

    def test_an_undocumented_key_is_refused_at_the_door(self):
        response = self._post("/api/intent/execute", {"action_type": "custom", "surprise": 1})
        self.assertEqual(response.status, 400)
        self.assertIn("invalid intent", response.body.decode("utf-8"))

    def test_an_intent_the_engine_does_not_implement_is_refused(self):
        response = self._post("/api/intent/execute", {"action_type": "delete_everything"})
        self.assertEqual(response.status, 400)

    def test_a_full_queue_is_refused_rather_than_accumulated(self):
        gateway = Gateway(queue=IntentQueue(capacity=1))
        self.assertEqual(
            gateway.dispatch(
                Request(method="POST", path="/api/intent/execute",
                        body=b'{"action_type": "custom", "message": "one"}')
            ).status,
            202,
        )
        refused = gateway.dispatch(
            Request(method="POST", path="/api/intent/execute",
                    body=b'{"action_type": "custom", "message": "two"}')
        )
        self.assertEqual(refused.status, 429)


class IntentQueueTests(unittest.TestCase):
    def test_an_accepted_intent_is_drained_in_order(self):
        queue = IntentQueue(capacity=4)
        first = queue.submit(IntentRequest(message="one"))
        second = queue.submit(IntentRequest(message="two"))
        self.assertEqual(queue.depth, 2)
        self.assertEqual(queue.take(timeout=0.1).id, first.id)
        self.assertEqual(queue.take(timeout=0.1).id, second.id)
        self.assertIsNone(queue.take(timeout=0.05))

    def test_the_queue_is_bounded(self):
        queue = IntentQueue(capacity=1)
        queue.submit(IntentRequest(message="one"))
        with self.assertRaises(IntentQueueFull):
            queue.submit(IntentRequest(message="two"))

    def test_the_worker_survives_a_handler_that_raises(self):
        """A dead loop turns every later submission into a receipt for work that never ran."""
        queue = IntentQueue(capacity=8)
        seen = []
        stop = threading.Event()

        def handler(intent):
            seen.append(intent.message)
            if intent.message == "boom":
                raise RuntimeError("handler fault")

        worker = threading.Thread(target=run_worker, args=(queue, handler, stop), daemon=True)
        worker.start()
        try:
            queue.submit(IntentRequest(message="boom"))
            queue.submit(IntentRequest(message="ok"))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and queue.status().completed < 1:
                time.sleep(0.02)
            status = queue.status()
            self.assertEqual(status.failed, 1)
            self.assertEqual(status.completed, 1)
            self.assertEqual(seen, ["boom", "ok"])
        finally:
            stop.set()
            queue.wake()
            worker.join(timeout=5)


class EventHubTests(unittest.TestCase):
    def test_an_event_is_framed_as_one_sse_data_line(self):
        frame = sse_frame(_LOG_EVENT)
        self.assertTrue(frame.startswith("data: "))
        self.assertTrue(frame.endswith("\n\n"))
        self.assertEqual(json.loads(frame[len("data: "):-2]), _LOG_EVENT)

    def test_a_subscriber_receives_what_is_published(self):
        hub = EventHub()
        subscriber = hub.subscribe()
        hub.publish(_LOG_EVENT)
        frame = subscriber.next_frame(timeout=0.5)
        self.assertIsNotNone(frame)
        self.assertEqual(json.loads(frame[len("data: "):-2]), _LOG_EVENT)

    def test_a_slow_subscriber_drops_frames_instead_of_stalling_the_emitter(self):
        hub = EventHub(backlog=2)
        subscriber = hub.subscribe()
        for _ in range(10):
            hub.publish(_LOG_EVENT)
        self.assertGreater(subscriber.dropped, 0)
        # The newest survived, and the emitter never blocked.
        self.assertIsNotNone(subscriber.next_frame(timeout=0.5))

    def test_unsubscribing_stops_delivery(self):
        hub = EventHub()
        subscriber = hub.subscribe()
        hub.unsubscribe(subscriber)
        hub.publish(_LOG_EVENT)
        self.assertEqual(hub.subscriber_count, 0)
        self.assertIsNone(subscriber.next_frame(timeout=0.1))

    def test_publishing_never_raises_on_an_unserialisable_event(self):
        hub = EventHub()
        subscriber = hub.subscribe()
        hub.publish({"type": "log", "value": object()})  # not JSON, and must not break the emit
        self.assertEqual(hub.subscriber_count, 1)


class BoundaryTests(unittest.TestCase):
    """The checks themselves, without a socket in the way."""

    def test_only_loopback_addresses_pass(self):
        for address in ("127.0.0.1", "127.5.5.5", "::1", "::ffff:127.0.0.1", "localhost"):
            self.assertTrue(is_loopback_address(address), address)
        for address in ("0.0.0.0", "10.0.0.7", "192.168.1.20", "::ffff:10.0.0.7", "", "evil.com"):
            self.assertFalse(is_loopback_address(address), address)

    def test_only_loopback_hosts_are_answered(self):
        for host in ("127.0.0.1:8765", "localhost:8765", "127.0.0.1", "[::1]:8765"):
            self.assertTrue(host_allowed(host), host)
        for host in ("evil.com:8765", "evil.com", "aleth.example", "", "127.0.0.1.evil.com:80"):
            self.assertFalse(host_allowed(host), host)

    def test_a_missing_origin_is_allowed_and_a_foreign_one_is_not(self):
        self.assertTrue(origin_allowed("", 8765))
        self.assertTrue(origin_allowed("http://127.0.0.1:8765", 8765))
        self.assertTrue(origin_allowed("https://aleth.local", 8765, ["https://aleth.local"]))
        self.assertFalse(origin_allowed("https://evil.example", 8765))
        self.assertFalse(origin_allowed("null", 8765))

    def test_the_loopback_origins_are_this_server_s_own(self):
        self.assertIn("http://127.0.0.1:8765", loopback_origins(8765))


class LiveServerTests(GatewayTestCase):
    """The same routes, over a real socket, with the boundary in front of them."""

    def setUp(self):
        super().setUp()
        self.seen_intents = []
        self.intent_seen = threading.Event()

        def on_intent(intent):
            self.seen_intents.append(intent)
            self.intent_seen.set()

        self.server = start_gateway(port=0, on_intent=on_intent)
        self.addCleanup(self.server.stop)

    def _request(self, method, path, *, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.bound_port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            connection.close()

    def test_the_socket_is_loopback_only(self):
        self.assertEqual(self.server.host, "127.0.0.1")
        self.assertEqual(self.server._httpd.server_address[0], "127.0.0.1")
        self.assertNotEqual(self.server.bound_port, 0)

    def test_state_is_served_over_http(self):
        self._seed_plan(status="in_progress")
        status, body = self._request("GET", "/api/plan")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["tasks"][0]["status"], "in_progress")

    def test_a_foreign_host_header_is_refused(self):
        """The DNS-rebinding guard: a name that resolves to loopback is still not our name."""
        status, body = self._request("GET", "/api/health", headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("unrecognised host", body)

    def test_a_cross_site_post_is_refused(self):
        status, body = self._request(
            "POST", "/api/intent/execute",
            body=json.dumps({"action_type": "custom", "message": "go"}),
            headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertIn("cross-site", body)
        self.assertEqual(self.server.gateway.queue.depth, 0)

    def test_an_accepted_intent_is_drained_by_the_orchestrator_loop(self):
        status, body = self._request(
            "POST", "/api/intent/execute",
            body=json.dumps({"action_type": "next_step", "message": "proceed"}),
            headers={"Content-Type": "application/json", "Origin": "http://127.0.0.1:%d" % self.server.bound_port},
        )
        self.assertEqual(status, 202)
        self.assertTrue(self.intent_seen.wait(timeout=5), "the intent worker never drained the queue")
        self.assertEqual([intent.action_type for intent in self.seen_intents], ["next_step"])
        self.assertEqual(self.server.gateway.queue.status().completed, 1)

    def test_a_preflight_is_answered_for_an_allowed_origin(self):
        status, _ = self._request(
            "OPTIONS", "/api/intent/execute",
            headers={"Origin": "http://127.0.0.1:%d" % self.server.bound_port},
        )
        self.assertEqual(status, 204)

    def test_an_event_on_the_bus_reaches_a_streaming_client(self):
        """The push the desktop window gets and the push a browser gets are the same event."""
        received = []
        ready = threading.Event()

        def reader():
            connection = http.client.HTTPConnection("127.0.0.1", self.server.bound_port, timeout=15)
            try:
                connection.request("GET", "/api/events")
                response = connection.getresponse()
                ready.set()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    line = response.readline()
                    if not line:
                        break
                    if line.startswith(b"data: "):
                        received.append(json.loads(line[len(b"data: "):]))
                        return
            except Exception:
                pass
            finally:
                connection.close()

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(timeout=5), "the stream never opened")
        # Wait for the subscriber to be registered before emitting, or the event is published to
        # nobody and the test fails for a reason that is not the code's.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.server.gateway.hub.subscriber_count == 0:
            time.sleep(0.02)
        import bridge_bus

        bridge_bus.emit(_LOG_EVENT)
        thread.join(timeout=10)
        self.assertEqual(received, [_LOG_EVENT])

    def test_the_bus_listener_is_detached_when_the_server_stops(self):
        self.server.stop()
        import bridge_bus

        bridge_bus.emit(_LOG_EVENT)  # must not raise, and must not reach a closed hub
        self.assertEqual(self.server.gateway.hub.subscriber_count, 0)


if __name__ == "__main__":
    unittest.main()
