// Phase 34 — the live HITL dashboard: the stream rendered, the run steerable, the connection resilient.
//
// Three properties, one per mandate:
//
//   1. the agent loop's own telemetry -- thoughts, tool executions, token spend -- is *rendered*,
//      not dropped on the floor;
//   2. the operator can interrupt a live run and steer it, and the correction reaches the engine
//      on the resume operation;
//   3. a window that drops -- or reloads -- follows the run's own stream, reconciles against the
//      durable ledger, and never duplicates the transcript or loses the terminal event.

import { test, expect } from "@playwright/test";

import { dispatchAgentEvent, openApp } from "./harness.mjs";

const INTENT_ID = "i-live";

/** Starts a real run through the palette + drawer confirm, so `state.activeIntentId` is set. */
async function startRun(page) {
  await page.locator("#btnCommandPalette").click();
  await page.fill("#commandPaletteInput", "next step");
  await page.keyboard.press("Enter");
  await page.locator("#btnConfirmActionParam").click();
}

/** The console transcript's text. The dock console is the surface these events render on. */
function consoleText(page) {
  return page.locator("#consoleStream");
}

function fetchBodies(page) {
  return page.evaluate(() => window.__alethFetchBodies);
}

function streamPath(page) {
  return page.evaluate(() => window.Aleth.diagnostics().streamPath);
}

/** A budget event for the live burn counter. `limit` is the engine's own ceiling. */
function budget(spent, limit) {
  return {
    type: "token_budget_update",
    intent_id: INTENT_ID,
    prompt_tokens: spent,
    completion_tokens: 0,
    spent,
    limit,
  };
}

// ---------------------------------------------------------------------------
// 1. The live stream is rendered
// ---------------------------------------------------------------------------

test("a live run's thoughts and tool executions reach the console", async ({ page }) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  await openApp(page, { api: { start_execution: { success: true, intent_id: INTENT_ID } } });
  await startRun(page);
  await dispatchAgentEvent(page, { type: "workflow_started", action_type: "next_step", plan_file: "PLAN.md" });

  await dispatchAgentEvent(page, {
    type: "agent_thought",
    intent_id: INTENT_ID,
    step: 1,
    text: "Reading the endpoint before changing it",
    tool_calls: ["read_file"],
  });
  await expect(consoleText(page)).toContainText("Reading the endpoint before changing it");
  await expect(consoleText(page)).toContainText("read_file");

  await dispatchAgentEvent(page, {
    type: "tool_execution_start",
    intent_id: INTENT_ID,
    tool: "read_file",
    arguments: { path: "api.py" },
  });
  await expect(consoleText(page)).toContainText("read_file api.py");

  await dispatchAgentEvent(page, {
    type: "tool_execution_complete",
    intent_id: INTENT_ID,
    tool: "read_file",
    result: "def handler():\n    return 1\n",
  });
  await expect(consoleText(page)).toContainText("def handler()");

  expect(errors).toEqual([]);
});

test("the token burn counter tracks the intent budget and warns as the cap approaches", async ({ page }) => {
  await openApp(page, { api: { start_execution: { success: true, intent_id: INTENT_ID } } });
  await startRun(page);
  await dispatchAgentEvent(page, { type: "workflow_started", action_type: "next_step", plan_file: "PLAN.md" });

  const burn = page.locator("#tokenBurn");

  await dispatchAgentEvent(page, budget(100, 250000));
  await expect(burn).toBeVisible();
  await expect(burn).toHaveText("100 / 250,000 tok");
  await expect(burn).not.toHaveClass(/burn-warn/);

  // Past the warning ratio: the readout turns amber, because the breaker's ceiling is close.
  await dispatchAgentEvent(page, budget(200000, 250000));
  await expect(burn).toHaveClass(/burn-warn/);
  await expect(burn).not.toHaveClass(/burn-over/);

  // At the ceiling: red, because the next check trips the breaker.
  await dispatchAgentEvent(page, budget(250000, 250000));
  await expect(burn).toHaveClass(/burn-over/);
});

// ---------------------------------------------------------------------------
// 2. The run is steerable
// ---------------------------------------------------------------------------

test("a live run is followed on its own stream, and Interrupt asks the engine to hold it", async ({ page }) => {
  await openApp(page, { api: { start_execution: { success: true, intent_id: INTENT_ID } } });
  await startRun(page);

  // The window re-targeted the *one* connection to the run's own stream -- not a second stream,
  // which would deliver every shared frame twice.
  await expect.poll(() => streamPath(page)).toBe(`/api/intents/${INTENT_ID}/stream`);
  await expect(page.locator("#btnInterruptRun")).toBeVisible();

  await page.locator("#btnInterruptRun").click();

  await expect.poll(() => fetchBodies(page)).toContainEqual({ intent_id: INTENT_ID, note: "" });
  expect(await page.evaluate(() => window.__alethFetchLog)).toContain("/api/intent/interrupt");
});

