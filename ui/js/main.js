// ui/js/main.js — the frontend's single entry point.
//
// Every module is imported here, in the order the old concatenating build used, so
// evaluation order is unchanged. The modules import each other explicitly now; this file
// exists so the document has one script to load and so the load order is stated in one
// place instead of living in a build plugin's array.
//
// The stylesheets are imported here too, in cascade order: Vite extracts them into one
// stylesheet and links it from the built document, which is what the old build did by
// writing <style> blocks into <head> in the same order.
//
// What this file publishes is one object -- the boot marker and health report the
// document's head watchdog reads, and the seam the Playwright suite uses to assert the
// app's own diagnostics without reaching into module scope. It is deliberately not the
// ~35 bare functions the old build exposed on `window`.

import "../css/base.css";
import "../css/sidebar.css";
import "../css/stage.css";
import "../css/actions.css";
import "../css/plan-tree.css";
import "../css/modals.css";
import "../css/tiered-prompt-editor.css";
import "../css/ui-vision.css";
import "../css/audit-modal.css";
import "../css/rollback-modal.css";
import "../css/dock.css";
import "../css/workbench.css";
import "../css/code-surface.css";
import "../css/console.css";
import "../css/preview.css";
import "../css/sidebar-panels.css";
import "../css/settings.css";
import "../css/command-palette.css";
import "../css/result-view.css";
import "../css/dag.css";

import "./store.js";
import "./bus.js";
import "./notify.js";
import "./dom.js";
import "./safe-dom.js";
import "./bootstrap.js";
import "./plan-tree.js";
import "./actions.js";
import "./plan-modals.js";
import "./agents.js";
import "./workspace.js";
import "./agent-events.js";
import "./bridge-bus.js";
import "./visuals.js";
import "./dock.js";
import "./workbench.js";
import "./code-surface.js";
import "./console.js";
import "./preview.js";
import "./sidebar.js";
import "./env.js";
import "./settings.js";
import "./command-palette.js";
import "./result-view.js";
import "./diff-surface.js";
import "./dag.js";
// Last on purpose: it registers the DOMContentLoaded bootstrap that calls into every
// module above it.
import "./wire.js";

import { unresolvedElements } from "./store.js";

// The one thing this bundle publishes to the page. The document's head watchdog is a
// classic script -- it runs before any module and cannot import anything -- so a global is
// the only channel it has; publishing a single named namespace is the smallest version of
// that, and is not the same thing as the ~35 loose functions the old build exposed.
//
// `diagnostics` is the app's own health report, used by the wiring as well (see wire.js),
// so it is not a test-only hook.
window.DeepAgents = {
  bootedAt: Date.now(),
  diagnostics: () => ({ unresolvedElements: unresolvedElements() }),
};
