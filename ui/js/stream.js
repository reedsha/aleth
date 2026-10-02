// ui/js/stream.js — the one server-sent-events connection, and which path it is on.
//
// The engine announces state on two streams:
//
//   * the firehose (`/api/events`) — every event, whoever it belongs to;
//   * a per-run stream (`/api/intents/<id>/stream`) — one run's own events plus its untagged
//     lifecycle events, filtered by the server (`api/server.py::_frame_for_intent`).
//
// The UI follows the per-run stream while a run is live and returns to the firehose when it ends.
// There is exactly **one** connection at a time, and this module owns which path it is on:
// re-targeting the existing connection rather than opening a second is what keeps a shared frame
// from being delivered twice.
//
// Reconnection is the client's own schedule — exponential backoff, capped — because
// `EventSource`'s built-in retry is a fixed interval that would hammer a restarting gateway. Every
// successful open calls the registered hydration hook, so a window that was disconnected (a laptop
// waking, a reload) reconciles against the durable ledger instead of trusting what it last saw.

import { connectEventStream, EVENTS_PATH, intentStreamPath } from "./api-client.js";
import { CONNECTED, LOST, RECONNECTING, setConnectionState } from "./connection.js";

let handle = null;
let path = EVENTS_PATH;
let onOpen = null;

/**
 * Opens the stream (once) and binds its health to the connection indicator.
 *
 * `onOpen` is called on every successful (re)connect — the hydration hook. It must be idempotent:
 * it runs at boot as well as after a drop, and its whole job is to reconcile the UI with state the
 * stream cannot replay.
 */
export function startStream({ onMessage, onOpen: hydrate } = {}) {
  onOpen = typeof hydrate === "function" ? hydrate : null;
  handle = connectEventStream({
    path,
    onMessage,
    onStatus: (status) => {
      if (!status) return;
      if (status.unsupported) {
        // No `EventSource` at all: the UI has no way to be told anything, so it must not pretend
        // it can act.
        setConnectionState(LOST);
        return;
      }
      setConnectionState(status.connected ? CONNECTED : RECONNECTING);
      if (status.connected && onOpen) onOpen();
    },
  });
  return handle;
}

/** Points the stream at one run's events. `""`/`null` returns it to the firehose. */
export function followIntent(intentId) {
  const next = intentId ? intentStreamPath(intentId) : EVENTS_PATH;
  if (next === path) return;
  path = next;
  if (handle) handle.setPath(next);
}

export function followFirehose() {
  followIntent("");
}

/** The path the stream is on. Published in the app's diagnostics so a test can assert it. */
export function streamPath() {
  return path;
}
