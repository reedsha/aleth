// ui/js/console.js — The dock's live console: a terminal-style echo of the agent stream.
//
// The agent cards in the centre stage render a run richly, but only while they are open.
// The console keeps a plain, append-only transcript in the dock, so a run can be followed
// (or read back afterwards) in the pane an IDE user already watches. It reads the same
// event funnel the cards do and never writes back into it, so a failure here costs the
// transcript and nothing else.
//
// The transcript is bounded: one node per echoed block would otherwise accumulate for the
// life of the window. Once the cap is reached the oldest lines are dropped from the top.

import { api } from "./api-client.js";
import { emit } from "./bus.js";
import { isDockDrawerOpen } from "./dock.js";
import { escapeHtml } from "./dom.js";
import { showToast } from "./notify.js";
import { setHtml, setText } from "./safe-dom.js";
import { DOM, setConsolePinned, state } from "./store.js";

const CONSOLE_MAX_LINES = 400;

// The share of the intent's token budget at which the live burn counter turns amber. The ceiling
// itself is the event's own `limit` (the engine's configured `MAX_INTENT_TOKENS`), so the UI never
// hard-codes a number the engine owns.
const BURN_WARN_RATIO = 0.8;

let consoleLineCount = 0;

// A parallel copy of the transcript, so the console can be detached into a second window
// with the history it already showed rather than starting blank. Bounded by the same cap
// as the DOM and trimmed in step with it.
const consoleLines = [];

// Whether the transcript is mirrored into the detached window. Cleared when that window is
// closed or refuses a push, so the mirror stops rather than logging into nothing forever.
let consoleDetached = false;

function consoleClock() {
  const now = new Date();
  const two = (n) => String(n).padStart(2, "0");
  return `${two(now.getHours())}:${two(now.getMinutes())}:${two(now.getSeconds())}`;
}

function appendConsoleLine(kind, text) {
  const stream = DOM.consoleStream;
  if (!stream || text === null || text === undefined || text === "") return;

  const content = String(text);
  const line = document.createElement("div");
  line.className = `console-line console-${kind}`;
  setHtml(line, `<span class="console-time">${consoleClock()}</span>` +
    `<span class="console-text">${escapeHtml(content)}</span>`);
  stream.appendChild(line);
  consoleLines.push({ kind, text: content });

  consoleLineCount += 1;
  while (consoleLineCount > CONSOLE_MAX_LINES && stream.firstChild) {
    stream.removeChild(stream.firstChild);
    consoleLines.shift();
    consoleLineCount -= 1;
  }

  // The console follows the tail like `tail -f`; the dock is resizable, so it stays put
  // by scrolling its own stream rather than the page.
  stream.scrollTop = stream.scrollHeight;

  pushDetachedConsoleLine(kind, content);
}

export function clearConsole() {
  if (DOM.consoleStream) setText(DOM.consoleStream, "");
  consoleLines.length = 0;
  consoleLineCount = 0;
}

function consoleSummaryText(summary) {
  if (!summary) return "summary";
  const title = summary.title || "Summary";
  const deliverables = (summary.deliverables || []).length;
  if (!deliverables) return title;
  return `${title} (${deliverables} deliverable${deliverables === 1 ? "" : "s"})`;
}

function consoleToolText(event) {
  const args = event.args || {};
  const detail = args.filename || args.plan_file || args.command || "";
  const head = detail ? `${event.tool || "tool"} ${detail}` : (event.tool || "tool");
  return event.description ? `${head} — ${event.description}` : head;
}

// -- the live run's own telemetry (Phase 34) -----------------------------------------
//
// The agent loop streams its thoughts and tool executions as they happen. These are more granular
// than the `log`/`tool_call`/`tool_result` events the workflow branches emit, so they read as the
// model's own narration rather than the workflow's.

function consoleThoughtText(event) {
  const text = String(event.text || "").trim();
  const calls = Array.isArray(event.tool_calls) ? event.tool_calls.filter(Boolean) : [];
  const step = event.step || "?";
  if (text && calls.length) return `step ${step}: ${text} \u2192 ${calls.join(", ")}`;
  if (text) return `step ${step}: ${text}`;
  if (calls.length) return `step ${step}: calling ${calls.join(", ")}`;
  return `step ${step}: thinking`;
}

function consoleExecText(event) {
  const args = event.arguments || {};
  const detail = args.path || args.filename || args.command || "";
  return detail ? `${event.tool || "tool"} ${detail}` : (event.tool || "tool");
}

function consoleExecResult(event) {
  // A tool result can be up to 16 KB on the wire; the transcript shows the head of it and keeps
  // the window readable. The full result is the model's, not the operator's.
  const result = String(event.result || "").replace(/\s+/g, " ").trim();
  if (!result) return "done";
  return result.length > 200 ? `${result.slice(0, 200)}\u2026` : result;
}

