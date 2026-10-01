"""The single, typed server -> client event bus.

Every event the engine announces crosses exactly one seam: this bus. It validates the payload
against the strict discriminated union in ``tools.payloads`` (``extra="forbid"``) *before* it is
serialised, so an unknown ``type`` or an undocumented key raises at the boundary rather than
reaching a renderer.

There is exactly one transport, and it is the API gateway's SSE hub
(``api.events.HubTransport``). The bus used to inject a call into the pywebview window with
``evaluate_js``; that path, its static sink, and the second transport it needed are gone. The
desktop window is now a browser reading the same stream every other client reads, so an event
has one delivery mechanism and one contract to satisfy.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Protocol

from tools.payloads import validated_bus_event


class Transport(Protocol):
    """Where a serialised event is delivered."""

    def send(self, json_text: str) -> None:
        """Deliver ``json_text`` (a JSON document) to the client."""


class NullTransport:
    """Drops everything: the bus before the gateway binds it, and the tests."""

    def send(self, json_text: str) -> None:
        return


class BridgeBus:
    """Validates and dispatches outbound events to its transport."""

    def __init__(self, transport: Transport):
        self._transport = transport

    def set_transport(self, transport: Transport) -> None:
        """Repoint the bus at a different transport (the gateway binds the stream's hub)."""
        self._transport = transport

    def dispatch(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Validate ``event``, deliver it, and return it unchanged.

        Raises ``pydantic.ValidationError`` (unknown type / undocumented key) or
        ``TypeError`` (a value that is not JSON-serialisable) at the boundary rather than
        swallowing it. The event is returned unchanged so a caller keeps the exact dict it
        built -- its key order is part of the wire contract.
        """
        validated_bus_event(event)
        self._transport.send(json.dumps(event, ensure_ascii=True, separators=(",", ":")))
        return event


# The process-wide bus. The gateway binds the stream's hub as its transport on startup; any other
# module (the state kernel, the workflow) emits through :func:`emit` without needing a reference
# to the server.
_bus = BridgeBus(NullTransport())


def set_transport(transport: Transport) -> None:
    """Point the process-wide bus at ``transport``."""
    _bus.set_transport(transport)


def emit(event: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and dispatch one event on the process-wide bus."""
    return _bus.dispatch(event)
