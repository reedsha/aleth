// ui/js/api-client.js — The frontend's typed client for the local API gateway.
//
// This replaces `window.pywebview.api`. Every backend capability the UI has is one HTTP call to
// the loopback gateway, and this module is the only place that knows the mapping. Two reasons it
// is a table rather than a call per site:
//
//   * the page is served *by* the gateway, so every path here is same-origin — no base URL to
//     discover, no CORS, and no chance of talking to some other server on loopback;
//   * the mapping is the client half of `api/operations.py`. The server refuses anything not in
//     its table; this refuses anything not in this one, so a typo fails at the call site.
//
// The envelope is uniform: `{ok, data, error}`. On success `data` is returned unchanged — the
// same dictionaries the pywebview bridge used to hand back, so a migrated call site keeps its
// own logic (`res.success === false`, `res.variables`, …) and only the transport changes.
//
// Nothing here touches `window` at module scope: the Playwright harness imports this table into
// Node to route its fetch mock, and a module that reads the DOM on import could not be imported
// there.

/** name -> { verb, path, args }. `args` names the positional parameters in order. */
export const OPERATIONS = {
  // -- reads -------------------------------------------------------------------
  get_agents: { verb: "GET", path: "/api/agents", args: [] },
  get_workspace_info: { verb: "GET", path: "/api/workspace", args: [] },
  get_active_plan: { verb: "GET", path: "/api/plan/document", args: [] },
  get_plan_files: { verb: "GET", path: "/api/plan/files", args: [] },
  get_run_state: { verb: "GET", path: "/api/run", args: [] },
  get_environment_variables: { verb: "GET", path: "/api/env", args: [] },
  get_settings: { verb: "GET", path: "/api/settings", args: [] },
  validate_plan_structure: { verb: "GET", path: "/api/plan/structure", args: [] },
  audit_codebase_sync: { verb: "GET", path: "/api/audit", args: [] },
  get_task_diff: { verb: "GET", path: "/api/task/diff", args: ["task_id"] },
  get_preview_source: { verb: "GET", path: "/api/preview", args: ["filename"] },
  extract_plan_steps: { verb: "GET", path: "/api/plan/steps", args: ["filename"] },
  get_source_span: { verb: "GET", path: "/api/artifact/span", args: ["file_path", "start", "end"] },
  // -- mutations ---------------------------------------------------------------
  save_system_prompt: { verb: "POST", path: "/api/agent/prompt", args: ["agent_id", "new_prompt", "is_custom_only"] },
  select_workspace: { verb: "POST", path: "/api/workspace/select", args: [] },
  add_plan_task: { verb: "POST", path: "/api/plan/task", args: ["title"] },
  revert_plan_update: { verb: "POST", path: "/api/plan/revert", args: [] },
  retag_plan_with_laya: { verb: "POST", path: "/api/plan/retag", args: [] },
  set_active_plan: { verb: "POST", path: "/api/plan/active", args: ["filename"] },
  normalize_plan: { verb: "POST", path: "/api/plan/normalize", args: [] },
  create_plan_file: { verb: "POST", path: "/api/plan/create", args: ["filename", "project_idea"] },
  stop_execution: { verb: "POST", path: "/api/run/stop", args: [] },
  retry_execution: { verb: "POST", path: "/api/run/retry", args: ["agent_id", "user_message"] },
  resolve_sync: { verb: "POST", path: "/api/plan/sync", args: ["resolution_type"] },
  rollback_task: { verb: "POST", path: "/api/task/rollback", args: ["task_id"] },
  run_task_tests: { verb: "POST", path: "/api/task/tests", args: ["task_id"] },
  save_settings: { verb: "POST", path: "/api/settings", args: ["values"] },
  approve_artifact: { verb: "POST", path: "/api/artifact/approve", args: ["task_id", "plan_id"] },
  reject_artifact: { verb: "POST", path: "/api/artifact/reject", args: ["task_id", "feedback", "plan_id"] },
  add_task_dependency: { verb: "POST", path: "/api/plan/dependency", args: ["parent_id", "child_id", "plan_id"] },
  update_artifact_target: { verb: "POST", path: "/api/artifact/target", args: ["task_id", "index", "content", "plan_id"] },
  open_console_window: { verb: "POST", path: "/api/console/open", args: ["backlog"] },
  push_console_line: { verb: "POST", path: "/api/console/line", args: ["kind", "text"] },
  ui_ready: { verb: "POST", path: "/api/ui/ready", args: [] },
  // The one intent. It is not an operation in `api/operations.py` because it is queued rather
  // than executed: the orchestrator drains it, and the run's progress arrives on the stream.
  start_execution: { verb: "POST", path: "/api/intent/execute", args: ["message", "action_type", "action_params"] },
};

