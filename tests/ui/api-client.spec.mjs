// The HTTP client's first migrated consumer, proven end to end.
//
// The env panel reads through `ui/js/api-client.js`. These tests assert the *transport*, not
// just the rendered result -- a panel that rendered the right rows without asking the gateway
// would be a migration that did not happen.

import { test, expect } from "@playwright/test";

import { openApp } from "./harness.mjs";

const ENV_ROWS = {
  success: true,
  variables: [
    { name: "OPENAI_API_KEY", set: true, length: 51 },
    { name: "ALETH_WORKSPACE_DIR", set: false, length: 0 },
  ],
};

test("the env panel reads over HTTP", async ({ page }) => {
  await openApp(page, { api: { get_environment_variables: ENV_ROWS } });

  await expect(page.locator("#sidebarEnvList")).toContainText("OPENAI_API_KEY");
  await expect(page.locator("#sidebarEnvList")).toContainText("ALETH_WORKSPACE_DIR");
  await expect(page.locator("#countEnvVars")).toHaveText("2");

  // The proof: the panel asked the gateway, on the endpoint the client's table declares.
  const fetched = await page.evaluate(() => window.__alethFetchLog);
  expect(fetched).toContain("/api/env");
});

test("the env panel does not consult a bridge", async ({ page }) => {
  // There is no pywebview bridge in the bundle at all any more -- the app is served by the
  // gateway and reaches it with `fetch`. This pins that: the page starts, renders from the
  // gateway, and nothing on `window` is a second way in.
  await openApp(page, { api: { get_environment_variables: ENV_ROWS } });

  await expect(page.locator("#sidebarEnvList")).toContainText("OPENAI_API_KEY");
  expect(await page.evaluate(() => typeof window.pywebview)).toBe("undefined");
  expect(await page.evaluate(() => typeof window.Aleth)).toBe("object");
});

test("the stream client opens on the events endpoint", async ({ page }) => {
  await openApp(page);

  const streams = await page.evaluate(() =>
    window.__alethStreams.map((stream) => ({ url: stream.url, closed: stream.closed }))
  );
  expect(streams.some((stream) => stream.url === "/api/events" && !stream.closed)).toBe(true);
});

test("a frame from the stream reaches the app through the strict sink", async ({ page }) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  await openApp(page);

  // Wrap the sink the app installed. This is the seam the pywebview push used, so a frame
  // arriving here has taken the same validated path an injected event took -- which is the
  // whole point of routing the stream through it.
  await page.evaluate(() => {
    window.__alethSeen = [];
    const sink = window.__deepAgentsBus;
    const original = sink.receive.bind(sink);
    sink.receive = (raw) => {
      window.__alethSeen.push(raw);
      return original(raw);
    };
  });

  await page.evaluate(() =>
    window.__alethEmit(
      JSON.stringify({ type: "log", agent: "architect", log_type: "thinking", text: "from the stream" })
    )
  );

  const seen = await page.evaluate(() => window.__alethSeen);
  expect(seen.length).toBe(1);
  expect(JSON.parse(seen[0]).text).toBe("from the stream");

  // And the sink still refuses what the contract does not describe: an unknown type is dropped
  // rather than dispatched, and never raises into the page.
  await page.evaluate(() => window.__alethEmit(JSON.stringify({ type: "totally_unknown" })));
  expect(errors).toEqual([]);
});
