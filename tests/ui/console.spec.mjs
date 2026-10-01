// The detached console page, tested as the page it now is.
//
// It used to be an inline `html=` string handed to pywebview, and its escaping was asserted in
// Python (`DetachedConsoleHtmlTests`). It is a real document now -- served by the gateway, so it
// shares the app's origin and can fetch and stream -- and what it renders is therefore a
// frontend concern. These tests replace that Python class.
//
// The page talks to the gateway, so the test stubs `fetch` and `EventSource` the way the harness
// stubs them for the main window: the point is the page's own logic, not the transport, which
// the Python side already covers.

import { test, expect } from "@playwright/test";

const BACKLOG = {
  lines: [
    { kind: "cmd", text: "run next_step · PLAN.md" },
    { kind: "log", text: "<script>alert(1)</script>" },
  ],
};

async function openConsole(page, backlog = BACKLOG) {
  await page.addInitScript((data) => {
    window.fetch = async () => ({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, data: data.backlog }),
    });
    const streams = [];
    window.__alethStreams = streams;
    window.EventSource = class {
      constructor(url) {
        this.url = url;
        this.closed = false;
        streams.push(this);
        setTimeout(() => !this.closed && this.onopen && this.onopen({}), 0);
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
  }, { backlog });
  await page.goto("/console.html");
}

test("the console page seeds itself from the backlog the backend holds", async ({ page }) => {
  await openConsole(page);

  await expect(page.locator("#stream .ln.cmd")).toHaveCount(1);
  await expect(page.locator("#stream")).toContainText("run next_step");
});

test("a transcript line is rendered as text, never as markup", async ({ page }) => {
  await openConsole(page);

  // The hostile line is in the backlog and is shown verbatim -- as text. `textContent` is the
  // reason: this page renders no HTML at all, so there is no escaping rule to get wrong.
  await expect(page.locator("#stream")).toContainText("<script>alert(1)</script>");
  expect(await page.locator("#stream script").count()).toBe(0);
});

test("the console page reads console_line frames from the stream", async ({ page }) => {
  await openConsole(page);

  const streams = await page.evaluate(() =>
    window.__alethStreams.map((stream) => ({ url: stream.url, closed: stream.closed }))
  );
  expect(streams.some((stream) => stream.url === "/api/events" && !stream.closed)).toBe(true);

  await page.evaluate(() =>
    window.__alethEmit(JSON.stringify({ type: "console_line", kind: "tool", text: "later line" }))
  );
  await expect(page.locator("#stream .ln.tool")).toContainText("later line");

  // A frame it does not consume is ignored, and one with an undocumented key is refused rather
  // than half-rendered -- the same `extra="forbid"` rule the main contract applies.
  await page.evaluate(() =>
    window.__alethEmit(JSON.stringify({ type: "log", agent: "a", log_type: "x", text: "nope" }))
  );
  await page.evaluate(() =>
    window.__alethEmit(JSON.stringify({ type: "console_line", kind: "log", text: "no", extra: 1 }))
  );
  await expect(page.locator("#stream")).not.toContainText("nope");
  await expect(page.locator("#stream")).not.toContainText("no");
});
