// ui/js/connection.js — whether the engine is actually reachable, and what the UI does about it.
//
// The stream used to fail silently. `EventSource` reconnects on its own, so a backend that had
// crashed produced no visible change at all: the window kept rendering the last state it had,
// and every button kept firing requests into a void. A user would stack intents into a dead
// queue and only find out much later, from nothing.
//
// So the stream's state is a first-class fact here:
//
//   connecting   -- the first attempt, before the stream has ever opened
//   connected    -- the stream is open; the engine is live
//   reconnecting -- it dropped, and the backoff is running
//   lost         -- it has been down past GRACE_MS; the UI is frozen rather than lying
//
// `engineAvailable()` is the single question every execution trigger asks. The indicator is the
// visible half; freezing the triggers is the half that stops the user doing damage.

import { api } from "./api-client.js";
import { showToast } from "./notify.js";

export const CONNECTING = "connecting";
export const CONNECTED = "connected";
export const RECONNECTING = "reconnecting";
export const LOST = "lost";

// How long the stream may be down before the UI stops accepting new work. Long enough to ride out
// a restart or a laptop waking, short enough that the user is not stacking intents into a queue
// nothing is draining.
export const GRACE_MS = 15000;

const INDICATOR_ID = "engineStatusPill";

// The controls that start or dispatch work. They are disabled whenever the engine is not
// reachable, so the UI cannot promise something it cannot deliver.
const TRIGGER_IDS = [
  "btnConfirmActionParam", // the action drawer's confirm -- the entry point for every run
  "btnConfirmNormalize", // the Normalization Gate's reformat, which is a run
  "btnRetryArchitect",
  "btnRetryCoder",
  // The steering controls (Phase 34) and the rewind (Phase 35): an interrupt, a resume and a
  // rollback are all requests to the engine, so they are disabled with everything else while it is
  // unreachable.
  "btnInterruptRun",
  "btnSteerSubmit",
  "btnSteerCancel",
  "btnSteerRollback",
  // The extraction gate (Phase 38): applying staged work is a request to the engine too.
  "btnApplyToProject",
];

let current = CONNECTING;
let graceTimer = null;
let indicator = null;
let failure = null;
let failureBanner = null;

/**
 * Whether the engine may be asked to do something.
 *
 * Two things close the door, and both are about not lying to the user:
 *
 * * a **lost** stream -- the engine is not answering at all;
 * * an **unacknowledged failure** -- the last run did not do what the user asked, and they have
 *   not been told yet. Accepting a new intent first would bury the failure under a fresh one.
 *
 * A drop does **not** immediately close the door: `reconnecting` is a warning, and the grace
 * window exists so a restart or a laptop waking costs the user nothing.
 */
export function engineAvailable() {
  return current !== LOST && !failure;
}

/** The failure gating the UI, or ``null``. */
export function pendingEngineFailure() {
  return failure;
}

/**
 * Gates the UI on a failed run, until the user acknowledges it.
 *
 * Called from two places on purpose: the stream (an `intent_failed` event, so it is immediate)
 * and startup (reading the ledger, so a failure that happened while the window was closed is
 * still seen). Either way it is the *ledger's* failure, not a UI-only dismissal -- a reload must
 * not forget that a run failed.
 */
export function blockOnEngineFailure(next) {
  failure = next || null;
  renderFailureBanner();
  applyTriggerAvailability();
}

export function connectionState() {
  return current;
}

function indicatorText(state) {
  if (state === CONNECTED) return "";
  if (state === LOST) return "Engine unreachable \u2014 actions are paused";
  if (state === RECONNECTING) return "Reconnecting to engine\u2026";
  return "Connecting to engine\u2026";
}

function renderIndicator(state) {
  if (!indicator) return;
  const text = indicatorText(state);
  indicator.textContent = text;
  indicator.style.display = text ? "block" : "none";
  indicator.dataset.state = state;
  indicator.classList.toggle("lost", state === LOST);
}

function applyTriggerAvailability() {
  const available = engineAvailable();
  TRIGGER_IDS.forEach((id) => {
    const element = document.getElementById(id);
    if (!element) return;
    // A control that was disabled for its own reason (an in-flight request) is not re-enabled
    // here; this only ever *adds* the engine-unavailable reason.
    if (!available) {
      element.disabled = true;
      element.dataset.engineBlocked = "1";
    } else if (element.dataset.engineBlocked === "1") {
      delete element.dataset.engineBlocked;
      element.disabled = false;
    }
  });
}

