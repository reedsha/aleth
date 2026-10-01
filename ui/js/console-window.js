// ui/js/console-window.js — the detached console's own document.
//
// This window is a browser like any other: it is served by the API gateway, so it shares the
// main window's origin and reaches the API with plain same-origin requests. That is the whole
// point of the page existing — the previous version was an inline `html=` string handed to
// pywebview, which gave it an opaque origin where `fetch` and `EventSource` are refused, and
// its lines had to be injected through `evaluate_js`.
//
// Two sources, in this order: the backlog the dock had when it detached (the backend holds it,
// because this window cannot read another window's memory), then the live stream.

import { api, connectEventStream } from "./api-client.js";

const MAX_LINES = 400;

const stream = document.getElementById("stream");
const status = document.getElementById("status");
let lineCount = 0;

/** The class suffix for a line's kind, restricted to what belongs in a class name. */
function kindClass(kind) {
  const cleaned = String(kind || "log").toLowerCase().replace(/[^a-z0-9_-]/g, "");
  return cleaned || "log";
}

function appendLine(kind, text) {
  if (!stream) return;
  const content = String(text);
  if (!content) return;
  const row = document.createElement("div");
  row.className = `ln ${kindClass(kind)}`;
  const span = document.createElement("span");
  span.className = "t";
  // `textContent`, never `innerHTML`: a transcript line is agent output, and agent output is
  // not markup. This window renders no HTML at all.
  span.textContent = content;
  row.appendChild(span);
  stream.appendChild(row);

  lineCount += 1;
  while (lineCount > MAX_LINES && stream.firstChild) {
    stream.removeChild(stream.firstChild);
    lineCount -= 1;
  }
  window.scrollTo(0, document.body.scrollHeight);
}

/**
 * Accepts one stream frame.
 *
 * The main window's sink validates against the full event vocabulary; this page only renders
 * `console_line`, so it checks the shape it consumes and ignores everything else. An unknown
 * key is refused rather than ignored, for the same reason the main contract is `extra="forbid"`.
 */
function acceptFrame(raw) {
  if (typeof raw !== "string") return;
  let event;
  try {
    event = JSON.parse(raw);
  } catch (_err) {
    return;
  }
  if (!event || event.type !== "console_line") return;
  const keys = Object.keys(event);
  if (keys.some((key) => key !== "type" && key !== "kind" && key !== "text")) return;
  if (typeof event.kind !== "string" || typeof event.text !== "string") return;
  appendLine(event.kind, event.text);
}

function setStatus(text) {
  if (status) status.textContent = text;
}

// Backlog first, then the stream: a line emitted in between is the one gap this ordering
// leaves, and it is the same gap the old buffered push had.
api.get_console_backlog().then((res) => {
  const lines = (res && res.lines) || [];
  lines.forEach((line) => appendLine(line && line.kind, line && line.text));
  setStatus("live");
  connectEventStream({
    onMessage: acceptFrame,
    onStatus: (state) => {
      if (state && state.connected) setStatus("live");
      else if (state && state.unsupported) setStatus("no stream support");
      else setStatus("reconnecting");
    },
  });
});
