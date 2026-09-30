// Behavioural E2E tests for the built frontend.
//
// The structural suite (./structural.spec.mjs) proves the shell mounts and the plan tree
// renders. It exercises no interaction at all. These tests close that gap: they drive real
// mouse gestures, real keystrokes and real inbound agent events, and assert on what the
// user would see -- computed layout, visible text, visibility, class state.
//
// They are deliberately written against public selectors (`#id`, the classes the CSS
// actually styles) rather than deep structural paths, because they are the safety net for
// an imminent ES-module migration of `ui/js`: a refactor that moves code between files but
// keeps behaviour identical must leave every assertion here passing. Nothing reads a
// module-scoped binding directly.

import { test, expect } from "@playwright/test";

import { openApp, dispatchAgentEvent, collectPageErrors } from "./harness.mjs";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// The console is collapsed at idle (the dock is `console-hidden`), which takes the resize
// seam out of the flow. A run opens it, so that is how a test gets the seam on screen:
// dispatch the same `workflow_started` event the backend sends when a run begins.
async function showConsole(page) {
  await dispatchAgentEvent(page, {
    type: "workflow_started",
    action_type: "custom",
    plan_file: "PLAN.md",
  });
  await expect(page.locator("#dockResizeHandle")).toBeVisible();
}

function dockHeight(page) {
  return page.evaluate(
    () => document.getElementById("bottomDock").getBoundingClientRect().height
  );
}

// One drag of the seam by `dy` pixels: negative is up (which grows the dock), positive is
// down. The pointer is moved onto the handle first so `pointerdown` lands on it, then
// dragged with intermediate moves -- the same gesture a user makes.
async function dragSeam(page, dy) {
  const box = await page.locator("#dockResizeHandle").boundingBox();
  const x = box.x + box.width / 2;
  const y = box.y + box.height / 2;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x, y + dy, { steps: 6 });
  await page.mouse.up();
}

async function openPalette(page) {
  await page.keyboard.press("Control+k");
  await expect(page.locator("#commandPaletteOverlay")).toBeVisible();
}

// Runs the palette command whose label/hint/id matches `query`, by filtering to it and
// pressing Enter -- the keyboard path, not a click.
async function runPaletteCommand(page, query) {
  await openPalette(page);
  await page.fill("#commandPaletteInput", query);
  await page.keyboard.press("Enter");
}

function elementWidth(page, selector) {
  return page.evaluate(
    (sel) => document.querySelector(sel).getBoundingClientRect().width,
    selector
  );
}

// ---------------------------------------------------------------------------
// The bottom dock resize seam
// ---------------------------------------------------------------------------

test.describe("the bottom dock resize seam", () => {
  test("dragging the seam resizes the dock in the direction of the drag", async ({ page }) => {
    await openApp(page);
    await showConsole(page);

    const initial = Math.round(await dockHeight(page));

    // Down shrinks it.
    await dragSeam(page, 100);
    const shrunk = Math.round(await dockHeight(page));
    expect(shrunk, "dragging the seam down should shrink the dock").toBeLessThan(initial);
    // ...by the distance dragged, not some other amount.
    expect(Math.abs(shrunk - (initial - 100)), "the drag delta should map 1:1 to the height").toBeLessThanOrEqual(2);

    // Up grows it.
    await dragSeam(page, -40);
    const grown = Math.round(await dockHeight(page));
    expect(grown, "dragging the seam up should grow the dock").toBeGreaterThan(shrunk);
    expect(Math.abs(grown - (shrunk + 40)), "the drag delta should map 1:1 to the height").toBeLessThanOrEqual(2);
  });

  test("the seam clamps at its floor and its ceiling", async ({ page }) => {
    await openApp(page);
    await showConsole(page);

    // The ceiling is a share of the viewport, so it is derived the same way the seam
    // derives it rather than hard-coded to a window size.
    const ceiling = await page.evaluate(() =>
      Math.max(120, Math.round(window.innerHeight * 0.3))
    );

    // Dragging far past the floor stops at the floor, not at the raw delta.
    await dragSeam(page, 200);
    expect(Math.round(await dockHeight(page)), "the dock cannot shrink below its floor").toBe(120);

    // Dragging far past the ceiling stops at the ceiling.
    await dragSeam(page, -200);
    expect(Math.round(await dockHeight(page)), "the dock cannot grow past its ceiling").toBe(ceiling);
  });
});