test("intent_paused opens the steering overlay and the directive is sent on resume", async ({ page }) => {
  await openApp(page, { api: { start_execution: { success: true, intent_id: INTENT_ID } } });
  await startRun(page);
  await dispatchAgentEvent(page, { type: "workflow_started", action_type: "next_step", plan_file: "PLAN.md" });

  // Before the pause, no overlay.
  await expect(page.locator("#steerOverlay")).toBeHidden();

  await dispatchAgentEvent(page, { type: "intent_paused", intent_id: INTENT_ID, message: "hold" });

  await expect(page.locator("#steerOverlay")).toBeVisible();
  await expect(page.locator("#consolePausedBadge")).toBeVisible();
  await expect(page.locator("#bottomDock")).toHaveClass(/console-paused/);

  await page.fill("#steerInput", "Stop refactoring the CSS; fix the API endpoint only");
  await page.locator("#btnSteerSubmit").click();

  await expect.poll(() => fetchBodies(page)).toContainEqual({
    intent_id: INTENT_ID,
    correction: "Stop refactoring the CSS; fix the API endpoint only",
  });
  expect(await page.evaluate(() => window.__alethFetchLog)).toContain("/api/intent/resume");
  // The resume POST *is* the state change, so the hold clears on the answer.
  await expect(page.locator("#steerOverlay")).toBeHidden();
});

test("resume without changes releases the hold with an empty correction", async ({ page }) => {
  await openApp(page, { api: { start_execution: { success: true, intent_id: INTENT_ID } } });
  await startRun(page);
  await dispatchAgentEvent(page, { type: "intent_paused", intent_id: INTENT_ID, message: "hold" });
  await expect(page.locator("#steerOverlay")).toBeVisible();

  await page.locator("#btnSteerCancel").click();

  await expect.poll(() => fetchBodies(page)).toContainEqual({ intent_id: INTENT_ID, correction: "" });
  await expect(page.locator("#steerOverlay")).toBeHidden();
});

test("a refused resume keeps the hold on screen and says why", async ({ page }) => {
  await openApp(page, {
    api: {
      start_execution: { success: true, intent_id: INTENT_ID },
      resume_intent: { success: false, error: "the intent is not paused" },
    },
  });
  await startRun(page);
  await dispatchAgentEvent(page, { type: "intent_paused", intent_id: INTENT_ID, message: "hold" });
  await expect(page.locator("#steerOverlay")).toBeVisible();

  await page.fill("#steerInput", "do something else");
  await page.locator("#btnSteerSubmit").click();

  // The hold did not lift, and the UI did not pretend it had.
  await expect(page.locator("#steerOverlay")).toBeVisible();
  await expect(page.locator("#toastContainer")).toContainText("Could not resume the run");
});

test("a held run offers its snapshots and the rewind reaches the engine", async ({ page }) => {
  await openApp(page, {
    api: {
      start_execution: { success: true, intent_id: INTENT_ID },
      get_intent_steps: {
        success: true,
        intent_id: INTENT_ID,
        steps: [
          { step: 0, sha: "a", message: "aleth: the workspace as staged" },
          { step: 1, sha: "b", message: "aleth: step 1: write_file" },
        ],
      },
      rollback_intent: { success: true, intent_id: INTENT_ID, step: 1 },
    },
  });
  await startRun(page);
  await dispatchAgentEvent(page, { type: "intent_paused", intent_id: INTENT_ID, message: "hold" });

  const select = page.locator("#steerStepSelect");
  await expect(select).toBeVisible();
  await expect(select.locator("option")).toHaveCount(2);
  // Newest first, so the step before the damage is the one already chosen.
  await expect(select).toHaveValue("1");

  await page.locator("#btnSteerRollback").click();

  await expect.poll(() => fetchBodies(page)).toContainEqual({ intent_id: INTENT_ID, step: 1 });
  expect(await page.evaluate(() => window.__alethFetchLog)).toContain("/api/intent/rollback");
  await expect(page.locator("#toastContainer")).toContainText("rewound to step 1");
});

test("a run with no snapshots offers no rewind rather than a dead control", async ({ page }) => {
  await openApp(page, {
    api: {
      start_execution: { success: true, intent_id: INTENT_ID },
      get_intent_steps: { success: true, intent_id: INTENT_ID, steps: [] },
    },
  });
  await startRun(page);
  await dispatchAgentEvent(page, { type: "intent_paused", intent_id: INTENT_ID, message: "hold" });

  const select = page.locator("#steerStepSelect");
  await expect(select).toContainText("no snapshots yet");
  await expect(select).toHaveValue("");
  await expect(page.locator("#btnSteerRollback")).toBeDisabled();
});

