// ui/js/fatal.js — The blocking failure screen, and the way out of it.
//
// A startup failure used to surface as a banner over an otherwise rendered window: the app looked
// alive while nothing it showed was real. Worse, an uncaught throw at the root of a single-page
// app leaves a blank document -- the user cannot tell whether it is still loading, whether the
// engine crashed, or whether the port never bound.
//
// This is the last-resort surface: it says what failed, and it offers exactly one action, which
// is to try again. It is deliberately dependency-free -- no stylesheet, no DOM cache, no
// modules -- because everything it would depend on is something that may have just failed.

let mounted = null;

const OVERLAY_ID = "fatalErrorOverlay";

/** Whether the fatal screen is on screen. Used by the tests and by nothing else. */
export function fatalErrorVisible() {
  return !!(mounted && mounted.isConnected);
}

/** Removes the fatal screen, if one is up. Safe to call unconditionally. */
export function clearFatalError() {
  if (mounted) {
    mounted.remove();
    mounted = null;
  }
}

/**
 * Shows the blocking failure screen.
 *
 * `title` is the one-line verdict, `detail` the underlying message, and `onRetry` is invoked
 * after the overlay is removed -- so a retry that fails again can put a fresh one up rather than
 * stacking two.
 */
export function showFatalError({ title, detail, onRetry } = {}) {
  clearFatalError();
  const body = document.body || document.documentElement;
  if (!body) return null;

  const overlay = document.createElement("div");
  overlay.id = OVERLAY_ID;
  overlay.setAttribute("role", "alertdialog");
  overlay.setAttribute("aria-modal", "true");
  // Inline styles on purpose: a fatal screen that needs a stylesheet to be legible is a fatal
  // screen that can be blanked by the same failure it is reporting.
  overlay.style.cssText =
    "position:fixed;inset:0;z-index:100000;display:flex;align-items:center;justify-content:center;" +
    "background:#0a0c10;color:#cbd5e1;font:13px/1.6 system-ui,-apple-system,Segoe UI,sans-serif;" +
    "padding:24px;text-align:center";

  const card = document.createElement("div");
  card.style.cssText = "max-width:520px;display:flex;flex-direction:column;gap:14px;align-items:center";

  const heading = document.createElement("div");
  heading.style.cssText = "font-size:16px;font-weight:600;color:#f87171";
  heading.textContent = String(title || "The application could not start");

  const message = document.createElement("div");
  message.style.cssText = "color:#94a3b8;white-space:pre-wrap;overflow-wrap:break-word";
  message.textContent = String(detail || "");

  const retry = document.createElement("button");
  retry.type = "button";
  retry.id = "fatalRetryButton";
  retry.textContent = "Retry Connection";
  retry.style.cssText =
    "margin-top:4px;padding:9px 18px;border-radius:8px;border:1px solid #334155;background:#15181d;" +
    "color:#e2e8f0;font:inherit;cursor:pointer";
  retry.addEventListener("click", () => {
    clearFatalError();
    if (typeof onRetry === "function") onRetry();
  });

  card.appendChild(heading);
  card.appendChild(message);
  card.appendChild(retry);
  overlay.appendChild(card);
  body.appendChild(overlay);

  mounted = overlay;
  return overlay;
}
