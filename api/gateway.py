"""Routing: a request in, a response out, with no socket in sight.

Everything about *what the API answers* lives here, and nothing about *how it is carried*. That
split is what makes the contract testable without binding a port: a test builds a
:class:`Request` and asserts on the :class:`Response`, and the same handlers serve the real
socket in ``api.server``.

The router is deliberately small and explicit. A path is matched by a compiled pattern against a
table of ``(method, pattern, handler)``; there is no framework, no dependency injection and no
decorator magic, so the whole surface of the API is one list a reader can check.
"""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple, Union

from pydantic import ValidationError

from api import operations, reads
from api.events import EventHub
from api.intents import IntentQueue
from api.schemas import (
    ErrorResponse,
    HealthResponse,
    OperationEnvelope,
    ServiceRefusal,
)

SERVICE_NAME = "aleth-api"
SERVICE_VERSION = "1.0"

JSON_CONTENT_TYPE = "application/json; charset=utf-8"


@dataclasses.dataclass(frozen=True)
class Request:
    """One inbound request, already parsed out of its socket."""

    method: str
    path: str
    query: Mapping[str, Sequence[str]] = dataclasses.field(default_factory=dict)
    body: bytes = b""


@dataclasses.dataclass(frozen=True)
class Response:
    """A complete, in-memory response."""

    status: int
    body: bytes
    content_type: str = JSON_CONTENT_TYPE


@dataclasses.dataclass(frozen=True)
class StreamResponse:
    """A response the *server* streams (server-sent events).

    The gateway decides that a route streams; it never builds the bytes, because a stream has no
    end. ``api.server`` owns the loop that turns the hub's subscribers into a wire.
    """


Handler = Callable[[Request, "Gateway", re.Match], Union[Response, StreamResponse]]


def _json(status: int, model) -> Response:
    return Response(status=status, body=model.model_dump_json().encode("utf-8"))


def _error(status: int, error: str, detail: str = "") -> Response:
    return _json(status, ErrorResponse(error=error, detail=detail))


def _first(query: Mapping[str, Sequence[str]], name: str) -> Optional[str]:
    values = query.get(name)
    return values[0] if values else None


# -- handlers ---------------------------------------------------------------------
def _health(request: Request, gateway: "Gateway", match: re.Match) -> Response:
    return _json(
        200,
        HealthResponse(
            service=SERVICE_NAME,
            version=SERVICE_VERSION,
            loopback_only=True,
            # The gateway is read-only *except* for the intent queue, and saying so in the
            # handshake is cheaper than a caller discovering it by trying.
            read_only=False,
            subscribers=gateway.hub.subscriber_count,
            active_plan_id=reads.active_plan_id(),
        ),
    )


def _plans(request: Request, gateway: "Gateway", match: re.Match) -> Response:
    return _json(200, reads.plans_view())


def _active_plan(request: Request, gateway: "Gateway", match: re.Match) -> Response:
    plan = reads.plan_view(reads.active_plan_id())
    if plan is None:
        return _error(404, "no active plan in the store", reads.active_plan_file())
    return _json(200, plan)


def _plan_by_id(request: Request, gateway: "Gateway", match: re.Match) -> Response:
    plan = reads.plan_view(match.group("plan_id"))
    if plan is None:
        return _error(404, "unknown plan", match.group("plan_id"))
    return _json(200, plan)


def _telemetry(request: Request, gateway: "Gateway", match: re.Match) -> Response:
    raw_limit = _first(request.query, "limit")
    try:
        limit = int(raw_limit) if raw_limit else 50
    except (TypeError, ValueError):
        return _error(400, "limit must be an integer", str(raw_limit))
    session_id = _first(request.query, "session_id")
    return _json(200, reads.telemetry_view(session_id=session_id, limit=limit))


def _receipt(request: Request, gateway: "Gateway", match: re.Match) -> Response:
    receipt = reads.receipt_view(match.group("execution_id"))
    if receipt is None:
        return _error(404, "unknown execution", match.group("execution_id"))
    return _json(200, receipt)


def _events(request: Request, gateway: "Gateway", match: re.Match) -> StreamResponse:
    """Hand the connection to the SSE loop; the gateway has nothing else to say about it."""
    return StreamResponse()


# The bare infrastructure reads -- health, the plan projections, the telemetry ledger and the
# event stream -- are the gateway's own. Everything the *engine* can do is in
# ``api.operations``: one typed table, so the intent surface has no privileged route beside it.
ROUTES: Tuple[Tuple[str, str, Handler], ...] = (
    ("GET", r"^/api/health$", _health),
    ("GET", r"^/api/plans$", _plans),
    ("GET", r"^/api/plan$", _active_plan),
    ("GET", r"^/api/plan/(?P<plan_id>[A-Za-z0-9_.\-]+)$", _plan_by_id),
    ("GET", r"^/api/telemetry$", _telemetry),
    ("GET", r"^/api/telemetry/(?P<execution_id>[A-Za-z0-9_.\-]+)$", _receipt),
    ("GET", r"^/api/events$", _events),
)


