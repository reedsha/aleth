// Security tests for the built frontend: the HTML-injection defence.
//
// `ui/js` used to build markup with template strings and assign it with `innerHTML`, which
// means a string that reached a sink with a `<script>` or an `on*` handler was live markup,
// not text. One sink interpolated `agent.display_name` with no escaping at all. Every sink
// now routes through `ui/js/safe-dom.js`, which sanitises against an explicit allowlist
// (DOMPurify), and the live preview's iframe is sandboxed without `allow-scripts` and
// without `allow-same-origin`.
//
// These tests feed the payload through the paths that actually reach those sinks -- the
// agent registry, the plan payload, the inbound agent-event wire and the preview's
// `get_preview_source` bridge call -- and assert on the outcome a browser would produce:
// `window.__pwned` stays undefined, and no `<script>`, `<img>` or `on*` handler survives in
// the DOM. The payload is also asserted to be present *as text*, so a renderer that simply
// dropped the field would not pass.

import { test, expect } from "@playwright/test";

import { openApp, dispatchAgentEvent, collectPageErrors, AGENTS, PLAN } from "./harness.mjs";

// The two shapes a missed escape turns into execution: an event handler on an injected
// element, and an injected script element. Both write the same marker, so a single
// `window.__pwned` assertion covers either.
const IMG_PAYLOAD = '<img src=x onerror="window.__pwned=1">';
const SCRIPT_PAYLOAD = "<script>window.__pwned=1</script>";
const PAYLOAD = `${IMG_PAYLOAD}${SCRIPT_PAYLOAD}`;

/** No injected element or handler survived inside `container`. */
async function expectNoLiveMarkup(container) {
  await expect(container.locator("script")).toHaveCount(0);
  await expect(container.locator("img")).toHaveCount(0);
  await expect(container.locator("iframe")).toHaveCount(0);
  await expect(container.locator("[onerror]")).toHaveCount(0);
  await expect(container.locator("[onclick]")).toHaveCount(0);
}

async function expectNotPwned(page) {
  expect(await page.evaluate(() => window.__pwned)).toBeUndefined();
}

// ---------------------------------------------------------------------------
// The sink that had no escaping at all: the agent registry
// ---------------------------------------------------------------------------

test("a malicious agent display_name cannot execute from the sidebar", async ({ page }) => {
  const errors = collectPageErrors(page);
  await openApp(page, {
    agents: {
      main_agents: [{ ...AGENTS.main_agents[0], display_name: PAYLOAD }],
      coder_agents: [{ ...AGENTS.coder_agents[0], display_name: PAYLOAD }],
    },
  });

  const list = page.locator("#sidebarMainAgentsList");
  await expectNoLiveMarkup(list);
  // The name is still shown -- as text, which is the whole point of the fix.
  await expect(list).toContainText(IMG_PAYLOAD);
  await expectNotPwned(page);
  expect(errors).toEqual([]);
});

// ---------------------------------------------------------------------------
// Plan-supplied strings: section titles, task titles, details and file chips
// ---------------------------------------------------------------------------

test("a malicious plan title cannot execute from the plan tree", async ({ page }) => {
  const errors = collectPageErrors(page);
  const task = {
    id: "task-1",
    section: "1. Payload",
    title: PAYLOAD,
    status: "pending",
    tag: "BE",
    details: [PAYLOAD],
    files: [PAYLOAD],
    behavioral_log: [PAYLOAD],
    sub_steps: [{ id: "task-1-sub-1", title: PAYLOAD, status: "pending", details: [PAYLOAD] }],
  };

  await openApp(page, {
    plan: {
      ...PLAN,
      tree: [{ id: "task-1", title: PAYLOAD, status: "pending" }],
      plan_json: {
        version: "1.0",
        plan_file: "PLAN.md",
        title: "Malicious",
        sections: [{ id: "sec-1", title: "1. Payload", tasks: [task] }],
        steps: [task],
      },
    },
  });

  const tree = page.locator("#planTreeContainer");
  await expect(tree.locator(".plan-tree-section")).toHaveCount(1);
  await expectNoLiveMarkup(tree);
  // Expanded, so the details drawer's payload is rendered too -- and still inert.
  await page.locator("#tree_task-1").click();
  await expectNoLiveMarkup(tree);
  // Shown as text. The plan renderer strips `__` markdown markers, so assert the stable
  // prefix rather than the full payload string.
  await expect(page.locator("#tree_task-1 .step-title")).toContainText('<img src=x onerror="window.');
  await expectNotPwned(page);
  expect(errors).toEqual([]);
});