test("a verified run offers Apply to Project and the egress reaches the engine", async ({ page }) => {
  await openApp(page, {
    api: {
      start_execution: { success: true, intent_id: INTENT_ID },
      workspace_diff: {
        success: true, staged: true, verified: true, mergeable: true,
        counts: { added: 1, modified: 0, deleted: 0 },
      },
      egress_intent: { success: true, applied: { added: 1, modified: 0, deleted: 0 } },
    },
  });
  await startRun(page);

  // Before the run ends the control is not offered; the terminal event is what makes it a question.
  await expect(page.locator("#btnApplyToProject")).toBeHidden();
  await dispatchAgentEvent(page, { type: "workflow_complete", status: "finished", message: "done" });

  const apply = page.locator("#btnApplyToProject");
  await expect(apply).toBeVisible();
  await apply.click();

  await expect.poll(() => fetchBodies(page)).toContainEqual({ intent_id: INTENT_ID });
  expect(await page.evaluate(() => window.__alethFetchLog)).toContain("/api/intent/egress");
  await expect(page.locator("#toastContainer")).toContainText("Applied to your project");
  await expect(apply).toBeHidden();
});

test("unverified work is not offered for extraction", async ({ page }) => {
  await openApp(page, {
    api: {
      start_execution: { success: true, intent_id: INTENT_ID },
      workspace_diff: { success: true, staged: true, verified: false, mergeable: false },
    },
  });
  await startRun(page);
  await dispatchAgentEvent(page, { type: "workflow_complete", status: "finished", message: "done" });

  await expect(page.locator("#btnApplyToProject")).toBeHidden();
});

test("a collision surfaces as a refusal rather than a silent overwrite", async ({ page }) => {
  await openApp(page, {
    api: {
      start_execution: { success: true, intent_id: INTENT_ID },
      workspace_diff: {
        success: true, staged: true, verified: true, mergeable: true,
        counts: { added: 0, modified: 1, deleted: 0 },
      },
      egress_intent: {
        success: false, conflict: true, collisions: ["pkg/mod.py"],
        error: "the host changed while the agent worked",
      },
    },
  });
  await startRun(page);
  await dispatchAgentEvent(page, { type: "workflow_complete", status: "finished", message: "done" });

  await page.locator("#btnApplyToProject").click();

  await expect(page.locator("#toastContainer")).toContainText("Could not apply the changes");
});

// ---------------------------------------------------------------------------
// 3. Stream resiliency: follow the run, return on the terminal event, hydrate on reconnect
// ---------------------------------------------------------------------------

test("the window returns to the firehose when the run ends, without dropping the terminal event", async ({ page }) => {
  await openApp(page, { api: { start_execution: { success: true, intent_id: INTENT_ID } } });
  await startRun(page);
  await dispatchAgentEvent(page, { type: "workflow_started", action_type: "next_step", plan_file: "PLAN.md" });
  await expect.poll(() => streamPath(page)).toBe(`/api/intents/${INTENT_ID}/stream`);

  // The terminal event is untagged; the per-run stream must still deliver it (Phase 34), or a
  // client would follow a run that had ended and never be told.
  await dispatchAgentEvent(page, { type: "workflow_complete", status: "finished", message: "done" });

  await expect.poll(() => streamPath(page)).toBe("/api/events");
  await expect(page.locator("#btnInterruptRun")).toBeHidden();
});

test("a reconnect hydrates a run that paused while the window was disconnected", async ({ page }) => {
  await openApp(page, { api: { start_execution: { success: true, intent_id: INTENT_ID } } });
  await startRun(page);
  await dispatchAgentEvent(page, { type: "workflow_started", action_type: "next_step", plan_file: "PLAN.md" });

  // The run pauses while the stream is down: the window cannot have seen `intent_paused`.
  await page.evaluate(() => {
    window.__alethApiReturns.get_intent_status = {
      pending_failure: null,
      ledger: [{ intent_id: "i-live", status: "paused_awaiting_input" }],
    };
  });
  const linesBefore = await page.evaluate(() => document.querySelectorAll("#consoleStream .console-line").length);

  await page.evaluate(() => window.__alethStream.drop());
  await expect(page.locator("#engineStatusPill")).toContainText("Reconnecting");
  await page.evaluate(() => window.__alethStream.open());

  // Hydration reads the ledger, so the hold appears even though the event was never delivered...
  await expect(page.locator("#steerOverlay")).toBeVisible();
  await expect(page.locator("#consolePausedBadge")).toBeVisible();
  // ...and it does not replay the transcript: the stream is live-only, so hydration adds no lines.
  const linesAfter = await page.evaluate(() => document.querySelectorAll("#consoleStream .console-line").length);
  expect(linesAfter).toBe(linesBefore);
});

test("a window that reloads into a paused run hydrates the hold from the ledger", async ({ page }) => {
  await openApp(page, {
    api: {
      get_run_state: { running: true },
      get_intent_status: {
        pending_failure: null,
        ledger: [{ intent_id: "i-boot", status: "paused_awaiting_input" }],
      },
    },
  });

  // No event ever reached this window -- it booted into the paused run -- so this is the ledger's
  // answer, not the stream's.
  await expect(page.locator("#steerOverlay")).toBeVisible();
  await expect(page.locator("#consolePausedBadge")).toBeVisible();
  await expect.poll(() => streamPath(page)).toBe("/api/intents/i-boot/stream");
});