class Gateway:
    """The router: one queue, one hub, one service, and the routes over them."""

    def __init__(
        self,
        queue: Optional[IntentQueue] = None,
        hub: Optional[EventHub] = None,
        service: Any = None,
    ):
        self.queue = queue or IntentQueue()
        self.hub = hub or EventHub()
        # The engine-facing operations (``api.operations``). ``None`` until the app attaches it,
        # and an operation reaching a gateway without one is a 503 rather than an AttributeError.
        self.service = service
        self._routes = [
            (method, re.compile(pattern), handler) for method, pattern, handler in ROUTES
        ]

    def dispatch(self, request: Request) -> Union[Response, StreamResponse]:
        """Route one request. Always answers; never raises for a bad request."""
        path = request.path or "/"
        matched_method = False
        for method, pattern, handler in self._routes:
            match = pattern.match(path)
            if match is None:
                continue
            if method != request.method.upper():
                matched_method = True
                continue
            return handler(request, self, match)
        # The typed operation surface: a closed table, so a path that is not in it is a 404 and
        # an operation that is not implemented has no address at all.
        if operations.is_known_path(path):
            return self._dispatch_operation(request)
        if matched_method:
            # The path exists but not for this verb: 405 says so, where 404 would send a reader
            # looking for a typo in the path they got right.
            return _error(405, "method not allowed", f"{request.method} {path}")
        return _error(404, "no such endpoint", path)

    def _dispatch_operation(self, request: Request) -> Response:
        """Validate the body against the operation's own model, then call the service."""
        operation = operations.operation_for(request.method, request.path)
        if operation is None:
            return _error(405, "method not allowed", f"{request.method} {request.path}")
        if self.service is None:
            return _error(503, "the engine service is not attached", operation.name)
        try:
            raw = _operation_body(operation, request)
            parsed = operation.request.model_validate(raw)
        except ValidationError as error:
            return _error(400, "invalid request", error.json(include_url=False))
        except ValueError as error:
            return _error(400, "invalid request", str(error))
        try:
            payload = operation.call(self.service, parsed)
        except Exception as error:  # a service fault is the operation's answer, not a crash
            return _error(500, "the operation failed", f"{type(error).__name__}: {error}")
        fault = _contract_fault(operation.name, payload)
        if fault:
            # A service that broke the envelope is a server fault, and saying so is the only safe
            # answer: sending the payload on would hand the client a failure it reads as success.
            return _error(500, "the operation broke the response contract", fault)
        try:
            envelope = OperationEnvelope(ok=True, data=payload)
            return Response(status=200, body=envelope.model_dump_json().encode("utf-8"))
        except Exception as error:
            return _error(500, "the answer could not be serialised", str(error))


def _contract_fault(name: str, payload: Any) -> str:
    """Whether a service answer breaks the envelope contract, and why.

    The rule is one sentence: **a payload that reports an ``error`` must also report
    ``success: false``**. The client's type guard keys on that flag -- it is what becomes a thrown
    ``ApiError`` -- so a bare ``{"error": ...}`` is delivered as a *successful* answer and read as
    one, which is exactly how a failure becomes corrupted state instead of an exception.

    Checked here rather than trusted: this is the boundary, and a boundary that assumes its
    inputs are well-formed is not one. A refusal is validated against
    :class:`~api.schemas.ServiceRefusal`; anything else is reported as the fault it is.
    """
    if not isinstance(payload, dict) or not payload.get("error"):
        return ""
    try:
        ServiceRefusal.model_validate(payload)
    except ValidationError as error:
        return f"{name} reported an error without success=false: {error.error_count()} field(s)"
    return ""


def _operation_body(operation: Any, request: Request) -> Dict[str, Any]:
    """The arguments for an operation: the query for a GET, the JSON body for anything else.

    A GET carries its arguments in the query string because that is what a GET is for -- a
    readable, cacheable read -- and the operation's model coerces the strings to its own types.
    """
    if operation.verb == "GET":
        return {name: values[0] for name, values in (request.query or {}).items() if values}
    if not request.body:
        return {}
    try:
        parsed = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError(f"the request body is not JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ValueError("the request body must be a JSON object")
    return parsed


__all__ = [
    "Gateway",
    "Request",
    "Response",
    "StreamResponse",
    "ROUTES",
    "JSON_CONTENT_TYPE",
]
