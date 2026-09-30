// Shared harness for the frontend's Playwright suite.
//
// Both spec files drive the *built* app through the same mocked desktop bridge. The mock
// mirrors the real pywebview handshake: pywebview defines `window.pywebview.api` and then
// fires `pywebviewready` *after* the document has loaded, which is when the app's own
// bootstrap listens for it. Unknown methods answer with an empty object rather than
// throwing, so a test never fails because the frontend asked for something this fixture
// does not model.

import { expect } from "@playwright/test";

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
 * `options.api` adds bridge methods beyond the four the fixture models: each entry is a
 * method name mapped to the (serialisable) value its `async` stub resolves to, e.g.
 * `{ api: { get_preview_source: { success: true, found: true, content: "<html>…" } } }`.
 * The values are serialised into the page, so they must be plain data, not functions.
 */
export async function openApp(page, options = {}) {
  const { waitForTree = true, api: apiReturns = {}, ...overrides } = options;
  const payload = {
    agents: AGENTS,
    workspace: WORKSPACE,
    plan: PLAN,
    ...overrides,
    apiReturns,
  };

  await page.addInitScript((data) => {
    const api = {
      get_agents: async () => data.agents,
      get_workspace_info: async () => data.workspace,
      get_active_plan: async () => data.plan,
      get_run_state: async () => ({ running: false }),
    };
    Object.keys(data.apiReturns).forEach((name) => {
      api[name] = async () => data.apiReturns[name];
    });
    window.pywebview = {
      api: new Proxy(api, {
        get: (target, name) => (name in target ? target[name] : async () => ({})),
      }),
    };
  }, payload);

  await page.goto("/index.html");
  await page.evaluate(() => window.dispatchEvent(new CustomEvent("pywebviewready")));
  if (waitForTree) {
    await expect(page.locator("#planTreeContainer .plan-tree-section").first()).toBeVisible();
  } else {
    await expect(page.locator("#planTreeContainer")).not.toBeEmpty();
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