export const EVENTS_PATH = "/api/events";

/** The transport failure shape, matching what the pywebview bridge's callers already handle. */
function transportFailure(message) {
  return { success: false, error: String(message || "the request failed") };
}

/**
 * Calls one operation and returns its `data`, or a `{success:false, error}` on any failure.
 *
 * Returning a value rather than throwing is deliberate: every migrated call site already reads
 * `res.success === false` for a refusal, and a rejected promise would change the shape of the
 * contract the UI was written against. A genuine transport failure (server down, gateway
 * stopped) is reported the same way, because "the backend said no" and "the backend did not
 * answer" are both "no" to a caller that cannot act on the difference.
 */
export async function callOperation(name, args = []) {
  const operation = OPERATIONS[name];
  if (!operation) return transportFailure(`unknown operation ${name}`);
  const payload = {};
  operation.args.forEach((key, index) => {
    if (index < args.length && args[index] !== undefined) payload[key] = args[index];
  });

  let response;
  try {
    if (operation.verb === "GET") {
      const query = new URLSearchParams();
      Object.entries(payload).forEach(([key, value]) => {
        if (value !== null && value !== "") query.set(key, String(value));
      });
      const suffix = query.toString() ? `?${query}` : "";
      response = await fetch(`${operation.path}${suffix}`, { headers: { Accept: "application/json" } });
    } else {
      response = await fetch(operation.path, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(payload),
      });
    }
  } catch (err) {
    return transportFailure((err && err.message) || err);
  }

  let envelope;
  try {
    envelope = await response.json();
  } catch (_err) {
    return transportFailure(`the gateway answered with non-JSON (${response.status})`);
  }
  if (!envelope || envelope.ok !== true) {
    return transportFailure((envelope && (envelope.detail || envelope.error)) || `HTTP ${response.status}`);
  }
  return envelope.data;
}

/**
 * The client, one method per operation: `api.get_environment_variables()`.
 *
 * Built from the table so a call site reads exactly as it did against the bridge — which is what
 * makes the migration a rename rather than a rewrite.
 */
export const api = Object.fromEntries(
  Object.keys(OPERATIONS).map((name) => [name, (...args) => callOperation(name, args)])
);

/**
 * Streams engine events from the gateway, reconnecting with exponential backoff.
 *
 * `EventSource` reconnects on its own, but at a fixed interval — a gateway that is restarting
 * would be hammered at a constant rate by every open window. This closes the browser's loop and
 * owns the schedule instead: 1s, 2s, 4s … capped at 30s, reset the moment a connection opens.
 *
 * `onMessage` receives the raw frame text, because the strict inbound contract already exists:
 * the caller feeds it to the same sink the pywebview push used
 * (`window.__deepAgentsBus.receive`), so validation stays in one place.
 */
export function connectEventStream({ onMessage, onStatus } = {}) {
  const BASE_MS = 1000;
  const MAX_MS = 30000;
  let attempt = 0;
  let source = null;
  let timer = null;
  let closed = false;

  function report(status) {
    if (typeof onStatus === "function") onStatus(status);
  }

  function schedule() {
    if (closed) return;
    const delay = Math.min(MAX_MS, BASE_MS * 2 ** attempt);
    attempt += 1;
    report({ connected: false, attempt, retry_in_ms: delay });
    timer = setTimeout(open, delay);
  }

  function open() {
    if (closed) return;
    if (typeof EventSource !== "function") {
      report({ connected: false, unsupported: true });
      return;
    }
    source = new EventSource(EVENTS_PATH);
    source.onopen = () => {
      attempt = 0;
      report({ connected: true, attempt: 0 });
    };
    source.onmessage = (event) => {
      if (typeof onMessage === "function") onMessage(event.data);
    };
    source.onerror = () => {
      // The browser would retry at its own fixed interval; take the schedule over.
      if (source) source.close();
      source = null;
      schedule();
    };
  }

  function close() {
    closed = true;
    if (timer) clearTimeout(timer);
    timer = null;
    if (source) source.close();
    source = null;
  }

  open();
  return { close };
}