// ---------------------------------------------------------------------------
// The command palette
// ---------------------------------------------------------------------------

test.describe("the command palette", () => {
  test("typing filters the command list to the matching command", async ({ page }) => {
    await openApp(page);
    await openPalette(page);

    await expect(page.locator("#commandPaletteList .palette-item")).toHaveCount(6);

    // A query that matches exactly one command narrows the list to it.
    await page.fill("#commandPaletteInput", "analyze");
    await expect(page.locator("#commandPaletteList .palette-item")).toHaveCount(1);
    await expect(page.locator("#commandPaletteList .palette-item")).toContainText("Analyze Codebase");

    // A different query matches a different command, so the filter is live, not one-shot.
    await page.fill("#commandPaletteInput", "bug");
    await expect(page.locator("#commandPaletteList .palette-item")).toHaveCount(1);
    await expect(page.locator("#commandPaletteList .palette-item")).toContainText("Fix Bug");
  });

  test("ArrowDown and ArrowUp move the highlight through the list", async ({ page }) => {
    await openApp(page);
    await openPalette(page);

    const active = page.locator("#commandPaletteList .palette-item.active");
    await expect(active).toHaveCount(1);
    await expect(active).toHaveAttribute("data-command", "next_step");

    await page.keyboard.press("ArrowDown");
    await expect(active).toHaveCount(1);
    await expect(active).toHaveAttribute("data-command", "fix_bug");

    await page.keyboard.press("ArrowUp");
    await expect(active).toHaveAttribute("data-command", "next_step");

    // The highlight cannot leave the list: up at the top and down past the end both hold.
    await page.keyboard.press("ArrowUp");
    await expect(active).toHaveAttribute("data-command", "next_step");
    for (let i = 0; i < 10; i++) await page.keyboard.press("ArrowDown");
    await expect(active).toHaveAttribute("data-command", "custom");
  });

  test("Enter runs the highlighted command and opens its action drawer", async ({ page }) => {
    await openApp(page);

    const drawer = page.locator("#actionDrawerPanel");
    await expect(drawer).toBeHidden();

    await runPaletteCommand(page, "bug");

    // The palette closes and the drawer for the selected command opens.
    await expect(page.locator("#commandPaletteOverlay")).toBeHidden();
    await expect(drawer).toBeVisible();
    await expect(page.locator("#paramModalTitle")).toHaveText("Fix Bug");
  });

  test("Escape closes the palette without opening anything", async ({ page }) => {
    await openApp(page);
    await openPalette(page);

    await page.keyboard.press("Escape");

    await expect(page.locator("#commandPaletteOverlay")).toBeHidden();
    await expect(page.locator("#actionDrawerPanel")).toBeHidden();
  });

  test("running the highlighted command twice folds its drawer away", async ({ page }) => {
    await openApp(page);

    const drawer = page.locator("#actionDrawerPanel");

    await runPaletteCommand(page, "analyze");
    await expect(drawer).toBeVisible();
    // The drawer moves focus into itself shortly after it opens. Wait for that to settle,
    // or it can steal focus back from the palette input before the second Enter lands.
    await expect
      .poll(() =>
        page.evaluate(() =>
          document.getElementById("actionDrawerPanel").contains(document.activeElement)
        )
      )
      .toBe(true);

    // Choosing the same command again is a toggle, not a reset: the drawer folds closed.
    await runPaletteCommand(page, "analyze");
    await expect(drawer).toBeHidden();
  });
});

// ---------------------------------------------------------------------------
// The agent event wire
// ---------------------------------------------------------------------------

