// Shared harness for the frontend's Playwright suite.
//
// Both spec files drive the *built* app against a mocked **API gateway**. There is no pywebview
// bridge any more: the page is served by the gateway, so it reaches the backend with plain
// same-origin `fetch` and `EventSource`, and those are the only two things this harness stands
// in for.
//
// The paths are taken from `ui/js/api-client.js` itself, so the mock cannot drift from the
// client's own table: an operation the client can call is an operation this harness answers.
//
// A fixture is written once (`options.api`, keyed by operation name) and served over HTTP.
// Unknown operations answer with an empty object rather than throwing, so a test never fails
// because the frontend asked for something this fixture does not model.

import { expect } from "@playwright/test";

import { OPERATIONS } from "../../ui/js/api-client.js";

// The client's table, flattened to the one thing the page-side mock needs: name -> path.
const OPERATION_PATHS = Object.fromEntries(
  Object.entries(OPERATIONS).map(([name, operation]) => [name, operation.path])
);

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

export const AGENTS = {
  main_agents: [
    {
      id: "software-architect",
      name: "software-architect",
      display_name: "Lead Software Architect",
      type: "main",
      role: "Coordinator",
      model: "openai:policy/architect",
      system_prompt: "Lead Architect: planner, verifier and delegator.",
      file_path: "agents/architect.py",
      tools: ["read_file", "write_file"],
    },
  ],
  coder_agents: [
    {
      id: "coder-deep",
      name: "coder-deep",
      display_name: "Senior Backend Coder",
      type: "coder",
      role: "Sub-Agent",
      model: "openai:policy/coder-deep",
      description: "Core algorithms and complex backend logic.",
      system_prompt: "Senior Backend Coder with full shell access.",
      file_path: "agents/coders.py",
      tools: ["read_file", "write_file", "execute_shell_command"],
    },
  ],
};

export const WORKSPACE = {
  workspace_dir: "C:/projects/demo",
  files: [
    { name: "main.py", path: "main.py", size: 120, vcs: "" },
    { name: "tests", path: "tests", size: 0, vcs: "" },
  ],
};

export const PLAN = {
  filename: "PLAN.md",
  exists: true,
  content: "# Project Plan: Demo\n",
  plans: ["PLAN.md"],
  load_error: "",
  tree: [
    { id: "task-1", title: "Scaffold the service", status: "completed" },
    { id: "task-2", title: "Build the endpoint", status: "pending" },
    { id: "task-3", title: "Write the tests", status: "failed" },
  ],
  plan_json: {
    version: "1.0",
    plan_file: "PLAN.md",
    title: "Demo Roadmap",
    state_summary: { title: "Global State Summary", bullets: ["One settled fact."] },
    sections: [
      {
        id: "sec-1",
        title: "1. Foundation",
        tasks: [
          {
            id: "task-1",
            section: "1. Foundation",
            title: "Scaffold the service",
            status: "completed",
            tag: "BE",
            details: ["Created: main.py"],
            files: ["main.py"],
            behavioral_log: ["wrote the module"],
            sub_steps: [
              { id: "task-1-sub-1", title: "Add the entry point", status: "completed", details: [] },
            ],
          },
          {
            id: "task-2",
            section: "1. Foundation",
            title: "Build the endpoint",
            status: "pending",
            tag: "API",
            details: [],
            files: [],
            sub_steps: [],
          },
        ],
      },
      {
        id: "sec-2",
        title: "2. Quality",
        tasks: [
          {
            id: "task-3",
            section: "2. Quality",
            title: "Write the tests",
            status: "failed",
            tag: "TEST",
            details: ["Verification failed: the suite did not pass"],
            files: [],
            sub_steps: [],
          },
        ],
      },
    ],
  },
};

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------

/**
 * Opens the built app with a mocked desktop bridge.
 *
 * See the module header for why the mock is shaped the way it is. `options` overrides any
 * of the three fixture payloads; `waitForTree: false` skips the plan-tree readiness gate
 * for a test that deliberately loads an empty plan.
 *
 * `options.api` adds operations beyond the ones the fixture models: each entry is an operation
 * name mapped to the (serialisable) value the gateway answers with, e.g.
 * `{ api: { get_preview_source: { success: true, found: true, content: "<html>…" } } }`.
 * The values are serialised into the page, so they must be plain data, not functions.
 */
