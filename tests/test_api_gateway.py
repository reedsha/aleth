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
import pathlib
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest

from api import gateway as gateway_module
from api import operations
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


class _StubIntentService:
    """The intent operations, wired exactly as production wires them.

    The queue mechanics live in ``api.intents`` (one implementation for the service and for a
    stub), so this only has to hold the queue the gateway injects and the ledger the test supplies.
    """

    def __init__(self, ledger=None):
        self._ledger = ledger
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


class GatewayTestCase(unittest.TestCase):
    """A real store on a throwaway plan directory, so the reads are the production reads."""

    def setUp(self):
        from storage.db import reset_stores as _reset

        self.tmp = tempfile.mkdtemp(prefix="aleth_api_")
        self._orig = (workspace.PLAN_DIR, workspace.PROJECT_DIR, workspace.ACTIVE_PLAN_FILE)
        workspace.PLAN_DIR = self.tmp
        workspace.PROJECT_DIR = self.tmp
        workspace.ACTIVE_PLAN_FILE = "PLAN.md"
        workspace.publish_state_root()
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
        self.service = _StubIntentService()
        self.gateway = Gateway(service=self.service)
        # The service answers for the intent operations, and the queue belongs to the gateway; the
        # server joins them the same way (``api.server.start_gateway``).
        self.service.attach_intent_queue(self.gateway.queue)

    def _get(self, path, query=None):
        return self.gateway.dispatch(Request(method="GET", path=path, query=query or {}))

    def _post(self, path, payload):
        body = json.dumps(payload).encode("utf-8")
        return self.gateway.dispatch(Request(method="POST", path=path, body=body))

    def _data(self, response):
        body = json.loads(response.body)
        self.assertTrue(body["ok"], body)
        return body["data"]

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

    def test_the_per_intent_stream_route_names_its_intent(self):
        """Phase 33: the same stream, narrowed to one run -- and the firehose is not narrowed."""
        answer = self._get("/api/intents/i-1/stream")
        self.assertIsInstance(answer, StreamResponse)
        self.assertEqual(answer.intent_id, "i-1")
        self.assertEqual(self._get("/api/events").intent_id, "")
        # The intent *operation* namespace is untouched: a per-intent stream is a route, not a
        # privileged operation beside the table, and it lives under the plural name.
        self.assertEqual(self._get("/api/intent/status").status, 200)

    def test_a_valid_intent_is_queued_and_not_executed(self):
        response = self._post("/api/intent/execute", {"action_type": "next_step", "message": "go"})
        self.assertEqual(response.status, 200)
        data = self._data(response)
        self.assertEqual(data["status"], "queued")
        self.assertTrue(data["intent_id"])
        # Queued, and still queued: nothing has run.
        self.assertEqual(self.gateway.queue.depth, 1)
        self.assertEqual(self.gateway.queue.status().completed, 0)

    def test_the_queue_status_is_served_as_an_operation(self):
        """The intent surface has no side-band route: its state is an operation like any other."""
        data = self._data(self._get("/api/intent/status"))
        self.assertEqual(data["depth"], 0)
        self.assertEqual(data["capacity"], 32)
        # The old gateway route is gone, not merely deprecated.
        self.assertEqual(self._get("/api/intents").status, 404)

    def test_an_undocumented_key_is_refused_at_the_door(self):
        response = self._post("/api/intent/execute", {"action_type": "custom", "surprise": 1})
        self.assertEqual(response.status, 400)
        self.assertIn("invalid request", response.body.decode("utf-8"))

    def test_an_intent_the_engine_does_not_implement_is_refused(self):
        response = self._post("/api/intent/execute", {"action_type": "delete_everything"})
        self.assertEqual(response.status, 400)

    def test_the_plan_wide_intent_needs_no_prompt(self):
        """``execute_plan`` resolves the DAG from the store, so it carries no directive."""
        response = self._post("/api/intent/execute", {"action_type": "execute_plan"})
        self.assertEqual(response.status, 200)
        self.assertTrue(self._data(response)["intent_id"])

    def test_every_other_intent_still_needs_a_prompt(self):
        self.assertEqual(self._post("/api/intent/execute", {"action_type": "custom"}).status, 400)

    def test_a_full_queue_is_refused_rather_than_accumulated(self):
        service = _StubIntentService()
        gateway = Gateway(queue=IntentQueue(capacity=1), service=service)
        service.attach_intent_queue(gateway.queue)
        self.assertEqual(
            gateway.dispatch(
                Request(method="POST", path="/api/intent/execute",
                        body=b'{"action_type": "custom", "message": "one"}')
            ).status,
            200,
        )
        refused = gateway.dispatch(
            Request(method="POST", path="/api/intent/execute",
                    body=b'{"action_type": "custom", "message": "two"}')
        )
        # A refusal is *data*, not an HTTP fault: the envelope is ok, the operation said no.
        self.assertEqual(refused.status, 200)
        data = json.loads(refused.body)["data"]
        self.assertIs(data["success"], False)
        self.assertIn("full", data["error"])


