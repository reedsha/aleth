"""The loopback HTTP server: sockets, the security guard, and the SSE transport.

The gateway decides *what* the API answers; this module decides *who may ask*. Four checks stand
between a request and a handler, and each closes a different hole:

1. **The socket binds ``127.0.0.1``** -- not ``0.0.0.0``. An engine that owns a container runtime
   must not be reachable from the network, so the bind is the boundary and the rest is belt to
   its braces.
2. **The peer must be loopback.** A request that somehow arrives from a routable address is
   refused before it is parsed.
3. **The ``Host`` header must name a loopback host.** This is the DNS-rebinding guard: a page on
   ``evil.com`` whose domain resolves to ``127.0.0.1`` reaches this socket, but it sends
   ``Host: evil.com``, which is not a name this server answers to.
4. **A state-changing request's ``Origin`` must be one the app itself served.** A cross-site
   ``POST`` is the one request a hostile page *can* aim at loopback, so the intent endpoint
   refuses any other origin.

Server-sent events get their own loop here because a stream has no end: the gateway returns a
:class:`~api.gateway.StreamResponse` marker and this module turns the hub's subscriber into a
wire, heartbeating so a quiet stream does not look dead and ending the connection the moment the
client goes away.
"""

from __future__ import annotations

import dataclasses
import json
import mimetypes
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Iterable, List, Optional, Sequence, Set
from urllib.parse import parse_qs, unquote, urlsplit

from api.events import FRAME_PREFIX, FRAME_SUFFIX, HEARTBEAT_FRAME, EventHub
from api.gateway import Gateway, Request, Response, StreamResponse
from api.intents import IntentQueue, run_worker
from api.schemas import ErrorResponse
from tools import lifecycle

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PORT_ENV = "ALETH_API_PORT"

# A request body is an intent, not an upload. Bounding it here means a hostile Content-Length
# cannot make the server allocate before the gateway ever sees the bytes.
MAX_BODY_BYTES = 256 * 1024

# How long a streaming thread waits before writing a heartbeat, and the longest a single stream
# is held open. The cap exists so a client that connects and then stops reading cannot pin a
# thread for the life of the process.
SSE_HEARTBEAT_SECONDS = 15.0
SSE_MAX_SECONDS = 3600.0

# Why an unfinished intent is failed at boot. It is the honest answer: nothing was written, and
# the only thing that writes a terminal state is the engine that is no longer there.
RECOVERY_REASON = "the engine stopped before this run finished"

_LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})


def default_port() -> int:
    """The configured port, or :data:`DEFAULT_PORT` when it is unset or nonsense."""
    raw = (os.environ.get(PORT_ENV) or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return value if 0 <= value <= 65535 else DEFAULT_PORT


def is_loopback_address(address: str) -> bool:
    """Whether a peer address is loopback. ``127.0.0.0/8``, ``::1`` and its v4 mapping."""
    value = (address or "").strip().lower()
    if not value:
        return False
    if value in ("::1", "localhost"):
        return True
    if value.startswith("::ffff:"):
        value = value[len("::ffff:"):]
    return value.startswith("127.")


def _host_name(host_header: str) -> str:
    """The name part of a ``Host`` header, without the port and without brackets."""
    value = (host_header or "").strip()
    if value.startswith("["):
        return value.split("]", 1)[0][1:].lower()
    if ":" in value:
        return value.rsplit(":", 1)[0].lower()
    return value.lower()


def host_allowed(host_header: str) -> bool:
    """Whether the server answers to this ``Host``. The DNS-rebinding guard."""
    return _host_name(host_header) in _LOOPBACK_HOSTNAMES


def loopback_origins(port: int) -> Set[str]:
    """The origins a page served *by this server* would carry."""
    return {
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}",
        f"http://[::1]:{port}",
    }


def origin_allowed(origin: str, port: int, extra: Iterable[str] = ()) -> bool:
    """Whether a request's ``Origin`` may reach a state-changing endpoint.

    An **absent** origin is allowed: a non-browser client (a test, curl, the app's own probe)
    sends none, and it is already behind the loopback and ``Host`` checks. A present origin must
    be one the app serves -- its own asset origin, or this server's own loopback origin.
    """
    value = (origin or "").strip()
    if not value:
        return True
    return value in set(extra) or value in loopback_origins(port)