export async function openApp(page, options = {}) {
  const {
    waitForTree = true,
    api: apiOverrides = {},
    gatewayDown = false,
    ...overrides
  } = options;
  // The three fixtures the app cannot start without are served by default, over whichever
  // transport asks for them. A test's own `agents`/`workspace`/`plan` override wins, and
  // `options.api` then overrides or adds individual operations.
  const agents = overrides.agents === undefined ? AGENTS : overrides.agents;
  const workspace = overrides.workspace === undefined ? WORKSPACE : overrides.workspace;
  const plan = overrides.plan === undefined ? PLAN : overrides.plan;
  const apiReturns = {
    get_agents: agents,
    get_workspace_info: workspace,
    get_active_plan: plan,
    get_run_state: { running: false },
    ...apiOverrides,
  };
  const payload = {
    agents,
    workspace,
    plan,
    ...overrides,
    apiReturns,
    gatewayDown: !!gatewayDown,
    operationPaths: OPERATION_PATHS,
  };

  await page.addInitScript((data) => {
    const jsonResponse = (body, status = 200) => ({
      ok: status >= 200 && status < 300,
      status,
      headers: { get: () => "application/json" },
      json: async () => body,
      text: async () => JSON.stringify(body),
    });

    // -- the gateway -----------------------------------------------------------
    // One fixture, one transport: a name in `apiReturns` is what the gateway answers with. The
    // path comes from the client's own table, so a route the client can address is a route this
    // mock answers. `gatewayDown` makes every request reject, which is what an engine that is not
    // running looks like to `fetch`.
    window.fetch = async (input, init) => {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      const path = url.split("?")[0];
      // Every request is recorded -- its path *and* its body -- so a test can assert *what* a
      // module asked the engine for, not merely which route it used.
      window.__alethFetchLog.push(path);
      if (init && typeof init.body === "string") {
        try {
          window.__alethFetchBodies.push(JSON.parse(init.body));
        } catch (_err) {
          window.__alethFetchBodies.push(null);
        }
      }
      if (data.gatewayDown) throw new TypeError("Failed to fetch");
      const name = Object.keys(data.operationPaths).find(
        (key) => data.operationPaths[key] === path
      );
      if (!name) return jsonResponse({ ok: false, error: "no such endpoint" }, 404);
      return jsonResponse({ ok: true, data: name in data.apiReturns ? data.apiReturns[name] : {} });
    };
    window.__alethFetchLog = [];
    window.__alethFetchBodies = [];
    // The same object the gateway mock reads, exposed so a test can change what an operation
    // answers *mid-run* -- e.g. a run that paused while the window was disconnected. It is a
    // reference, not a copy, so mutating it here changes what the next request is told.
    window.__alethApiReturns = data.apiReturns;

    // The stream. Tests drive it with `window.__alethStream`: `drop()` models the backend dying
    // (every attempt fails, as it would against a dead port) and `open()` models it coming back.
    // `__alethEmit(rawJson)` pushes a frame, which is exactly the string the backend sends, so
    // the strict parse/validate path is exercised.
    let down = false;
    const streams = [];
    window.__alethStreams = streams;
    window.EventSource = class {
      constructor(url) {
        this.url = url;
        this.closed = false;
        streams.push(this);
        setTimeout(() => {
          if (this.closed) return;
          if (down) {
            if (this.onerror) this.onerror({});
          } else if (this.onopen) {
            this.onopen({});
          }
        }, 0);
      }

      close() {
        this.closed = true;
      }
    };
    window.__alethEmit = (raw) => {
      streams.forEach((stream) => {
        if (!stream.closed && stream.onmessage) stream.onmessage({ data: raw });
      });
    };
    // Drives the stream's own lifecycle, so a test can simulate the backend dying and coming
    // back the way `EventSource` reports it: an error, then a successful reopen.
    window.__alethStream = {
      drop: () => {
        down = true;
        streams.forEach((stream) => {
          if (!stream.closed && stream.onerror) stream.onerror({});
        });
      },
      open: () => {
        down = false;
        streams.forEach((stream) => {
          if (stream.onopen) stream.onopen({});
        });
      },
    };
  }, payload);

  await page.goto("/index.html");
  // `waitForTree: false` means the caller is asserting something else (a fatal screen, an empty
  // plan) and will do its own waiting -- this must not fail the boot on its behalf.
  if (waitForTree) {
    await expect(page.locator("#planTreeContainer .plan-tree-section").first()).toBeVisible();
  }
}

/**
 * Feeds one event through the real inbound wire, exactly as the backend's bus push would:
 * `window.__deepAgentsBus.receive` is the single sink `ui/js/bootstrap.js` installs on the
 * handshake, and it takes the same JSON *string* the backend sends, so this exercises the
 * strict parse/validate path and everything downstream of it.
 */
export function dispatchAgentEvent(page, event) {
  return page.evaluate((raw) => window.__deepAgentsBus.receive(raw), JSON.stringify(event));
}

/** Uncaught exceptions, which are what a half-initialised window looks like. */
export function collectPageErrors(page) {
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  return errors;
}