function formatCount(value) {
  const number = Number(value) || 0;
  return String(number).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

// The live burn counter: tokens spent against the intent's budget, with a visual alert as the
// breaker's ceiling approaches. It is the one readout that turns "the agent is thinking" into "the
// agent is spending", which is the number that matters to the person paying for the run.
function renderTokenBurn(event) {
  const element = DOM.tokenBurn;
  if (!element) return;
  const spent = Number(event.spent) || 0;
  const limit = Number(event.limit) || 0;
  element.hidden = false;
  element.textContent = limit > 0
    ? `${formatCount(spent)} / ${formatCount(limit)} tok`
    : `${formatCount(spent)} tok`;
  const ratio = limit > 0 ? spent / limit : 0;
  element.classList.toggle("burn-warn", ratio >= BURN_WARN_RATIO && ratio < 1);
  element.classList.toggle("burn-over", ratio >= 1);
  element.title = limit > 0
    ? `This run has spent ${spent} of ${limit} tokens`
    : `This run has spent ${spent} tokens`;
}

export function clearTokenBurn() {
  const element = DOM.tokenBurn;
  if (!element) return;
  element.hidden = true;
  element.textContent = "";
  element.classList.remove("burn-warn", "burn-over");
}

// One line per event, chosen so the transcript reads like a shell session: commands that
// started a phase, output the agents produced, and the exit line that closed it.
export function echoAgentEvent(event) {
  if (!event || !event.type) return;

  switch (event.type) {
    case "workflow_started":
      appendConsoleLine("cmd", `run ${event.action_type || "custom"} · ${event.plan_file || state.activePlan || "plan"}`);
      break;
    case "architect_spawn":
      appendConsoleLine("agent", `architect online · ${event.name || "Lead Software Architect"}`);
      break;
    case "coder_spawn":
      appendConsoleLine("agent", `${event.name || event.agent || "coder"} online · ${event.model || "coder"}`);
      break;
    case "log":
      appendConsoleLine(event.log_type === "decision" ? "decision" : "log", event.text);
      break;
    case "tool_call":
      appendConsoleLine("tool", consoleToolText(event));
      break;
    case "tool_result":
      appendConsoleLine("ok", event.result || "done");
      break;
    case "delegation":
      // The delegated task text is the useful half of this event; showing only the target
      // left the transcript with a delegate line and no idea what was delegated (audit L2).
      appendConsoleLine("delegate", `delegate -> ${event.target_name || event.target_agent || "coder"}${event.task ? " \u00b7 " + event.task : ""}`);
      break;
    case "coder_summary":
    case "architect_summary":
      appendConsoleLine("summary", consoleSummaryText(event.summary));
      break;
    case "agent_error":
      appendConsoleLine("error", event.error || "agent error");
      break;
    case "workflow_complete":
    case "workflow_stopped":
      appendConsoleLine("cmd", `exit ${event.status || "done"}${event.message ? " · " + event.message : ""}`);
      break;
    case "plan_updated":
      appendConsoleLine("state", `plan updated · ${event.filename || state.activePlan || "plan"}`);
      break;
    case "workspace_changed":
      appendConsoleLine("state", `workspace -> ${event.workspace_dir || ""}`);
      break;
    case "agents_updated":
      appendConsoleLine("state", "agent registry updated");
      break;
    case "console_detached":
      appendConsoleLine("state", "console detached to a second window");
      showToast("Console detached to a second window", "success");
      break;
    case "console_detach_failed":
      consoleDetached = false;
      appendConsoleLine("error", `console detach failed: ${event.error || "unknown error"}`);
      showToast("Could not detach the console", "error");
      break;

    // The live run's own telemetry (Phase 34). `token_budget_update` carries no line of its own --
    // it would be one per step and say nothing the burn counter does not -- so it drives the
    // readout instead of the transcript.
    case "agent_thought":
      appendConsoleLine("think", consoleThoughtText(event));
      break;
    case "tool_execution_start":
      appendConsoleLine("tool", consoleExecText(event));
      break;
    case "tool_execution_complete":
      appendConsoleLine("ok", consoleExecResult(event));
      break;
    case "token_budget_update":
      renderTokenBurn(event);
      break;
    case "intent_paused":
      appendConsoleLine("pause", "run paused \u2014 awaiting your steering");
      break;
    case "intent_steered":
      appendConsoleLine("steer", `steering: ${event.correction || ""}`);
      break;
    case "intent_resumed":
      appendConsoleLine("cmd", `resumed with ${event.corrections || 0} correction(s)`);
      break;
  }
}

export function initTerminalConsole() {
  clearConsole();
  appendConsoleLine("state", "console ready");
  // The console starts collapsed (see the visibility section below), so the class is applied
  // from state at startup rather than being written into the markup alone.
  syncConsoleVisibility();

  // Ctrl + ` toggles the console, matching the Ctrl/Cmd+K the palette installed. `code` is
  // checked as well as `key`: on a layout where the backtick is a dead key, `key` can arrive
  // as "Dead" while `code` is still Backquote.
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && (event.code === "Backquote" || event.key === "`")) {
      event.preventDefault();
      toggleConsole();
    }
  });
}

