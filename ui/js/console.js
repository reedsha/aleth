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

const CONSOLE_MAX_LINES = 400;

let consoleLineCount = 0;

function consoleClock() {
  const now = new Date();
  const two = (n) => String(n).padStart(2, "0");
  return `${two(now.getHours())}:${two(now.getMinutes())}:${two(now.getSeconds())}`;
}

function appendConsoleLine(kind, text) {
  const stream = DOM.consoleStream;
  if (!stream || text === null || text === undefined || text === "") return;

  const line = document.createElement("div");
  line.className = `console-line console-${kind}`;
  line.innerHTML = `<span class="console-time">${consoleClock()}</span>` +
    `<span class="console-text">${escapeHtml(String(text))}</span>`;
  stream.appendChild(line);

  consoleLineCount += 1;
  while (consoleLineCount > CONSOLE_MAX_LINES && stream.firstChild) {
    stream.removeChild(stream.firstChild);
    consoleLineCount -= 1;
  }

  // The console follows the tail like `tail -f`; the dock is resizable, so it stays put
  // by scrolling its own stream rather than the page.
  stream.scrollTop = stream.scrollHeight;
}

function clearConsole() {
  if (DOM.consoleStream) DOM.consoleStream.innerHTML = "";
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

// One line per event, chosen so the transcript reads like a shell session: commands that
// started a phase, output the agents produced, and the exit line that closed it.
function echoAgentEvent(event) {
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
      appendConsoleLine("delegate", `delegate -> ${event.target_name || event.target_agent || "coder"}`);
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
  }
}

function initTerminalConsole() {
  clearConsole();
  appendConsoleLine("state", "console ready");
}
