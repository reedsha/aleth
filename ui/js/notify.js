// ui/js/notify.js — The user-facing feedback channel, owned by no feature module.
//
// Its only import is the DOM cache, so any module can report to the user without importing
// the module that owns the surface. `showToast` lives here rather than in visuals.js because
// visuals.js imports the inbound event handler (`agent-events.js`); before this split every
// module that showed a toast imported visuals.js, which closed a cycle through the event
// handler. Feedback and failure reporting are both channels that must be reachable from
// anywhere, so both live here.

import { DOM } from "./store.js";

export function showToast(message, type = "info") {
  // The feedback channel itself must never throw: if the container is missing, no error the
  // user is being told about could surface (audit L5).
  if (!DOM.toastContainer) return;
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  DOM.toastContainer.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateX(20px)";
    setTimeout(() => toast.remove(), 300);
  }, 3200);
}

// Moved verbatim from wire.js. Surfaces a startup failure that would otherwise be invisible:
// the packaged app runs with debug=False, so an exception during wiring leaves a fully
// rendered window with no click handlers at all ("open, but nothing responds") and no way to
// tell why. It lives here -- not in wire.js -- so modules that wire themselves in (bootstrap)
// can report a startup failure without importing the composition layer, which the other way
// round would close a cycle.
export function reportStartupFailure(message) {
  console.error("[DeepAgents UI]", message);
  // The document head installs a reporter before any module runs. Sharing it keeps
  // every failure in one stack of banners instead of several overlapping at the same
  // position, where all but the last message would be unreadable.
  if (typeof window !== "undefined" && typeof window.__deepAgentsReport === "function") {
    window.__deepAgentsReport(message);
    return;
  }
  try {
    const banner = document.createElement("div");
    banner.textContent = "[DeepAgents UI] " + message;
    banner.style.cssText = "position:fixed;left:12px;right:12px;bottom:12px;z-index:99999;" +
      "padding:10px 14px;background:#7f1d1d;color:#fff;font:12px/1.5 monospace;" +
      "border-radius:8px;white-space:pre-wrap;pointer-events:none";
    (document.body || document.documentElement).appendChild(banner);
  } catch (_err) {
    /* the console line above is the last resort */
  }
}