class OperationTableTests(unittest.TestCase):
    """The client's table and the server's table are the same table.

    The intent surface used to be a side-band: two routes on the gateway that the frontend reached
    through an exception in its client table, which is how a second paradigm starts. This pins the
    repair -- the two tables map 1:1, and the gateway keeps no intent route at all.
    """

    @staticmethod
    def _client_paths():
        source = pathlib.Path(__file__).resolve().parents[1] / "ui" / "js" / "api-client.js"
        return set(re.findall(r'path:\s*"([^"]+)"', source.read_text(encoding="utf-8")))

    def test_the_frontend_and_backend_operation_tables_agree(self):
        server_paths = {operation.path for operation in operations.OPERATIONS}
        self.assertEqual(self._client_paths(), server_paths)

    def test_the_gateway_keeps_no_intent_route(self):
        """The two intent routes are gone from the gateway, not merely shadowed by the table.

        Scoped to the intent surface on purpose. (An earlier note here excused the collision
        between ``/api/plan/document`` and the ``/api/plan/{id}`` read as a plan id; that
        collision was a bug -- it 404'd the UI's first boot read -- and the table now outranks
        the pattern. See ``TableRoutePrecedenceTests``.)
        """
        intent_paths = [op.path for op in operations.OPERATIONS if "/intent/" in op.path]
        self.assertTrue(intent_paths, "the intent operations must exist somewhere")
        for path in intent_paths:
            for _method, pattern, _handler in gateway_module.ROUTES:
                self.assertIsNone(
                    re.compile(pattern).match(path),
                    f"{path} is served by a gateway route as well as the operation table",
                )


class TableRoutePrecedenceTests(GatewayTestCase):
    """The typed table outranks the infrastructure patterns that could swallow it.

    ``/api/plan/{plan_id}``'s regex used to capture the table's own GET paths -- ``document``,
    ``files``, ``steps``, ``structure`` -- as plan *ids*, so the UI's very first boot read
    (``get_active_plan`` on ``/api/plan/document``) died with 404 "unknown plan" and both the
    desktop window and a plain browser rendered *the engine is unreachable* over a perfectly
    healthy server. The closed table is the surface; a real plan id is whatever it does not claim.
    """

    class _Reads:
        """The service module-surface the shadowed reads need, and nothing more."""

        def get_active_plan(self):
            return {"plan_id": "PLAN", "note": "from the table"}

        def get_plan_files(self):
            return {"plan_files": ["PLAN.md"]}

        def validate_plan_structure(self):
            return {"struct": "checked"}

        def extract_plan_steps(self, filename=None):
            return {"filename": filename or "PLAN.md", "steps_count": 0}

    def setUp(self):
        super().setUp()
        self.gateway = Gateway(service=self._Reads())

    def _get(self, path, query=None):
        return self.gateway.dispatch(Request(method="GET", path=path, query=query or {}))

    def test_the_document_read_reaches_the_operation_not_the_plan_id_route(self):
        response = self._get("/api/plan/document")
        self.assertEqual(response.status, 200, response.body)
        body = json.loads(response.body)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["data"]["note"], "from the table")

    def test_every_table_read_under_plan_is_served_by_the_table(self):
        for path, field in (
            ("/api/plan/files", "plan_files"),
            ("/api/plan/steps", "steps_count"),
            ("/api/plan/structure", "struct"),
        ):
            with self.subTest(path=path):
                response = self._get(path)
                self.assertEqual(response.status, 200, response.body)
                self.assertIn(field, json.loads(response.body)["data"])

    def test_a_real_plan_id_still_resolves_through_the_infrastructure_read(self):
        self._seed_plan()
        response = self._get("/api/plan/PLAN")
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(json.loads(response.body)["plan_id"], "PLAN")
        self.assertEqual(self._get("/api/plan/NOPE").status, 404)