def _frame_for_intent(frame: str, intent_id: str) -> bool:
    """Whether an SSE data frame belongs on the stream for ``intent_id``. One rule, one place.

    A frame is delivered when it **names this intent** or **names none**. The untagged case is not
    a loophole: an event with no ``intent_id`` is a *global* one -- the run's lifecycle
    (``workflow_started``/``workflow_complete``), a plan or workspace change -- and the engine runs
    **one intent per project at a time** (the ledger mutex, Phase 25), so an untagged frame on this
    stream belongs to the run being followed. Dropping them would be the worse bug: a client would
    follow a run to its last tool call and never be told it ended.

    A frame naming a *different* intent is dropped, and so is one that cannot be parsed: the filter
    is the boundary, and a boundary that guessed would hand a client another run's events.
    """
    if not frame.startswith(FRAME_PREFIX):
        return False
    body = frame[len(FRAME_PREFIX):]
    if body.endswith(FRAME_SUFFIX):
        body = body[: -len(FRAME_SUFFIX)]
    try:
        payload = json.loads(body)
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    named = payload.get("intent_id")
    return not named or str(named) == intent_id


class _ThreadingServer(ThreadingHTTPServer):
    """The socket, plus the four things a handler needs to answer a request."""

    # A wedged handler must never hold the process open, so the threads are daemons -- but that
    # also means ``server_close()`` does not wait for them. They are tracked here so ``stop()``
    # can: a request from a finished test still in flight is a request that can answer into the
    # next one.
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address,
        handler,
        *,
        gateway: Gateway,
        allowed_origins: Set[str],
        stop_event: threading.Event,
        static_root: str = "",
    ):
        super().__init__(address, handler)
        self.gateway = gateway
        self.allowed_origins = allowed_origins
        self.stop_event = stop_event
        self.handlers: List[threading.Thread] = []
        # The built frontend. Serving it from this socket is what makes the window an ordinary
        # browser on the gateway's own origin: no CORS, no "null" origin, and no second way for
        # the page to learn where the API lives -- it is where the page came from.
        self.static_root = str(static_root or "")

    def process_request(self, request, client_address) -> None:
        thread = threading.Thread(
            target=self.process_request_thread, args=(request, client_address), daemon=True
        )
        self.handlers.append(thread)
        thread.start()

    def join_handlers(self, timeout: float) -> bool:
        """Wait for in-flight request threads, bounded. Returns whether all of them finished."""
        deadline = time.monotonic() + timeout
        for thread in list(self.handlers):
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return all(not thread.is_alive() for thread in self.handlers)


