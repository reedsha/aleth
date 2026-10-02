// ui/js/live-run.js — following a live run: the stream it is on, and the steering wheel.
//
// Phase 34 closes the loop between the engine and the operator's screen. A run is no longer a black
// box the UI polls for a corpse:
//
//   * the window follows the run's own stream while it is live and returns to the firehose when it
//     ends (`ui/js/stream.js`);
//   * the operator can *interrupt* a live run — which holds it rather than failing it — and steer it
//     with a correction the agent loop injects as a `[user]` message;
//   * a (re)connection reconciles against the durable ledger, so a window that was closed or
//     disconnected does not lie about whether the run is running, paused, or over.

import { api } from "./api-client.js";
import { emit } from "./bus.js";
import { clearTokenBurn, showConsole } from "./console.js";
import { engineAvailable } from "./connection.js";
import { showToast } from "./notify.js";
import { DOM, setActiveIntentId, state } from "./store.js";
import { followFirehose, followIntent, startStream } from "./stream.js";

// The ledger's vocabulary, not this module's: one spelling, defined where the state is written
// (`storage/intents.py`).
const PAUSED = "paused_awaiting_input";
const TERMINAL = ["completed", "stopped", "failed"];

let paused = false;

/** Starts the stream, with the run-state reconciler bound as its (re)connect hook. */
export function startLiveStream({ onMessage } = {}) {
  return startStream({ onMessage, onOpen: hydrateRunState });
}

/** Points the window at the run it just launched. */
export function followActiveRun(intentId) {
  followIntent(intentId);
}

/**
 * Returns to the firehose and clears every run-scoped surface.
 *
 * Called when a run ends, however it ended. Idempotent: several terminal paths reach it (the
 * completion event, a failure, a launch that never started) and none of them may depend on being
 * the first.
 */
export function leaveRun() {
  paused = false;
  hideSteerOverlay();
  setPausedBadge(false);
  clearTokenBurn();
  followFirehose();
}

// -- the steering wheel --------------------------------------------------------------

/**
 * Holds the live run so the operator can steer it.
 *
 * The interrupt is a *request*: the engine writes the paused state and the loop's gate takes it at
 * its next boundary, so nothing is failed and nothing is cut mid-call. The overlay is not shown
 * here — it appears on the loop's own `intent_paused`, which is the moment the run is genuinely
 * holding.
 */
export async function interruptActiveRun() {
  const intentId = state.activeIntentId;
  if (!intentId) {
    showToast("No live run to interrupt.", "info");
    return;
  }
  if (!engineAvailable()) {
    showToast("The engine is unreachable. Reconnect before interrupting.", "error");
    return;
  }
  try {
    await api.interrupt_intent(intentId, "");
    showToast("Interrupting the run\u2026", "info");
  } catch (err) {
    showToast(`Could not interrupt the run: ${(err && err.message) || err}`, "error");
  }
}

/** Submits the typed correction, which the loop injects into the run's context window. */
export async function submitSteering() {
  const intentId = state.activeIntentId;
  if (!intentId) {
    showToast("No paused run to steer.", "error");
    return;
  }
  const correction = DOM.steerInput ? String(DOM.steerInput.value || "").trim() : "";
  await resumeRun(intentId, correction);
}

/** Resume without a correction: the operator chose not to steer, and the run carries on. */
export async function dismissSteering() {
  const intentId = state.activeIntentId;
  if (!intentId) {
    hideSteerOverlay();
    return;
  }
  await resumeRun(intentId, "");
}

// -- the rewind (Phase 35) ----------------------------------------------------------

/**
 * Rewinds the held run's workspace to the step the operator picked.
 *
 * The files are restored by the engine; the context window and the token bill follow when the run
 * resumes, because those belong to the loop's process and the target travels through the ledger.
 * So this only has to send the intent and say what happened -- and then re-read the step list,
 * because the tree has moved.
 */
export async function rollbackToStep() {
  const intentId = state.activeIntentId;
  const select = DOM.steerStepSelect;
  const step = select && select.value !== "" ? Number(select.value) : NaN;
  if (!intentId) {
    showToast("No paused run to rewind.", "error");
    return;
  }
  if (!Number.isFinite(step)) {
    showToast("Choose a step to rewind to.", "info");
    return;
  }
  const button = DOM.btnSteerRollback;
  if (button) button.disabled = true;
  try {
    await api.rollback_intent(intentId, step);
  } catch (err) {
    showToast(`Could not rewind the run: ${(err && err.message) || err}`, "error");
    return;
  } finally {
    if (button) button.disabled = false;
  }
  showToast(`Workspace rewound to step ${step}.`, "success");
  await loadRollbackSteps();
}

/**
 * Fills the step picker from the shadow's snapshots. Never raises.
 *
 * A run with no snapshots (an older shadow, or a project that never changed a file) offers nothing
 * to rewind to, and the control says so rather than pretending to work.
 */
