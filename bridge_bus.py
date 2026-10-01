"""The single, typed server -> client event bus.

Every payload the backend pushes to the webview crosses exactly one seam: this bus. It
replaces the ad-hoc ``window.evaluate_js(f"window.onAgentEvent({json})")`` calls that
were built by string interpolation at each call site.

Two guarantees:

* **Typed.** :meth:`BridgeBus.dispatch` validates the payload against the strict
  discriminated union in ``tools.payloads`` (``extra="forbid"``) *before* it is
  serialised. An unknown ``type`` or an undocumented key raises at the boundary.
* **Inert.** The serialised payload is handed to the frontend as a JSON *string*
  argument to one fixed sink, ``window.__deepAgentsBus.receive``. The sink call is a
  constant; only a JSON string literal varies, and it is encoded with
  ``ensure_ascii=True`` so it can never terminate the call or introduce executable
  code. The client parses it with ``JSON.parse`` and validates it again.

pywebview offers no data channel of its own -- ``evaluate_js`` is the only server ->
client push -- so the safety does not come from avoiding ``evaluate_js``; it comes from
never interpolating anything but a safely-encoded JSON string into a single static sink.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Protocol

from tools.payloads import validated_bus_event

# The one sink every outbound event is delivered to. It is a *constant*: the call site
# is fixed, and the only thing that varies is the JSON string argument.
SINK_NAME = "window.__deepAgentsBus"
_SINK_CALL = "window.__deepAgentsBus && window.__deepAgentsBus.receive("


def sink_script(json_text: str) -> str:
    """The single static call that delivers ``json_text`` to the frontend sink.

    The payload is embedded as a JSON *string literal* (a second ``json.dumps``) rather
    than as a raw object literal, so the argument is unambiguously data: even a
    hypothetical malformed payload could only produce a ``JSON.parse`` failure, never
    executable code. ``ensure_ascii=True`` escapes every non-ASCII character --
    including U+2028/U+2029, which are valid in JSON but not in a JS string literal.
    """
    return _SINK_CALL + json.dumps(json_text, ensure_ascii=True) + ");"


class Transport(Protocol):
    """Where a serialised event is delivered."""

    def send(self, json_text: str) -> None:
        """Deliver ``json_text`` (a JSON document) to the client."""


class WebviewTransport:
    """Delivers events to a pywebview window through the static sink.

    ``get_window`` is a callable rather than a window so the transport survives the
    window being bound (or replaced) after the bus is constructed -- ``BridgeAPI``
    builds the bus in ``__init__`` but only receives the window from ``set_window``.
    A callable that returns ``None`` makes delivery a silent no-op, which is what keeps
    an event emitted before the window exists from raising.
    """

    def __init__(self, get_window: Callable[[], Any]):
        self._get_window = get_window

    def send(self, json_text: str) -> None:
        window = self._get_window()
        if window is None:
            return
        window.evaluate_js(sink_script(json_text))


class BridgeBus:
    """Validates and dispatches outbound events to a transport, and to local listeners."""

    def __init__(self, transport: Transport):
        self._transport = transport
        # Local observers: the API gateway's SSE hub is one. They are *in-process* consumers of
        # the same validated event the transport carries, which is what keeps a browser client and
        # the desktop window from being two renderings that can drift.
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []

    def set_transport(self, transport: Transport) -> None:
        """Repoint the bus at a different transport (the app binds the webview one)."""
        self._transport = transport

    def add_listener(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        """Observe every dispatched event. Idempotent."""
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        """Stop observing. Idempotent."""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def dispatch(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Validate ``event``, deliver it to the sink, and return it unchanged.

        Raises ``pydantic.ValidationError`` (unknown type / undocumented key) or
        ``TypeError`` (a value that is not JSON-serialisable) at the boundary rather
        than swallowing it. The event is returned unchanged so a caller keeps the exact
        dict it built -- its key order is part of the wire contract.

        Listeners are notified **after** the transport and can never change the answer: one that
        raises is not allowed to break the emit that reached it, because the emitter is the
        workflow thread and the listener is an observer.
        """
        validated_bus_event(event)
        self._transport.send(json.dumps(event, ensure_ascii=True, separators=(",", ":")))
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                pass
        return event


class _NullTransport:
    """Drops everything: the bus before the window exists, and the tests."""

    def send(self, json_text: str) -> None:
        return


# The process-wide bus. The app binds the webview transport on construction; any other
# module (the state kernel, the workflow) emits through :func:`emit` without needing a
# reference to the window.
_bus = BridgeBus(_NullTransport())


def set_transport(transport: Transport) -> None:
    """Point the process-wide bus at ``transport``."""
    _bus.set_transport(transport)


def add_listener(listener: Callable[[Dict[str, Any]], None]) -> None:
    """Observe every event on the process-wide bus (the API gateway's SSE hub)."""
    _bus.add_listener(listener)


def remove_listener(listener: Callable[[Dict[str, Any]], None]) -> None:
    """Stop observing the process-wide bus."""
    _bus.remove_listener(listener)


def emit(event: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and dispatch one event on the process-wide bus."""
    return _bus.dispatch(event)