class _Handler(BaseHTTPRequestHandler):
    """One connection. Parses nothing it has not already been allowed to parse."""

    protocol_version = "HTTP/1.1"
    server_version = "aleth-api"

    # -- logging ---------------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:
        print(f"[api] {self.address_string()} {fmt % args}", file=sys.stderr)

    # -- helpers ---------------------------------------------------------------
    @property
    def _server(self) -> _ThreadingServer:
        return self.server  # type: ignore[return-value]

    def _drain_body(self) -> None:
        """Discard any request body the client sent.

        A refusal that answers *without* reading the body leaves unread bytes in the socket's
        receive buffer, and closing a socket with unread data sends RST rather than FIN. On
        Windows the client then fails with ``ConnectionAbortedError`` instead of reading the
        403 -- a refusal that arrives as a transport error, which is why this gate looked
        flaky under load: whether the response was seen before the reset depended on timing.

        Draining first is the whole fix, and it is the correct HTTP behaviour regardless: a
        server that closes a connection must not leave the client's bytes unread.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return
        remaining = min(max(length, 0), MAX_BODY_BYTES)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                return
            remaining -= len(chunk)

    def _denied(self, status: int, error: str, detail: str = "") -> None:
        # Before answering: the body is read and discarded, so the connection can close cleanly.
        self._drain_body()
        body = ErrorResponse(error=error, detail=detail).model_dump_json().encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _cors(self) -> Dict[str, str]:
        """The CORS headers, emitted only for an origin the app itself served."""
        origin = self.headers.get("Origin", "")
        headers = {"Vary": "Origin"}
        if origin and origin_allowed(origin, self._server.server_port, self._server.allowed_origins):
            headers["Access-Control-Allow-Origin"] = origin
            headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            headers["Access-Control-Allow-Headers"] = "Content-Type"
            headers["Access-Control-Max-Age"] = "600"
        return headers

    def _guard(self, *, state_changing: bool) -> bool:
        """The boundary. Returns True when the request may proceed; answers it otherwise."""
        peer = self.client_address[0] if self.client_address else ""
        if not is_loopback_address(peer):
            self._denied(403, "not loopback", str(peer))
            return False
        if lifecycle.is_shutting_down():
            # Phase 42: a draining engine accepts no new work. Answered rather than dropped, so a
            # client learns the engine is going away instead of seeing a bare connection error --
            # and so a request that arrives mid-drain cannot start a run the engine will not finish.
            # Checked *after* the loopback gate: a foreign peer is still refused as a stranger, not
            # told the engine happens to be stopping.
            self._denied(503, "the engine is shutting down")
            return False
        if not host_allowed(self.headers.get("Host", "")):
            self._denied(403, "unrecognised host", str(self.headers.get("Host", "")))
            return False
        if state_changing and not origin_allowed(
            self.headers.get("Origin", ""), self._server.server_port, self._server.allowed_origins
        ):
            self._denied(403, "cross-site request refused", str(self.headers.get("Origin", "")))
            return False
        return True

    def _read_body(self) -> Optional[bytes]:
        """The request body, or ``None`` when it is too large (already answered)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self._denied(400, "bad Content-Length")
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            self._denied(413, "request body too large", str(length))
            return None
        return self.rfile.read(length) if length else b""

    def _write(self, response: Response) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        for name, value in self._cors().items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(response.body)

    def _stream(self, intent_id: str = "") -> None:
        """Server-sent events: the hub's subscriber, framed onto the wire until the client goes.

        ``intent_id`` narrows the stream to one run (Phase 33). When it is set, a frame naming a
        *different* intent is dropped before it reaches the wire, so a client following one run
        never sees a neighbour's events; an untagged frame (the run's own lifecycle, a plan change)
        is delivered, because dropping it would leave the client following a run that had ended
        (Phase 34). Empty is the firehose, and that path is byte-for-byte what it always was.
        """
        hub = self._server.gateway.hub
        subscriber = hub.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            # Framing by connection close: a stream has no Content-Length, and chunked encoding
            # buys nothing a local client needs.
            self.send_header("Connection", "close")
            for name, value in self._cors().items():
                self.send_header(name, value)
            self.end_headers()
            # A comment and a retry hint: both valid SSE, neither a bus event, so the data frames
            # stay exactly the vocabulary the frontend already validates.
            self.wfile.write(b": connected\n\nretry: 1000\n\n")
            self.wfile.flush()

            deadline = time.monotonic() + SSE_MAX_SECONDS
            while not self._server.stop_event.is_set() and time.monotonic() < deadline:
                frame = subscriber.next_frame(timeout=SSE_HEARTBEAT_SECONDS)
                # A frame that is for another run is not written at all: the client asked for one
                # intent, and a filtered stream that leaked its neighbours would be worse than no
                # stream. The wait still elapsed for a heartbeat, so the loop stays responsive.
                if frame is not None and intent_id and not _frame_for_intent(frame, intent_id):
                    continue
                self.wfile.write((frame or HEARTBEAT_FRAME).encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            hub.unsubscribe(subscriber)

    # -- verbs ----------------------------------------------------------------
    def do_OPTIONS(self) -> None:  # noqa: N802 - the base class names it
        if not self._guard(state_changing=True):
            return
        self.send_response(204)
        for name, value in self._cors().items():
            self.send_header(name, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if not self._guard(state_changing=False):
            return
        path = urlsplit(self.path).path
        if not path.startswith("/api/") and path != "/console":
            self._serve_static(path)
            return
        self._dispatch(b"")

    def _serve_static(self, path: str) -> None:
        """Serve the built frontend from ``static_root``. Never escapes that directory.

        The page is served by the same socket that answers ``/api``, so the document's origin
        *is* the gateway's: the API needs no CORS exemption for its own UI, and a browser client
        cannot be confused about which server it is talking to.
        """
        root = self._server.static_root
        if not root or not os.path.isdir(root):
            self._denied(404, "the frontend is not being served", path)
            return
        relative = unquote(path).lstrip("/") or "index.html"
        candidate = os.path.realpath(os.path.join(root, relative))
        base = os.path.realpath(root)
        # A path that resolves outside the bundle is refused, not normalised into something
        # that happens to exist: ``..`` must never reach the repository it was built from.
        if candidate != base and not candidate.startswith(base + os.sep):
            self._denied(404, "no such asset", path)
            return
        if os.path.isdir(candidate):
            candidate = os.path.join(candidate, "index.html")
        if not os.path.isfile(candidate):
            self._denied(404, "no such asset", path)
            return
        with open(candidate, "rb") as handle:
            body = handle.read()
        content_type = mimetypes.guess_type(candidate)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript",):
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in self._cors().items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard(state_changing=True):
            return
        body = self._read_body()
        if body is None:
            return
        self._dispatch(body)

    def _dispatch(self, body: bytes) -> None:
        parts = urlsplit(self.path)
        request = Request(
            method=self.command,
            path=parts.path,
            query=parse_qs(parts.query, keep_blank_values=True),
            body=body,
        )
        answer = self._server.gateway.dispatch(request)
        if isinstance(answer, StreamResponse):
            self._stream(intent_id=answer.intent_id)
            return
        self._write(answer)


@dataclasses.dataclass
class ApiServer:
    """The bound socket, the intent worker, and the hub's attachment to the event bus."""

    gateway: Gateway
    host: str = LOOPBACK_HOST
    port: int = DEFAULT_PORT
    allowed_origins: Set[str] = dataclasses.field(default_factory=set)
    # The built frontend to serve at ``/``. Empty means "API only", which is what the tests want.
    static_root: str = ""

    def __post_init__(self) -> None:
        self.stop_event = threading.Event()
        self._httpd: Optional[_ThreadingServer] = None
        self._thread: Optional[threading.Thread] = None
        self._worker: Optional[threading.Thread] = None
        self._worker_stop: Optional[threading.Event] = None
        self._bus_attached = False

    # -- lifecycle ------------------------------------------------------------
    def attach_bus(self) -> None:
        """Make this hub the bus's **transport**, so every event streams to the clients.

        There is one delivery path now. The bus used to push into the pywebview window with
        ``evaluate_js``; pointing its transport at the hub means the desktop window and a browser
        client receive the identical validated event, because they are reading the same stream.
        """
        if self._bus_attached:
            return
        import bridge_bus
        from api.events import HubTransport

        bridge_bus.set_transport(HubTransport(self.gateway.hub))
        self._bus_attached = True

    def detach_bus(self) -> None:
        """Restore the inert transport, so a stopped server stops receiving events."""
        if not self._bus_attached:
            return
        import bridge_bus

        bridge_bus.set_transport(bridge_bus.NullTransport())
        self._bus_attached = False

    def start_intent_worker(self, handler) -> None:
        """Start the background orchestrator loop that drains the intent queue."""
        if self._worker is not None:
            return
        self._worker_stop = threading.Event()
        self._worker = threading.Thread(
            target=run_worker,
            args=(self.gateway.queue, handler, self._worker_stop),
            name="aleth-intent-worker",
            daemon=True,
        )
        self._worker.start()

    def _bind(self, port: int) -> _ThreadingServer:
        return _ThreadingServer(
            (self.host, port),
            _Handler,
            gateway=self.gateway,
            allowed_origins=set(self.allowed_origins),
            stop_event=self.stop_event,
            static_root=self.static_root,
        )

    def start(self) -> str:
        """Bind and serve. Returns the base URL. Falls back to an ephemeral port when busy."""
        try:
            self._httpd = self._bind(self.port)
        except OSError:
            if self.port == 0:
                raise
            print(
                f"[api] port {self.port} is taken; binding an ephemeral one instead",
                file=sys.stderr,
            )
            self._httpd = self._bind(0)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="aleth-api", daemon=True
        )
        self._thread.start()
        # Phase 42: the engine's drain stops this server. Registered rather than reached into, so
        # the signal handler stays in the container perimeter and knows nothing about the API.
        lifecycle.register_drain(self.stop)
        return self.base_url

    def stop(self) -> None:
        """Stop serving, release subscribers, and wait for the socket to actually be gone.

        Idempotent, and never raises. The waiting is the point: a caller that hands control back
        while the listening socket is still open -- or while a request thread is still running --
        is a caller whose next test can meet the previous one's server. That is how a security
        assertion ends up answered by a socket it did not start.
        """
        lifecycle.unregister_drain(self.stop)
        self.stop_event.set()
        if self._worker_stop is not None:
            self._worker_stop.set()
            self.gateway.queue.wake()
        # Wakes any streaming handler out of its wait, so the handler threads below finish
        # promptly instead of sitting out their heartbeat interval.
        self.gateway.hub.close_all()
        httpd, self._httpd = self._httpd, None
        port = 0
        if httpd is not None:
            port = int(httpd.server_address[1])
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)
        if httpd is not None:
            try:
                httpd.join_handlers(timeout=5)
            except Exception:
                pass
        if port:
            self._wait_for_release(port)
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.join(timeout=5)
        self.detach_bus()

    def _wait_for_release(self, port: int, timeout: float = 5.0) -> bool:
        """Wait until nothing is listening on ``port`` any more.

        ``server_close()`` closes the descriptor, but a descriptor closed is not the same claim
        as a port released, and this is the only form of that claim a caller can actually make.
        The probe binds *without* ``SO_REUSEADDR`` on purpose: with it, the probe would succeed
        against a socket that is still open, which would make the check worthless.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                probe.bind((self.host, port))
                return True
            except OSError:
                time.sleep(0.01)
            finally:
                probe.close()
        return False

    # -- addressing -----------------------------------------------------------
    @property
    def bound_port(self) -> int:
        return int(self._httpd.server_address[1]) if self._httpd is not None else int(self.port)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.bound_port}"


def start_gateway(
    *,
    service=None,
    ledger=None,
    port: Optional[int] = None,
    host: str = LOOPBACK_HOST,
    allowed_origins: Sequence[str] = (),
    on_intent=None,
    capacity: int = 32,
    static_root: str = "",
) -> ApiServer:
    """Build the queue, hub and router; attach the bus; optionally start the worker; bind.

    This is the one function the app calls. Everything it wires is real from the first request:
    the hub is attached to the bus before the socket opens, so an event emitted the moment the
    server is up reaches a client that connected for it.

    ``service`` is the engine-facing object the operation table calls. Passing ``None`` leaves the
    read-only endpoints working and every operation a 503 -- which is what a test that only wants
    the state surface asks for. ``static_root`` mounts the built frontend at ``/``.

    ``ledger`` is reconciled **before** anything is served. An intent left unfinished in it is one
    whose engine stopped -- crashed, OOM-killed, powered off -- and marking it failed at boot is
    the only deterministic answer available, because the process that would have written a
    terminal state is gone. It is deliberately not a retry: the user is told, and decides.
    """
    if ledger is not None:
        try:
            recovered = ledger.reconcile(RECOVERY_REASON)
            if recovered:
                print(
                    f"[intents] {recovered} intent(s) never finished and were marked failed "
                    f"({RECOVERY_REASON})",
                    file=sys.stderr,
                )
        except Exception as error:  # a boot that cannot reconcile still boots
            print(
                f"[intents] the ledger could not be reconciled: {type(error).__name__}: {error}",
                file=sys.stderr,
            )

    gateway = Gateway(
        queue=IntentQueue(capacity=capacity, ledger=ledger),
        hub=EventHub(),
        service=service,
    )
    if service is not None:
        # Hand the engine service the queue it now answers for. The operations that touch intents
        # (``get_intent_status``, ``submit_intent``) live in the service layer, so the typed
        # operation table stays the only intent surface -- and the service is injected here, where
        # the queue is minted, rather than reaching into the gateway later.
        service.attach_intent_queue(gateway.queue)
    server = ApiServer(
        gateway=gateway,
        host=host,
        port=default_port() if port is None else int(port),
        allowed_origins=set(allowed_origins),
        static_root=str(static_root or ""),
    )
    server.attach_bus()
    if on_intent is not None:
        server.start_intent_worker(on_intent)
    server.start()
    return server


__all__ = [
    "ApiServer",
    "start_gateway",
    "default_port",
    "is_loopback_address",
    "host_allowed",
    "origin_allowed",
    "loopback_origins",
    "LOOPBACK_HOST",
    "DEFAULT_PORT",
    "PORT_ENV",
]