// ---------------------------------------------------------------------------
// The inbound agent-event wire: card summaries, tool calls and the transcript
// ---------------------------------------------------------------------------

test("a malicious agent event cannot execute from the cards or the transcript", async ({ page }) => {
  const errors = collectPageErrors(page);
  await openApp(page);

  await dispatchAgentEvent(page, {
    type: "architect_summary",
    agent: "software-architect",
    summary: { title: PAYLOAD, status: "Done", deliverables: [PAYLOAD], proposals: [PAYLOAD] },
  });
  await dispatchAgentEvent(page, {
    type: "tool_call",
    agent: "software-architect",
    tool: PAYLOAD,
    args: { filename: PAYLOAD },
    description: PAYLOAD,
  });
  await dispatchAgentEvent(page, {
    type: "log",
    agent: "software-architect",
    log_type: "thinking",
    text: PAYLOAD,
  });

  await expectNoLiveMarkup(page.locator("#architectSummary"));
  await expectNoLiveMarkup(page.locator("#architectStreamContent"));
  await expectNoLiveMarkup(page.locator("#consoleStream"));
  await expect(page.locator("#architectSummary")).toContainText(IMG_PAYLOAD);
  await expectNotPwned(page);
  expect(errors).toEqual([]);
});

// ---------------------------------------------------------------------------
// The document sink: the live preview's srcdoc
// ---------------------------------------------------------------------------

test("the preview sanitises workspace HTML and is sandboxed without scripts", async ({ page }) => {
  const errors = collectPageErrors(page);

  // A generated interface that carries both halves of the attack: a script and an inline
  // handler, plus an embedded frame with its own srcdoc. A working preview keeps the card
  // and its styling; it must drop everything executable.
  const malicious = [
    "<!DOCTYPE html>",
    '<html lang="en"><head><meta charset="utf-8"><title>Preview</title>',
    "<style>body { background: #07090e; } .card { color: #38bdf8; }</style>",
    "<script>window.parent.__pwned = 1; window.__pwned = 1;</script>",
    "</head><body>",
    '<div class="card" onclick="window.__pwned=1">Hello preview</div>',
    '<img src=x onerror="window.__pwned=1">',
    '<iframe srcdoc="<script>window.__pwned=1</script>"></iframe>',
    "</body></html>",
  ].join("\n");

  await openApp(page, {
    api: { get_preview_source: { success: true, found: true, content: malicious } },
  });

  await page.click("#btnTogglePreview");
  const frame = page.locator("#previewFrame");
  await expect(page.locator("#previewPane")).toHaveClass(/active/);
  // The bridge call is async, so wait for the sanitised document to land.
  await expect(frame).toHaveAttribute("srcdoc", /Hello preview/);

  // The sandbox is the primary containment: neither capability that would let the document
  // run script or reach the app's origin is present.
  const sandbox = (await frame.getAttribute("sandbox")) || "";
  expect(sandbox).not.toContain("allow-scripts");
  expect(sandbox).not.toContain("allow-same-origin");

  // ...and the sanitiser is the second layer: the raw HTML it received carried all of these.
  const srcdoc = (await frame.getAttribute("srcdoc")) || "";
  expect(srcdoc).not.toMatch(/<script/i);
  expect(srcdoc).not.toMatch(/<iframe/i);
  expect(srcdoc).not.toMatch(/onerror/i);
  expect(srcdoc).not.toMatch(/onclick/i);
  expect(srcdoc).not.toMatch(/srcdoc/i);
  // The document still previews: the generated markup and its styling survive.
  expect(srcdoc).toContain('class="card"');
  expect(srcdoc).toContain("background: #07090e");

  await expectNotPwned(page);
  expect(errors).toEqual([]);
});
