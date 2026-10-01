// Structural tests for the built frontend.
//
// These replace the old startup-contract harness, which asserted on the *shape of the
// generated document* (bundle freshness, module order, a hand-maintained inventory of
// element ids, and a count of `<div>` tags in the markup region). Counting tags proved
// that the document had not been edited by hand, but nothing about whether the app it
// builds works -- a stray `</div>` and a correctly balanced one both passed as long as
// the arithmetic matched.
//
// What is asserted here instead is the structure the app produces at runtime, in a real
// browser, from the real bundle: the four regions mount, every element the wiring caches
// exists, the plan tree renders one card per milestone with its state carried through,
// and the keyboard wiring is live. The bridge is mocked, so a failure here is a failure
// of the frontend alone.
//
// The mock bridge and its fixtures now live in ./harness.mjs, shared with the behavioural
// suite (./interactions.spec.mjs).

import { test, expect } from "@playwright/test";

import { openApp, collectPageErrors } from "./harness.mjs";

// ---------------------------------------------------------------------------
// The shell
// ---------------------------------------------------------------------------

test("the four regions of the shell mount", async ({ page }) => {
  await openApp(page);

  // Mounted, not necessarily on screen: the centre stage is deliberately hidden until a
  // run starts, and shows its empty state in the meantime.
  for (const id of ["leftSidebar", "executionStage", "rightPlanSidebar", "bottomDock"]) {
    await expect(page.locator(`#${id}`), `${id} should be mounted`).toBeAttached();
  }

  await expect(page.locator("#leftSidebar")).toBeVisible();
  await expect(page.locator("#rightPlanSidebar")).toBeVisible();
  await expect(page.locator("#emptyStateContainer")).toBeVisible();
});

test("every element the wiring caches resolves", async ({ page }) => {
  await openApp(page);

  // `DOM` is the app's own element cache: `initDOMElements()` fills it from the document,
  // and every listener and renderer reads it. A null entry is the exact defect the old
  // id inventory was hand-maintained to catch, so it is asserted here from the cache
  // itself -- no list to keep in step with the markup.
  //
  // Read through `window.Aleth.diagnostics()`, which is the app's own health report
  // (the wiring calls the same function and raises a startup banner for any it finds). The
  // cache is module-scoped since the ES-module migration, so this is the explicit seam
  // rather than a reach into a global that no longer exists.
  const diagnostics = await page.evaluate(() => window.Aleth.diagnostics());
  expect(diagnostics.unresolvedElements, "elements the app caches but cannot find").toEqual([]);
});

test("startup raises no uncaught exception and no failure banner", async ({ page }) => {
  const errors = collectPageErrors(page);
  await openApp(page);

  await expect(page.locator("#planTreeContainer")).not.toContainText("[Aleth UI]");
  expect(errors).toEqual([]);
});

test("the gateway handshake populates the agent lists", async ({ page }) => {
  await openApp(page);

  await expect(page.locator("#sidebarMainAgentsList")).toContainText("Lead Software Architect");
  await expect(page.locator("#sidebarCoderAgentsList")).toContainText("Senior Backend Coder");
});

// ---------------------------------------------------------------------------
// The plan tree
// ---------------------------------------------------------------------------

test("the plan tree renders one phase per section and one card per milestone", async ({ page }) => {
  await openApp(page);

  const tree = page.locator("#planTreeContainer");
  await expect(tree.locator(".plan-tree-section")).toHaveCount(2);
  await expect(tree.locator(".plan-phase-header .phase-title")).toHaveText([
    "1. FOUNDATION",
    "2. QUALITY",
  ]);

  // The card count is the *milestone* count: sub-steps are folded into their parent's
  // card, never rendered as cards of their own.
  await expect(tree.locator(".plan-tree-item.plan-step-card")).toHaveCount(3);
  await expect(tree.locator("#tree_task-1 .step-title")).toHaveText("Scaffold the service");
  await expect(tree.locator("#tree_task-3 .step-title")).toHaveText("Write the tests");
});

test("each milestone carries its state and its tag into the tree", async ({ page }) => {
  await openApp(page);

  await expect(page.locator("#tree_task-1")).toHaveClass(/step-completed/);
  await expect(page.locator("#tree_task-2")).toHaveClass(/step-pending/);
  await expect(page.locator("#tree_task-3")).toHaveClass(/step-failed/);

  // A phase's header reports its own completion, which is what makes the tree a
  // summary rather than a list.
  await expect(page.locator('.plan-tree-section[data-phase-id="sec-1"] .phase-count')).toHaveText("1/2");
  await expect(page.locator('.plan-tree-section[data-phase-id="sec-2"] .phase-count')).toHaveText("0/1");

  await expect(page.locator("#tree_task-2")).toContainText("API");
});

test("a milestone's sub-steps, ledger and details stay inside its own card", async ({ page }) => {
  await openApp(page);

  const card = page.locator("#tree_task-1");
  await card.click();

  await expect(card).toContainText("Add the entry point");
  await expect(card).toContainText("wrote the module");
  await expect(card).toContainText("Created: main.py");

  // ...and nothing from another milestone leaked into it.
  await expect(card).not.toContainText("Write the tests");
});

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

test("the keyboard wiring is live: Ctrl+K opens the command palette", async ({ page }) => {
  await openApp(page);

  const overlay = page.locator("#commandPaletteOverlay");
  await expect(overlay).toBeHidden();

  await page.keyboard.press("Control+k");
  await expect(overlay).toBeVisible();
  await expect(page.locator("#commandPaletteList .palette-item")).toHaveCount(6);

  await page.keyboard.press("Escape");
  await expect(overlay).toBeHidden();
});

test("an empty plan is explained rather than left as a blank tree", async ({ page }) => {
  await openApp(page, {
    waitForTree: false,
    plan: {
      filename: "PLAN.md",
      exists: true,
      content: "",
      plans: ["PLAN.md"],
      load_error: "",
      tree: [],
      plan_json: { version: "1.0", plan_file: "PLAN.md", title: "Empty", sections: [], steps: [] },
    },
  });

  // The old harness could not see this at all: a plan with no milestones is a
  // legitimate document, and the app has to say so instead of rendering nothing.
  await expect(page.locator("#planTreeContainer .plan-tree-section")).toHaveCount(0);
  await expect(page.locator("#planTreeContainer .empty-plan-tree-card")).toBeVisible();
  await expect(page.locator("#planTreeContainer")).toContainText("No plan steps detected");
});
