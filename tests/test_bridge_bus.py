"""The typed IPC bus: validation at the boundary and inert delivery to one sink.

Two properties are pinned here:

* :meth:`bridge_bus.BridgeBus.dispatch` validates the payload against the strict event
  union *before* it is delivered -- an unknown type or an undocumented key raises, and
  nothing reaches the sink; and
* the sink call is one static expression whose only variable part is a JSON *string*
  literal, so a hostile payload stays data and cannot break out of the call.

    .\\venv\\Scripts\\python.exe -m pytest tests/test_bridge_bus.py -n0 -q
"""

import json
import unittest

from pydantic import ValidationError

import bridge_bus
from bridge_bus import BridgeBus, WebviewTransport

_SINK_PREFIX = "window.__deepAgentsBus && window.__deepAgentsBus.receive("


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

    def test_a_rejected_event_never_reaches_the_sink(self):
        recorder = _Recorder()
        with self.assertRaises(ValidationError):
            BridgeBus(recorder).dispatch({"type": "workflow_complete", "surprise": True})
        self.assertEqual(recorder.sent, [])

    def test_the_detached_console_line_is_a_first_class_bus_event(self):
        recorder = _Recorder()
        BridgeBus(recorder).dispatch({"type": "console_line", "kind": "tool", "text": "x"})
        self.assertEqual(json.loads(recorder.sent[0])["type"], "console_line")


class SinkScriptTests(unittest.TestCase):
    def test_the_sink_is_one_static_call_with_a_string_literal_argument(self):
        script = bridge_bus.sink_script('{"type":"log"}')
        self.assertTrue(script.startswith(_SINK_PREFIX))
        self.assertTrue(script.endswith(");"))
        argument = script[len(_SINK_PREFIX):-2]
        # A JS *string* literal, not an object literal: it opens with a quote, and parsing
        # it yields the JSON text back.
        self.assertTrue(argument.startswith('"') and argument.endswith('"'))
        self.assertEqual(json.loads(argument), '{"type":"log"}')

    def test_a_hostile_payload_cannot_break_out_of_the_call(self):
        hostile = {
            "type": "log",
            "agent": "a",
            "log_type": "x",
            "text": '"); window.__pwned = 1; ("',
        }
        recorder = _Recorder()
        BridgeBus(recorder).dispatch(hostile)
        script = bridge_bus.sink_script(recorder.sent[0])

        # The hostile text stays inside one quoted literal, and there is exactly one sink
        # call in the statement -- so the payload can never terminate it and run code.
        self.assertEqual(script.count("window.__pwned"), 1)
        self.assertEqual(script.count("__deepAgentsBus.receive("), 1)
        self.assertTrue(script.endswith(");"))

        # It round-trips as data: the literal decodes to the JSON text, which decodes to the
        # original event with the text intact.
        argument = script[len(_SINK_PREFIX):-2]
        self.assertEqual(json.loads(json.loads(argument))["text"], hostile["text"])


class WebviewTransportTests(unittest.TestCase):
    def test_a_missing_window_makes_delivery_a_no_op(self):
        WebviewTransport(lambda: None).send("{}")  # must not raise

    def test_a_present_window_receives_the_static_sink_call(self):
        calls = []

        class _Window:
            def evaluate_js(self, code):
                calls.append(code)

        WebviewTransport(lambda: _Window()).send('{"type":"log"}')
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith(_SINK_PREFIX))


if __name__ == "__main__":
    unittest.main()