test.describe("the agent event wire", () => {
  test("log and tool_call events reach the console transcript", async ({ page }) => {
    await openApp(page);
    await showConsole(page);

    await dispatchAgentEvent(page, {
      type: "log",
      agent: "software-architect",
      log_type: "thinking",
      text: "Scanning the workspace\n",
    });
    await dispatchAgentEvent(page, {
      type: "tool_call",
      agent: "software-architect",
      tool: "read_file",
      args: { filename: "main.py" },
      description: "Reading the file",
    });

    const console = page.locator("#consoleStream");
    await expect(console).toBeVisible();
    await expect(console).toContainText("Scanning the workspace");
    await expect(console).toContainText("read_file main.py");
    await expect(console).toContainText("Reading the file");
  });

  test("an event with an undocumented key is rejected at the bus, not rendered", async ({ page }) => {
    const busErrors = [];
    page.on("console", (msg) => {
      if (msg.type() === "error" && msg.text().includes("deepAgentsBus")) busErrors.push(msg.text());
    });
    await openApp(page);
    await showConsole(page);

    await dispatchAgentEvent(page, {
      type: "log",
      agent: "software-architect",
      log_type: "thinking",
      text: "rejected line\n",
      undocumented: true,
    });

    // The strict sink refuses the payload, so nothing reaches the transcript...
    await expect(page.locator("#consoleStream")).not.toContainText("rejected line");
    // ...and it reports why.
    expect(busErrors.length).toBeGreaterThan(0);
  });

  test("a payload that is not a JSON string is rejected at the bus", async ({ page }) => {
    const busErrors = [];
    page.on("console", (msg) => {
      if (msg.type() === "error" && msg.text().includes("deepAgentsBus")) busErrors.push(msg.text());
    });
    await openApp(page);

    // Exactly what a naive interpolation would have handed over: an object, not a string.
    await page.evaluate(() => window.__deepAgentsBus.receive({ type: "log" }));

    expect(busErrors.length).toBeGreaterThan(0);
  });

  test("a coder_spawn event brings the coder card on screen", async ({ page }) => {
    await openApp(page);

    // A run has to be live for the execution stage (and so the cards) to be on screen;
    // launch one through the real palette + drawer confirm path.
    await runPaletteCommand(page, "next step");
    await page.click("#btnConfirmActionParam");
    await expect(page.locator("#executionStage")).toBeVisible();

    // Before the event, the coder card is not shown.
    await expect(page.locator("#cardCoder")).toBeHidden();

    await dispatchAgentEvent(page, {
      type: "architect_spawn",
      agent: "software-architect",
      name: "Lead Software Architect",
    });
    await dispatchAgentEvent(page, {
      type: "coder_spawn",
      agent: "coder-deep",
      name: "Senior Backend Coder",
      model: "openai:policy/coder-deep",
    });

    await expect(page.locator("#cardArchitect")).toBeVisible();
    await expect(page.locator("#cardCoder")).toBeVisible();
    await expect(page.locator("#coderCardName")).toHaveText("Senior Backend Coder");
    await expect(page.locator("#coderStatusBadge")).toHaveText("Implementing");
  });

  test("a terminal workflow_complete mounts the result view", async ({ page }) => {
    await openApp(page);

    // The result view is chosen by the action that ran, so select one the way a user does.
    await runPaletteCommand(page, "analyze");

    await dispatchAgentEvent(page, {
      type: "architect_summary",
      agent: "software-architect",
      summary: {
        title: "Architect Codebase & Structural Analysis",
        status: "Analysis Complete",
        files: ["main.py"],
        deliverables: ["One settled finding."],
      },
    });
    await dispatchAgentEvent(page, {
      type: "workflow_complete",
      status: "finished",
      message: "Codebase analysis completed.",
    });

    const result = page.locator("#resultView");
    await expect(result).toBeVisible();
    await expect(page.locator("#resultKind")).toHaveText("Analyze Codebase");
    await expect(page.locator("#resultTitle")).toHaveText("Architect Codebase & Structural Analysis");
    await expect(page.locator("#resultBody")).toContainText("One settled finding.");
  });
});

// ---------------------------------------------------------------------------
// Every action's result view
// ---------------------------------------------------------------------------

