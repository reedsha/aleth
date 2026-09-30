// Playwright configuration for the frontend's structural tests.
//
// The tests run against the *built* frontend (`dist/`), served by Vite's own preview
// server, because that is the artifact the desktop app loads. A test that ran against
// the sources would prove nothing about the bundle the user actually gets.
//
// The window's Python bridge is mocked per test (see tests/ui/structural.spec.mjs):
// these are structural checks of the document the app builds, not end-to-end runs of
// the agent workflow, and they must not need a workspace, a model or a plan on disk.

import { defineConfig, devices } from "@playwright/test";

const PORT = 5199;

export default defineConfig({
  testDir: "./tests/ui",
  timeout: 30_000,
  expect: { timeout: 7_000 },
  fullyParallel: true,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    // `vite preview` serves dist/ exactly as built, including the unhashed asset
    // names the app's virtual-host mapping resolves at runtime. The host is pinned to
    // IPv4: Vite's default binds `localhost`, which resolves to ::1 on this machine,
    // and a test runner polling 127.0.0.1 would then never see the server come up.
    command: `npm run preview -- --host 127.0.0.1 --port ${PORT} --strictPort`,
    url: `http://127.0.0.1:${PORT}/index.html`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