/**
 * The failure banner: what went wrong, and the one thing the user can do about it.
 *
 * It is not a toast. A toast is gone in three seconds, and this is a gate -- the UI will not
 * accept a new intent until it is dismissed, so it has to stay until it is.
 */
function renderFailureBanner() {
  const body = document.body || document.documentElement;
  if (!body) return;
  if (!failure) {
    if (failureBanner) {
      failureBanner.remove();
      failureBanner = null;
    }
    return;
  }
  if (failureBanner && failureBanner.isConnected) return;

  const banner = document.createElement("div");
  banner.id = "engineFailureBanner";
  banner.setAttribute("role", "alert");
  banner.style.cssText =
    "position:fixed;left:14px;right:14px;bottom:14px;z-index:9999;display:flex;gap:14px;" +
    "align-items:center;justify-content:space-between;padding:10px 14px;border-radius:10px;" +
    "background:#3f1d1d;color:#fecaca;border:1px solid #7f1d1d;" +
    "font:12px/1.5 system-ui,-apple-system,Segoe UI,sans-serif";

  const text = document.createElement("div");
  const detail = failure.error || "the run failed";
  text.textContent = `The last run failed: ${detail}. Actions are paused until you acknowledge it.`;

  const ack = document.createElement("button");
  ack.type = "button";
  ack.id = "engineFailureAcknowledge";
  ack.textContent = "Acknowledge";
  ack.style.cssText =
    "flex:0 0 auto;padding:6px 14px;border-radius:8px;border:1px solid #7f1d1d;" +
    "background:#15181d;color:#e2e8f0;font:inherit;cursor:pointer";
  ack.addEventListener("click", () => acknowledgeFailure());

  banner.appendChild(text);
  banner.appendChild(ack);
  body.appendChild(banner);
  failureBanner = banner;
}

/**
 * Clears the gate, in the ledger and in the UI.
 *
 * The ledger first: a UI-only dismissal would come back on the next reload and tell the user
 * about a failure they already dealt with. If the write fails the gate stays up, which is the
 * honest outcome -- the acknowledgement did not happen.
 */
export async function acknowledgeFailure() {
  if (!failure) return false;
  try {
    await api.acknowledge_intent(failure.intent_id);
  } catch (err) {
    showToast(`Could not acknowledge the failure: ${(err && err.message) || err}`, "error");
    return false;
  }
  blockOnEngineFailure(null);
  showToast("Failure acknowledged.", "success");
  return true;
}

/** Moves the connection to `next` and reflects it: indicator, triggers, and the grace timer. */
export function setConnectionState(next) {
  if (next === current) return;
  // `lost` is left by a successful open and by nothing else. A failed retry is not new
  // information -- and letting it demote the state would un-freeze the UI on every attempt.
  if (current === LOST && next === RECONNECTING) return;
  const previous = current;
  current = next;

  if (graceTimer) {
    clearTimeout(graceTimer);
    graceTimer = null;
  }
  if (next === RECONNECTING) {
    // The freeze is deferred: a drop that recovers inside the grace window should cost nothing.
    graceTimer = setTimeout(() => {
      graceTimer = null;
      if (current === RECONNECTING) setConnectionState(LOST);
    }, GRACE_MS);
  }
  if (next === LOST && previous !== LOST) {
    showToast("The engine is unreachable. Actions are paused until it returns.", "error");
  }
  renderIndicator(next);
  applyTriggerAvailability();
}

/** Builds the status pill. Created from JS so it needs no markup entry and no DOM cache slot. */
export function initConnectionIndicator() {
  if (indicator && indicator.isConnected) return indicator;
  const body = document.body || document.documentElement;
  if (!body) return null;
  indicator = document.createElement("div");
  indicator.id = INDICATOR_ID;
  indicator.setAttribute("role", "status");
  indicator.setAttribute("aria-live", "polite");
  indicator.style.cssText =
    "position:fixed;right:14px;bottom:14px;z-index:9998;padding:6px 12px;border-radius:999px;" +
    "font:11.5px/1.4 system-ui,-apple-system,Segoe UI,sans-serif;letter-spacing:.02em;" +
    "background:#3f2d0a;color:#fbbf24;border:1px solid #78350f;pointer-events:none";
  body.appendChild(indicator);
  renderIndicator(current);
  renderFailureBanner();
  applyTriggerAvailability();
  return indicator;
}
