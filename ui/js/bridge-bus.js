// ui/js/bridge-bus.js — The single, strict inbound event sink (window.__deepAgentsBus).
//
// Every payload the backend pushes arrives here, as a JSON *string*, through one static
// call: `window.__deepAgentsBus.receive(rawJson)`. This module parses it, validates it
// against the same event vocabulary the Python side enforces (`tools/payloads.py`), and
// only then hands it to the renderer. A payload that is not a JSON string, an unknown
// event type, or an undocumented key is rejected rather than dispatched -- the frontend
// half of the `extra="forbid"` contract.
//
// This is not the same thing as `ui/js/bus.js`, which is the frontend's *internal*
// pub/sub. This is the desktop bridge's inbound wire.
import { handleAgentEvent } from "./agent-events.js";

const isString = (v) => typeof v === "string";
const isNumber = (v) => typeof v === "number";
const isBoolean = (v) => typeof v === "boolean";
const isArray = (v) => Array.isArray(v);
const isObject = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const isStringOrNull = (v) => v === null || typeof v === "string";
const isObjectOrNull = (v) => v === null || isObject(v);

// The `summary` object's keys, mirroring tools/payloads.SummaryPayload. Only the keys the
// Python model declares are accepted; an unknown one rejects the event.
const SUMMARY_KEYS = {
  title: isString,
  status: isString,
  files: isArray,
  deliverables: isArray,
  proposals: isArray,
  metrics: isObjectOrNull,
  root_cause: isString,
  fix_spec: isString,
};

function isSummary(v) {
  if (!isObject(v)) return false;
  return Object.keys(v).every(
    (key) => typeof SUMMARY_KEYS[key] === "function" && SUMMARY_KEYS[key](v[key])
  );
}

// type -> { key: validator }. Mirrors tools/payloads.BUS_EVENT_TYPES; `type` itself is
// implicit. Keep this in step with the Python models -- a drift is caught by
// tests/test_payloads.py on the Python side and by tests/ui on this side.
const SCHEMAS = {
  workflow_started: { plan_file: isString, action_type: isString },
  architect_spawn: { agent: isString, name: isString, model: isString },
  coder_spawn: { agent: isString, name: isString, model: isString },
  delegation: {
    from_agent: isString,
    target_agent: isString,
    target_name: isString,
    task: isStringOrNull,
  },
  architect_summary: { agent: isString, summary: isSummary },
  coder_summary: { agent: isString, summary: isSummary },
  plan_updated: { filename: isString, content: isString, tree: isArray, plan_json: isObject },
  workflow_complete: { status: isString, message: isString },
  workflow_stopped: { status: isString, message: isString },
  agent_error: { agent: isString, error: isString },
  agents_updated: { agents: isObject },
  workspace_changed: { workspace_dir: isString, files: isArray, plans: isArray },
  laya_tagging_started: {},
  laya_tagging_progress: { done: isNumber, total: isNumber, title: isString },
  laya_tagging_done: { success: isBoolean, changed: isArray, engine: isString, error: isString },
  console_detached: {},
  console_detach_failed: { error: isString },
  console_line: { kind: isString, text: isString },
  task_state_updated: { plan_id: isString, task_id: isString, status: isString },
  artifact_planned: { artifact: isObject },
  artifact_approved: { plan_id: isString, task_id: isString },
  intent_failed: {
    intent_id: isString,
    action_type: isString,
    error: isString,
  },
  // The live run's telemetry (Phase 33): the loop's own events, each naming its intent so a
  // per-intent stream can filter to one run. Mirrors tools/payloads.py.
  agent_thought: { intent_id: isString, step: isNumber, text: isString, tool_calls: isArray },
  tool_execution_start: { intent_id: isString, tool: isString, arguments: isObject },
  tool_execution_complete: { intent_id: isString, tool: isString, result: isString },
  token_budget_update: {
    intent_id: isString,
    prompt_tokens: isNumber,
    completion_tokens: isNumber,
    spent: isNumber,
    limit: isNumber,
  },
  intent_paused: { intent_id: isString, message: isString },
  intent_steered: { intent_id: isString, correction: isString },
  intent_resumed: { intent_id: isString, corrections: isNumber },
  log: { agent: isString, log_type: isString, text: isString },
  tool_call: { agent: isString, tool: isString, args: isObject, description: isString },
  tool_result: { agent: isString, tool: isString, result: isString },
};

/**
 * Returns "" when `event` satisfies the contract, or a human-readable reason it does not.
 *
 * The rules are the frontend mirror of `extra="forbid"`: the object must carry a known
 * `type`, every key it carries must be one the schema declares, and each present key must
 * have the declared type. A *missing* key is allowed (the backend models default theirs).
 */
export function validateEvent(event) {
  if (!isObject(event) || !isString(event.type)) {
    return "payload is not an object with a string type";
  }
  const schema = SCHEMAS[event.type];
  if (!schema) return `unknown event type "${event.type}"`;
  for (const key of Object.keys(event)) {
    if (key === "type") continue;
    const check = schema[key];
    if (!check) return `undocumented key "${key}" on "${event.type}"`;
    if (!check(event[key])) return `key "${key}" on "${event.type}" has the wrong type`;
  }
  return "";
}

/** Parses, validates and dispatches one inbound payload. Rejects anything malformed. */
function receive(rawJson) {
  if (typeof rawJson !== "string") {
    console.error("[deepAgentsBus] rejected a payload that is not a JSON string");
    return;
  }
  let event;
  try {
    event = JSON.parse(rawJson);
  } catch (_err) {
    console.error("[deepAgentsBus] rejected a payload that is not valid JSON");
    return;
  }
  const problem = validateEvent(event);
  if (problem) {
    console.error(`[deepAgentsBus] rejected an event: ${problem}`);
    return;
  }
  handleAgentEvent(event);
}

/** Installs the single inbound sink. Idempotent, and safe to call before any event. */
export function installBus() {
  window.__deepAgentsBus = { receive };
  return window.__deepAgentsBus;
}