class SteeringOperationTests(unittest.TestCase):
    """Phase 33: the two steering operations are on the table and reach the service.

    The logic lives in the service (``test_characterization.SteeringBridgeTests``); this pins the
    transport -- the paths are served, the body is validated, and the answer is the envelope.
    """

    class _Service:
        def __init__(self):
            self.calls = []

        def interrupt_intent(self, intent_id=None, note=""):
            self.calls.append(("interrupt", intent_id, note))
            return {"success": True, "intent_id": intent_id, "status": "paused_awaiting_input"}

        def resume_intent(self, intent_id=None, correction=""):
            self.calls.append(("resume", intent_id, correction))
            return {"success": True, "intent_id": intent_id, "status": "running"}

    def test_interrupt_is_served(self):
        service = self._Service()
        response = Gateway(service=service).dispatch(
            Request(method="POST", path="/api/intent/interrupt",
                    body=b'{"intent_id": "i-1", "note": "hold"}')
        )
        self.assertEqual(response.status, 200)
        data = json.loads(response.body)["data"]
        self.assertTrue(data["success"])
        self.assertEqual(service.calls, [("interrupt", "i-1", "hold")])

    def test_resume_is_served(self):
        service = self._Service()
        response = Gateway(service=service).dispatch(
            Request(method="POST", path="/api/intent/resume",
                    body=b'{"intent_id": "i-1", "correction": "focus"}')
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(service.calls, [("resume", "i-1", "focus")])

    def test_an_intent_id_is_required(self):
        response = Gateway(service=self._Service()).dispatch(
            Request(method="POST", path="/api/intent/interrupt", body=b"{}")
        )
        self.assertEqual(response.status, 400)


class WorkspaceBoundaryTests(GatewayTestCase):
    """The merge boundary's HTTP contract: the intent id is required, and a held lock is a 409."""

    class _Service:
        """Records what reached it, so the test proves the call and not just the status."""

        def __init__(self, *, locked=False):
            self.locked = locked
            self.calls = []

        def workspace_diff(self, intent_id=None):
            self.calls.append(("diff", intent_id))
            return {"success": True, "staged": False,
                    "counts": {"added": 0, "modified": 0, "deleted": 0}}

        def workspace_merge(self, intent_id=None, approve=True):
            from tools.staging import StagingLocked

            self.calls.append(("merge", intent_id, approve))
            if self.locked:
                raise StagingLocked("held by another merge")
            return {"success": True, "applied": {"added": 0, "modified": 0, "deleted": 0}}

    def _gateway(self, **kwargs):
        return Gateway(service=self._Service(**kwargs))

    def test_a_diff_without_an_intent_id_is_a_400(self):
        response = self._gateway().dispatch(Request(method="GET", path="/api/workspace/diff"))
        self.assertEqual(response.status, 400)

    def test_a_merge_without_an_intent_id_is_a_400(self):
        response = self._gateway().dispatch(
            Request(method="POST", path="/api/workspace/merge", body=b"{}")
        )
        self.assertEqual(response.status, 400)

    def test_a_merge_under_a_held_lock_is_a_409(self):
        service = self._Service(locked=True)
        response = Gateway(service=service).dispatch(
            Request(method="POST", path="/api/workspace/merge",
                    body=b'{"intent_id": "i-1", "approve": true}')
        )
        # A conflict has its own status: a 500 would report a bug that is not there.
        self.assertEqual(response.status, 409)
        self.assertIn("locked", response.body.decode("utf-8"))

    def test_a_merge_with_an_intent_id_reaches_the_service(self):
        service = self._Service()
        response = Gateway(service=service).dispatch(
            Request(method="POST", path="/api/workspace/merge",
                    body=b'{"intent_id": "i-2", "approve": false}')
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(service.calls, [("merge", "i-2", False)])


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


class IntentStreamFilterTests(unittest.TestCase):
    """Phase 34: what a per-run stream delivers, decided by one function.

    The rule has two halves, and both are load-bearing: a frame naming a *different* intent is
    dropped (the filter), and an untagged lifecycle frame is delivered (or a client following one
    run would never be told it ended).
    """

    @staticmethod
    def _frame(payload):
        return sse_frame(payload)

    def test_a_frame_for_this_intent_is_delivered(self):
        from api.server import _frame_for_intent

        self.assertTrue(
            _frame_for_intent(self._frame({"type": "agent_thought", "intent_id": "i-1"}), "i-1")
        )

    def test_a_frame_for_another_intent_is_dropped(self):
        from api.server import _frame_for_intent

        self.assertFalse(
            _frame_for_intent(self._frame({"type": "agent_thought", "intent_id": "i-2"}), "i-1")
        )

    def test_an_untagged_lifecycle_frame_is_delivered(self):
        from api.server import _frame_for_intent

        self.assertTrue(
            _frame_for_intent(
                self._frame({"type": "workflow_complete", "status": "finished"}), "i-1"
            )
        )

    def test_a_frame_that_is_not_a_data_frame_or_is_unparseable_is_dropped(self):
        from api.server import _frame_for_intent

        self.assertFalse(_frame_for_intent(": keepalive\n\n", "i-1"))
        self.assertFalse(_frame_for_intent("data: not json\n\n", "i-1"))


class LiveServerTests(GatewayTestCase):
    """The same routes, over a real socket, with the boundary in front of them."""

    def setUp(self):
        super().setUp()
        self.seen_intents = []
        self.intent_seen = threading.Event()

        def on_intent(intent):
            self.seen_intents.append(intent)
            self.intent_seen.set()

        self.service = _StubIntentService()
        self.server = start_gateway(port=0, on_intent=on_intent, service=self.service)
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

    def test_a_refused_post_leaves_its_body_readable(self):
        """A refusal that is never read is a refusal the client cannot see.

        Answering without draining the request body leaves unread bytes in the socket, and
        closing a socket with unread data sends RST rather than FIN -- so on Windows the client
        fails with ``ConnectionAbortedError`` instead of reading the 403. That is what made this
        gate look flaky under load: the refusal was always correct, and whether the client saw it
        depended on timing. Repeated, because a race is what it was.
        """
        payload = json.dumps({"action_type": "custom", "message": "x" * 4096})
        for _ in range(20):
            status, body = self._request(
                "POST", "/api/intent/execute",
                body=payload,
                headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
            )
            self.assertEqual(status, 403)
            self.assertIn("cross-site", body)

    def test_an_accepted_intent_is_drained_by_the_orchestrator_loop(self):
        status, body = self._request(
            "POST", "/api/intent/execute",
            body=json.dumps({"action_type": "next_step", "message": "proceed"}),
            headers={"Content-Type": "application/json", "Origin": "http://127.0.0.1:%d" % self.server.bound_port},
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["data"]["intent_id"])
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

    def test_the_per_intent_stream_delivers_only_that_intent(self):
        """A client following one run must not see a neighbour's events -- and must see its own
        run's untagged lifecycle events, or it would never be told the run ended (Phase 34).

        Three frames are published on the bus: a neighbour's tagged event (dropped), this run's
        tagged event, and an untagged terminal event (both delivered). The filter is the server's,
        not the client's -- it is the boundary.
        """
        received = []
        ready = threading.Event()

        def reader():
            connection = http.client.HTTPConnection("127.0.0.1", self.server.bound_port, timeout=15)
            try:
                connection.request("GET", "/api/intents/i-want/stream")
                response = connection.getresponse()
                ready.set()
                deadline = time.monotonic() + 10
                # Two frames are expected (this run's event, then the untagged terminal); stop as
                # soon as both have arrived rather than waiting out a deadline.
                while len(received) < 2 and time.monotonic() < deadline:
                    line = response.readline()
                    if not line:
                        break
                    if line.startswith(b"data: "):
                        received.append(json.loads(line[len(b"data: "):]))
            except Exception:
                pass
            finally:
                connection.close()

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(timeout=5), "the stream never opened")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.server.gateway.hub.subscriber_count == 0:
            time.sleep(0.02)
        import bridge_bus

        bridge_bus.emit({"type": "agent_thought", "intent_id": "someone-else",
                         "step": 1, "text": "not yours"})
        bridge_bus.emit({"type": "agent_thought", "intent_id": "i-want",
                         "step": 2, "text": "yours"})
        # An untagged lifecycle event: it belongs to the one run this project is executing.
        bridge_bus.emit({"type": "workflow_complete", "status": "finished", "message": "done"})
        thread.join(timeout=10)

        self.assertEqual([event["type"] for event in received], ["agent_thought", "workflow_complete"])
        self.assertEqual(received[0]["intent_id"], "i-want")

    def test_the_bus_listener_is_detached_when_the_server_stops(self):
        self.server.stop()
        import bridge_bus

        bridge_bus.emit(_LOG_EVENT)  # must not raise, and must not reach a closed hub
        self.assertEqual(self.server.gateway.hub.subscriber_count, 0)


class SocketTeardownTests(unittest.TestCase):
    """A stopped server leaves nothing listening and nothing in flight.

    A fixture that hands control back before the socket is actually gone is a fixture whose next
    test can meet the previous one's server -- which is how a security assertion gets an answer
    from a socket it did not start. This pins the guarantee rather than assuming it.
    """

    def test_stop_releases_the_port_and_joins_the_handlers(self):
        server = start_gateway(port=0)
        port = server.bound_port
        httpd = server._httpd

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request("GET", "/api/health")
            self.assertEqual(connection.getresponse().status, 200)
        finally:
            connection.close()

        # While it is serving, the port is genuinely held -- otherwise the check below proves
        # nothing at all.
        self.assertFalse(server._wait_for_release(port, timeout=0.05))

        server.stop()

        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # No SO_REUSEADDR: a socket that is still open would win the bind, so this is the
            # only form of "the port was released" a caller can actually assert.
            probe.bind(("127.0.0.1", port))
        except OSError as error:  # pragma: no cover - the failure this test exists for
            self.fail(f"the port was still held after stop(): {error}")
        finally:
            probe.close()

        self.assertIsNone(server._httpd)
        self.assertTrue(
            all(not thread.is_alive() for thread in httpd.handlers),
            "a request thread outlived stop()",
        )

    def test_stopping_twice_is_harmless(self):
        server = start_gateway(port=0)
        server.stop()
        server.stop()  # must not raise


class ShutdownRefusalTests(unittest.TestCase):
    """Phase 42: a draining engine refuses new work with 503, not a dropped connection.

    The distinction matters: a client that sees a connection reset cannot tell a shutdown from a
    crash, and a client that sees 503 knows the engine is going away on purpose and can stop
    stacking intents into a queue nothing will drain.
    """

    def setUp(self):
        from tools import lifecycle

        lifecycle.reset()
        self.addCleanup(lifecycle.reset)

    def _request(self, port, path):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            connection.close()

    def test_a_request_during_shutdown_is_a_503(self):
        from tools import lifecycle

        server = start_gateway(port=0)
        self.addCleanup(server.stop)

        status, _ = self._request(server.bound_port, "/api/health")
        self.assertEqual(status, 200)

        lifecycle.begin_shutdown()

        status, body = self._request(server.bound_port, "/api/health")
        self.assertEqual(status, 503)
        self.assertIn("shutting down", body)

    def test_the_server_registers_and_forgets_its_drain(self):
        from tools import lifecycle

        server = start_gateway(port=0)
        self.assertIn(server.stop, lifecycle._drains)

        server.stop()

        self.assertNotIn(server.stop, lifecycle._drains)


class SpaRoutingTests(unittest.TestCase):
    """Phase 47: the served bundle is one page, so a client route must not 404.

    The gateway serves the built frontend and the API from one socket. A path with no file
    extension is a *client* route and gets the document; a missing asset is still a 404, because
    answering HTML for a script would break the page rather than help it.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="aleth_spa_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        with open(os.path.join(self.root, "index.html"), "w", encoding="utf-8") as handle:
            handle.write("<!doctype html><title>spa</title>")
        os.makedirs(os.path.join(self.root, "assets"), exist_ok=True)
        with open(os.path.join(self.root, "assets", "app.js"), "w", encoding="utf-8") as handle:
            handle.write("console.log(1)")
        self.server = start_gateway(port=0, static_root=self.root)
        self.addCleanup(self.server.stop)

    def _get(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.bound_port, timeout=10)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            connection.close()

    def test_the_root_serves_the_document(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("spa", body)

    def test_a_client_route_serves_the_document(self):
        status, body = self._get("/plan/42")
        self.assertEqual(status, 200)
        self.assertIn("spa", body)

    def test_a_real_asset_is_served_as_itself(self):
        status, body = self._get("/assets/app.js")
        self.assertEqual(status, 200)
        self.assertIn("console.log", body)

    def test_a_missing_asset_is_still_a_404(self):
        status, _ = self._get("/assets/missing.js")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