// The result view is the largest module and the one with the most per-action branches:
// each kind has its own renderer, and a reference that resolves in one branch can be
// missing in another. This sweeps all of them in one place, which is how a module-scope
// reference that the migration forgot to import was found (`state`, used by five
// renderers, undefined in all of them).

test.describe("every action's result view", () => {
  // `query` is a palette filter string (matched against label, hint and id); `label` is
  // what the result view names the action, which is `RESULT_LABELS`'s wording and not
  // always the palette's.
  for (const [query, label] of [
    ["execute next step", "Execute Next Step"],
    ["fix bug", "Fix Bug"],
    ["analyze", "Analyze Codebase"],
    ["recommend", "Get Recommendation"],
    ["update", "Update Plan"],
  ]) {
    test(`"${label}" mounts its result view without an uncaught error`, async ({ page }) => {
      const errors = collectPageErrors(page);
      await openApp(page);

      // Choosing the command is what sets the action the terminal event routes on.
      await runPaletteCommand(page, query);

      await dispatchAgentEvent(page, {
        type: "architect_summary",
        agent: "software-architect",
        summary: { title: `${label} result`, status: "Done", files: [], deliverables: [] },
      });
      await dispatchAgentEvent(page, {
        type: "workflow_complete",
        status: "finished",
        message: `${label} finished.`,
      });

      await expect(page.locator("#resultView")).toBeVisible();
      await expect(page.locator("#resultKind")).toHaveText(label);
      await expect(page.locator("#resultBody")).not.toBeEmpty();

      // The banner is the app's own report of a failure it survived; a renderer that threw
      // would leave the view half-drawn and this would name it.
      await expect(page.locator("#resultView")).not.toContainText("[DeepAgents UI]");
      expect(errors, `uncaught errors while rendering ${label}`).toEqual([]);
    });
  }
});

// ---------------------------------------------------------------------------
// The side panels
// ---------------------------------------------------------------------------

test.describe("the side panels", () => {
  test("collapsing the left sidebar shrinks it to its rail", async ({ page }) => {
    await openApp(page);

    const sidebar = page.locator("#leftSidebar");
    const expanded = await elementWidth(page, "#leftSidebar");
    await expect(sidebar).not.toHaveClass(/collapsed/);
    await expect(page.locator("#leftSidebar .brand-logo-text")).toBeVisible();

    await page.click("#btnToggleLeftSidebar");

    // Collapsed is a real layout change -- the pane narrows to its icon rail and the
    // content it can no longer show is hidden, not merely overlaid.
    await expect(sidebar).toHaveClass(/collapsed/);
    await expect(page.locator("#btnToggleLeftSidebar")).toHaveAttribute("aria-expanded", "false");
    await expect.poll(() => elementWidth(page, "#leftSidebar")).toBeLessThan(expanded);
    await expect(page.locator(".brand-logo-text")).toBeHidden();

    // Toggling back restores the expanded pane.
    await page.click("#btnToggleLeftSidebar");
    await expect(sidebar).not.toHaveClass(/collapsed/);
    await expect(page.locator("#btnToggleLeftSidebar")).toHaveAttribute("aria-expanded", "true");
    await expect.poll(() => elementWidth(page, "#leftSidebar")).toBeGreaterThan(100);
  });

  test("the rail's console toggle opens and closes the dock console", async ({ page }) => {
    await openApp(page);

    // The right pane is a fixed utility rail in this build -- its own collapse chevron is
    // retired (`display:none`, see ui/css/sidebar-panels.css), so the live control it does
    // expose is the console toggle at its foot. That is what is exercised here.
    const dock = page.locator("#bottomDock");
    const toggle = page.locator("#btnConsoleToggle");
    await expect(toggle).toBeVisible();
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(dock).toHaveClass(/console-hidden/);
    await expect(page.locator("#consoleStream")).toBeHidden();

    await toggle.click();

    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    await expect(dock).not.toHaveClass(/console-hidden/);
    await expect(page.locator("#consoleStream")).toBeVisible();
    await expect(page.locator("#dockResizeHandle")).toBeVisible();

    await toggle.click();

    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(dock).toHaveClass(/console-hidden/);
    await expect(page.locator("#consoleStream")).toBeHidden();
  });
});
