// Phase 16 — resilience: the UI must survive backend latency, crashes and desyncs without lying.
//
// Three properties, one per mandate:
//
//   1. a failed API call is an *exception*, not a value a caller can forget to check;
//   2. an engine that cannot be reached produces a blocking, actionable screen -- never a blank
//      document and never a rendered window full of nothing;
//   3. the stream's health is visible, and an engine that is gone stops accepting new work
//      instead of queueing intents nothing will drain.

import { test, expect } from "@playwright/test";

import { openApp } from "./harness.mjs";

const ENV_ROWS = { success: true, variables: [{ name: "OPENAI_API_KEY", set: true, length: 9 }] };

// ---------------------------------------------------------------------------
// 1. Promise integrity: failure throws, so nothing silently marches on
// ---------------------------------------------------------------------------

test("a refused call surfaces as an error rather than an empty panel", async ({ page }) => {
  // The gateway answers `{ok:true, data:{success:false}}`: the call succeeded, the operation
  // refused. That must reach the caller as a failure -- the panel must say it could not read,
  // not render as though there were simply no variables.
  await openApp(page, {
    api: { get_environment_variables: { success: false, error: "the ledger is locked" } },
  });

  await expect(page.locator("#sidebarEnvList")).toContainText("Could not read environment variables");
  await expect(page.locator("#sidebarEnvList")).not.toContainText("OPENAI_API_KEY");
  // ...and specifically NOT the empty state, which would be a lie: nothing was read, so the panel
  // cannot claim there is nothing to read.
  await expect(page.locator("#sidebarEnvList")).not.toContainText("No environment variables are configured");
});

test("an unreachable engine during startup is a blocking screen, not a blank document", async ({ page }) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));

  await openApp(page, { gatewayDown: true, waitForTree: false });

  const overlay = page.locator("#fatalErrorOverlay");
  await expect(overlay).toBeVisible();
  await expect(overlay).toContainText("The engine is unreachable");
  await expect(page.locator("#fatalRetryButton")).toBeVisible();
  // No white screen: the document has content, and the failure did not escape as an uncaught
  // exception (which is what leaves a blank page).
  expect(errors).toEqual([]);
});

test("Retry Connection recovers once the engine is back", async ({ page }) => {
  await openApp(page, { gatewayDown: true, waitForTree: false });
  await expect(page.locator("#fatalErrorOverlay")).toBeVisible();

  // The engine comes back. Retry must re-run the handshake and take the overlay down -- a retry
  // button that cannot succeed is a dead end.
  await page.evaluate(() => {
    window.fetch = async () => ({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, data: {} }),
    });
  });
  await page.locator("#fatalRetryButton").click();

  await expect(page.locator("#fatalErrorOverlay")).toHaveCount(0);
  await expect(page.locator("#planTreeContainer")).toBeAttached();
});

// ---------------------------------------------------------------------------
// 2. The stream's health is visible, and it gates new work
// ---------------------------------------------------------------------------

test("the connection state is part of the app's own health report", async ({ page }) => {
  await openApp(page);

  const diagnostics = await page.evaluate(() => window.Aleth.diagnostics());
  expect(diagnostics.connection).toBe("connected");
  expect(diagnostics.engineAvailable).toBe(true);
  // The indicator is only up while something is wrong.
  await expect(page.locator("#engineStatusPill")).toBeHidden();
});

test("a dropped stream is announced without freezing the UI yet", async ({ page }) => {
  await openApp(page);

  await page.evaluate(() => window.__alethStream.drop());

  // Immediately: visible, and honest about what is happening.
  await expect(page.locator("#engineStatusPill")).toBeVisible();
  await expect(page.locator("#engineStatusPill")).toContainText("Reconnecting to engine");
  expect(await page.evaluate(() => window.Aleth.diagnostics().connection)).toBe("reconnecting");

  // The freeze is deferred: a drop that recovers inside the grace window costs nothing, so the
  // UI keeps accepting work while it waits.
  expect(await page.evaluate(() => window.Aleth.diagnostics().engineAvailable)).toBe(true);
  await expect(page.locator("#btnConfirmActionParam")).not.toBeDisabled();
});

test("a stream that stays down freezes the execution triggers", async ({ page }) => {
  await page.clock.install();
  await openApp(page);
  // Let the boot timers run, including the stream's first open.
  await page.clock.runFor(100);
  expect(await page.evaluate(() => window.Aleth.diagnostics().connection)).toBe("connected");

  await page.evaluate(() => window.__alethStream.drop());
  await page.clock.fastForward(16000);

  expect(await page.evaluate(() => window.Aleth.diagnostics().connection)).toBe("lost");
  await expect(page.locator("#engineStatusPill")).toContainText("unreachable");
  await expect(page.locator("#btnConfirmActionParam")).toBeDisabled();
  await expect(page.locator("#btnRetryArchitect")).toBeDisabled();
});

test("reconnecting re-enables the triggers", async ({ page }) => {
  await page.clock.install();
  await openApp(page);
  await page.clock.runFor(100);

  await page.evaluate(() => window.__alethStream.drop());
  await page.clock.fastForward(16000);
  await expect(page.locator("#btnConfirmActionParam")).toBeDisabled();

  await page.evaluate(() => window.__alethStream.open());
  await page.clock.runFor(50);

  expect(await page.evaluate(() => window.Aleth.diagnostics().connection)).toBe("connected");
  await expect(page.locator("#btnConfirmActionParam")).not.toBeDisabled();
  await expect(page.locator("#engineStatusPill")).toBeHidden();
});
