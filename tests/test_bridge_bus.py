"""The typed IPC bus: validation at the boundary and one delivery path.

Two properties are pinned here:

* :meth:`bridge_bus.BridgeBus.dispatch` validates the payload against the strict event
  union *before* it is delivered -- an unknown type or an undocumented key raises, and
  nothing reaches the transport; and
* the transport is the API gateway's SSE hub, so a payload is delivered as one ``data:``
  frame carrying compact JSON. Hostile text stays *data*: the client parses the frame with
  ``JSON.parse``, and the frame itself cannot be terminated by a payload, because JSON
  serialisation escapes the newlines a frame delimiter would need.

    .\\venv\\Scripts\\python.exe -m pytest tests/test_bridge_bus.py -n0 -q
"""

import json
import unittest

from pydantic import ValidationError

from bridge_bus import BridgeBus, NullTransport
from api.events import EventHub, HubTransport


class _Recorder:
    """A transport that records the JSON text it is handed."""

    def __init__(self):
        self.sent = []

    def send(self, json_text):
        self.sent.append(json_text)


class DispatchTests(unittest.TestCase):
    def test_a_valid_event_is_validated_then_delivered_unchanged(self):
        recorder = _Recorder()
        event = {"type": "log", "agent": "a", "log_type": "thinking", "text": "hi"}
        self.assertIs(BridgeBus(recorder).dispatch(event), event)
        self.assertEqual(json.loads(recorder.sent[0]), event)

    def test_an_unknown_type_is_rejected_at_the_boundary(self):
        with self.assertRaises(ValidationError):
            BridgeBus(_Recorder()).dispatch({"type": "totally_unknown"})

    def test_an_undocumented_key_is_rejected_at_the_boundary(self):
        with self.assertRaises(ValidationError):
            BridgeBus(_Recorder()).dispatch(
                {"type": "workflow_complete", "status": "finished", "message": "m", "extra": 1}
            )

    def test_a_rejected_event_never_reaches_the_transport(self):
        recorder = _Recorder()
        with self.assertRaises(ValidationError):
            BridgeBus(recorder).dispatch({"type": "workflow_complete", "surprise": True})
        self.assertEqual(recorder.sent, [])

    def test_the_detached_console_line_is_a_first_class_bus_event(self):
        recorder = _Recorder()
        BridgeBus(recorder).dispatch({"type": "console_line", "kind": "tool", "text": "x"})
        self.assertEqual(json.loads(recorder.sent[0])["type"], "console_line")

    def test_the_null_transport_swallows_everything(self):
        NullTransport().send('{"type":"log"}')  # must not raise, must not store


class StreamTransportTests(unittest.TestCase):
    """The bus's one transport is the stream: an event arrives as a ``data:`` frame."""

    def test_an_event_reaches_a_subscriber_as_one_data_frame(self):
        hub = EventHub()
        subscriber = hub.subscribe()
        BridgeBus(HubTransport(hub)).dispatch(
            {"type": "log", "agent": "a", "log_type": "thinking", "text": "hi"}
        )
        frame = subscriber.next_frame(timeout=0.5)
        self.assertIsNotNone(frame)
        self.assertTrue(frame.startswith("data: "))
        self.assertTrue(frame.endswith("\n\n"))
        self.assertEqual(json.loads(frame[len("data: "):-2])["type"], "log")

    def test_a_hostile_payload_stays_inside_one_frame(self):
        """A payload cannot forge a frame boundary: JSON escapes the newlines it would need."""
        hostile = {
            "type": "log",
            "agent": "a",
            "log_type": "x",
            "text": '"); window.__pwned = 1; ("\n\ndata: {"type":"workflow_complete"}',
        }
        hub = EventHub()
        subscriber = hub.subscribe()
        BridgeBus(HubTransport(hub)).dispatch(hostile)
        frame = subscriber.next_frame(timeout=0.5)

        # Exactly one frame, and exactly one blank-line terminator: the injected blank line and
        # the fake `data:` line are escaped into the JSON string, not emitted as framing. The
        # text itself survives the round trip, which is what proves it stayed data.
        self.assertEqual(frame.count("\n\n"), 1)
        self.assertTrue(frame.startswith("data: "))
        self.assertEqual(json.loads(frame[len("data: "):-2])["text"], hostile["text"])

    def test_a_subscriber_that_goes_away_does_not_break_the_emit(self):
        hub = EventHub()
        subscriber = hub.subscribe()
        hub.unsubscribe(subscriber)
        # The emitter is the workflow thread; a dead subscriber must never reach it.
        BridgeBus(HubTransport(hub)).dispatch(
            {"type": "log", "agent": "a", "log_type": "x", "text": ""}
        )


if __name__ == "__main__":
    unittest.main()