export async function loadRollbackSteps() {
  const select = DOM.steerStepSelect;
  const button = DOM.btnSteerRollback;
  if (!select) return;
  let payload = null;
  try {
    payload = await api.get_intent_steps(state.activeIntentId);
  } catch (_err) {
    // A read that fails offers nothing to rewind to, and the control says so below.
  }
  const steps = (payload && payload.steps) || [];
  select.textContent = "";
  if (!steps.length) {
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "no snapshots yet";
    select.appendChild(empty);
    if (button) button.disabled = true;
    return;
  }
  if (button) button.disabled = false;
  // Newest first: the rewind a person wants is usually the step before the damage.
  steps.slice().reverse().forEach((entry) => {
    const option = document.createElement("option");
    option.value = String(entry.step);
    const note = String(entry.message || "").replace(/^aleth:\s*/, "");
    option.textContent = `step ${entry.step}${note ? ` \u2014 ${note}` : ""}`;
    select.appendChild(option);
  });
}

async function resumeRun(intentId, correction) {
  const button = DOM.btnSteerSubmit;
  if (button) button.disabled = true;
  try {
    await api.resume_intent(intentId, correction);
  } catch (err) {
    // The hold is still on: the overlay stays, and the operator is told the directive did not go.
    showToast(`Could not resume the run: ${(err && err.message) || err}`, "error");
    return;
  } finally {
    if (button) button.disabled = false;
  }
  // The resume POST *is* the state change — the ledger now says running — so the UI clears the
  // hold on the answer rather than waiting for the loop's `intent_resumed` to arrive. The event
  // still comes, and `renderResumed` is idempotent.
  paused = false;
  hideSteerOverlay();
  setPausedBadge(false);
  if (DOM.steerInput) DOM.steerInput.value = "";
  showToast(correction ? "Directive sent to the agent." : "Run resumed.", "success");
}

// -- rendering the pause/resume transitions -------------------------------------------

/** The run is holding for the operator: freeze the view and open the steering input. */
export function renderPaused(event) {
  const wasPaused = paused;
  paused = true;
  setPausedBadge(true);
  showConsole();
  if (DOM.steerOverlay) {
    DOM.steerOverlay.hidden = false;
    if (DOM.steerInput) {
      DOM.steerInput.value = "";
      DOM.steerInput.focus();
    }
  }
  // A repeat `intent_paused` (a resume that was refused, then the loop announcing the hold again)
  // must not stack a second toast on the same fact.
  if (!wasPaused) showToast("The run is paused. Tell the agent what to change.", "info");
  // The rewind points are the shadow's snapshots, so they are read when the hold opens rather than
  // carried on every event.
  loadRollbackSteps();
}

export function renderResumed() {
  paused = false;
  hideSteerOverlay();
  setPausedBadge(false);
}

export function renderSteered(event) {
  const correction = String((event && event.correction) || "").trim();
  if (correction) showToast(`Steering applied: ${correction}`, "success");
}

function hideSteerOverlay() {
  if (DOM.steerOverlay) DOM.steerOverlay.hidden = true;
}

function setPausedBadge(on) {
  if (DOM.consolePausedBadge) DOM.consolePausedBadge.hidden = !on;
  if (DOM.bottomDock) DOM.bottomDock.classList.toggle("console-paused", Boolean(on));
}

// -- hydration -----------------------------------------------------------------------

/**
 * Reconciles the UI with the ledger after a (re)connect.
 *
 * The stream cannot replay, so a window that dropped -- or was reloaded mid-run -- learns what it
 * missed from the durable ledger instead of from a transcript that no longer exists. Three facts,
 * and only three:
 *
 *   * a run the window is *already* following and the ledger says is held -> show the hold;
 *   * a run the window is following and the ledger says is over -> finalize it (the terminal event
 *     was missed while the stream was down, and without this the UI would show "running" forever);
 *   * a window with no run of its own (a reload) -> adopt whichever run the ledger says is live.
 *
 * It deliberately does **not** consult `get_run_state`, and it never *stops* following a run the
 * launch put it on. The execution thread starts asynchronously, so `running` lags a fresh launch by
 * a moment -- reconciling against it would tear down a run the user had just started.
 *
 * Never raises: a reconcile that fails leaves the UI as it was, which is better than taking the
 * stream's health down with it.
 */
export async function hydrateRunState() {
  try {
    const payload = await api.get_intent_status();
    const ledger = (payload && payload.ledger) || [];
    const activeId = state.activeIntentId;
    if (activeId) {
      const row = ledger.find((entry) => entry && entry.intent_id === activeId);
      if (row && row.status === PAUSED) {
        renderPaused({ intent_id: activeId });
      } else if (row && TERMINAL.indexOf(row.status) !== -1) {
        // The run ended while the window was disconnected. Finalizing unwinds the run lock and
        // returns the stream to the firehose, which is what the missed terminal event would have
        // done.
        emit("run:finalize", { status: row.status });
      }
      return;
    }
    // No run of this window's own: a reloaded window adopts whatever the ledger says is live.
    const live = ledger.find(
      (row) => row && (row.status === "running" || row.status === PAUSED)
    );
    if (!live) return;
    setActiveIntentId(live.intent_id);
    followIntent(live.intent_id);
    if (live.status === PAUSED) renderPaused({ intent_id: live.intent_id });
  } catch (_err) {
    // An observer, not a gate: the stream's health is reported by connection.js, not here.
  }
}