// ----------------------------------------------------------------------------
// Dock visibility: the console is a run-scoped pane, not a permanent fixture
// ----------------------------------------------------------------------------
// The transcript used to occupy the dock's flexible remainder permanently, spending vertical
// space on an idle window to show a single "console ready" line. It is collapsed at idle now:
// it opens when a run starts, folds away when a run finishes cleanly, and *stays* up when an
// agent errored or a gate refused the instruction, because that transcript is the explanation
// the user has to read.
//
// Visibility is a class on the dock rather than a style on the pane, because the dock has to
// shrink to its head at the same time (see .bottom-dock.console-hidden in ui/css/dock.css).

// The toggle is a rail icon now, not the dock pill it used to be: the pane's open/closed state
// is carried by aria-expanded (the cyan tint in ui/css/sidebar-panels.css) and the title, so
// there is no ⤒/⤓ glyph to swap.
function syncConsoleToggle() {
  const button = DOM.btnConsoleToggle;
  if (!button) return;
  const open = !!state.consoleVisible;
  button.setAttribute("aria-expanded", open ? "true" : "false");
  button.title = open ? "Hide the console (Ctrl+`)" : "Show the console (Ctrl+`)";
}

// Re-applies `state.consoleVisible` to the dock. Separated from show/hide because hiding also
// has to unwind the overlay: an expanded overlay anchors an absolutely positioned pane above
// a dock that no longer has room for it, and collapsing it restores the expand glyph too.
function syncConsoleVisibility() {
  const dock = DOM.bottomDock;
  if (!dock) return;
  dock.classList.toggle("console-hidden", !state.consoleVisible);
  if (!state.consoleVisible && dock.classList.contains("console-overlay")) {
    toggleConsoleOverlay();
  }
  syncConsoleToggle();
}

export function showConsole() {
  if (state.consoleVisible) return;
  state.consoleVisible = true;
  syncConsoleVisibility();
}

export function hideConsole() {
  if (!state.consoleVisible) return;
  state.consoleVisible = false;
  syncConsoleVisibility();
}

export function toggleConsole() {
  if (state.consoleVisible) hideConsole(); else showConsole();
}

// Draws the eye to a console that revealed itself without the user asking. Removed on a timer
// so the highlight cannot outlive the moment (see .console-attention in ui/css/console.css).
function flagConsoleAttention() {
  const dock = DOM.bottomDock;
  if (!dock) return;
  dock.classList.add("console-attention");
  setTimeout(() => dock.classList.remove("console-attention"), 1800);
}

// An error or a refusal is the one case that has to outlive the run that produced it: the run
// ends a moment later, and the transcript is the diagnosis. Pinned, so the clean-completion
// path leaves it up, and the next run clears the pin.
export function revealConsoleForAttention() {
  setConsolePinned(true);
  showConsole();
  flagConsoleAttention();
}

// ----------------------------------------------------------------------------
// Drawer, transcript handoff and the detached window
// ----------------------------------------------------------------------------

function consoleBacklog() {
  return consoleLines.slice();
}

// Mirrors one line into the detached window. Fire-and-forget: a refused push means no window is
// open (or it just closed), so the mirror stops rather than emitting into nothing for the rest of
// the session. The refusal is deliberately not reported -- it is a normal end, not a fault.
function pushDetachedConsoleLine(kind, text) {
  if (!consoleDetached) return;
  api.push_console_line(kind, text).catch(() => {
    consoleDetached = false;
  });
}

// The console's slide-up overlay: expanded, the transcript leaves the dock's flow and
// covers the workbench (see .bottom-dock.console-overlay), so a long run can be read
// without first dragging the dock tall. Restoring drops it back into the dock pane.
export function toggleConsoleOverlay() {
  const dock = DOM.bottomDock;
  if (!dock) return;
  const expanded = !dock.classList.contains("console-overlay");
  // Expanding is a request to read the transcript, so the pane is brought back first when the
  // dock is collapsed. showConsole() cannot recurse: by the time it re-applies the classes
  // `state.consoleVisible` is already true, so this branch is skipped.
  if (expanded) showConsole();
  dock.classList.toggle("console-overlay", expanded);
  // The command drawer opens into the dock below the toolbar, so leaving one open under
  // the overlay would stack two unrelated panes on the same edge. Expanding folds it away.
  if (expanded && typeof isDockDrawerOpen === "function" && isDockDrawerOpen()) {
    emit("action:close-drawer");
  }
  const button = DOM.btnConsoleOverlay;
  if (button) {
    button.textContent = expanded ? "\u2921" : "\u2922";
    button.title = expanded
      ? "Restore the console to the dock"
      : "Expand the console over the workbench";
  }
  if (expanded) {
    const stream = DOM.consoleStream;
    if (stream) stream.scrollTop = stream.scrollHeight;
  }
}

// Opens the transcript in a real second native window, pointed at the gateway's console page.
// The backend answers with a console_detached or console_detach_failed event, so the mirror is
// only claimed once a window exists.
export function detachConsole() {
  consoleDetached = true;
  api.open_console_window(consoleBacklog());
  showToast("Opening the detached console...", "info");
}
