// ui/js/plan-modals.js — Switch-plan and create-plan modals.
// ============================================================================
// Plan Setup & Creation Modals (Split: Switch vs Create)
// ============================================================================
import { api } from "./api-client.js";
import { emit } from "./bus.js";
import { escapeHtml } from "./dom.js";
import { showToast } from "./notify.js";
import { applyPlanData } from "./plan-tree.js";
import { setHtml, setText } from "./safe-dom.js";
import { DOM, setAvailablePlans, state } from "./store.js";

export async function openSwitchPlanModal() {
  if (!DOM.switchPlanModalOverlay) return;
  DOM.switchPlanModalOverlay.style.display = "flex";
  // The set of plan files is a backend fact, not something to remember from the last
  // payload. Fetch it fresh every time the switcher opens: a save, sync or Laya re-tag
  // sends a `plan_updated` event that carries the plan's *content* but no file list, and
  // remembering the list across those is what left the modal showing only the active
  // file. Asking the backend here makes the list correct regardless of what state holds.
  try {
    const plans = await api.get_plan_files();
    setAvailablePlans(plans);
  } catch (_err) {
    /* offline / no bridge: fall back to the list already in state */
  }
  renderExistingPlansList();
}

export function closeSwitchPlanModal() {
  DOM.switchPlanModalOverlay.style.display = "none";
}

export function openCreatePlanModal() {
  DOM.inputNewPlanName.value = "PLAN.md";
  DOM.inputProjectIdea.value = "";
  DOM.createPlanModalOverlay.style.display = "flex";
  // The overlay was just shown in this same task, so it has no box to receive focus yet;
  // the next frame puts focus after layout instead of after a fixed 60 ms guess.
  const field = DOM.inputProjectIdea;
  if (field) {
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => field.focus && field.focus());
    else if (field.focus) field.focus();
  }
}

export function closeCreatePlanModal() {
  DOM.createPlanModalOverlay.style.display = "none";
}

// ----------------------------------------------------------------------------
// Normalization Gate
// ----------------------------------------------------------------------------
// An imported .md that carries neither `##` sections nor `- [ ]` milestones renders as a
// blank plan tree with every action locked. The switch flow asks the backend for a
// structure verdict and offers a 1-click reformat when it fails. The reformat itself is
// the deterministic parser's job (zero Coder tokens); this modal is only the gate.

function openNormalizeGateModal(planName, report) {
  const issues = (report && report.issues) || [];
  const counts = (report && report.counts) || {};

  setHtml(DOM.normalizeGateIssues, issues
    .map((text) => `<li>${escapeHtml(text)}</li>`)
    .join(""));

  const pills = [
    `sections: ${counts.sections || 0}`,
    `milestones: ${counts.milestones || 0}`,
    `bullets: ${counts.bullets || 0}`,
    planName ? `file: ${planName}` : "",
  ].filter(Boolean);
  setHtml(DOM.normalizeGateCounts, pills
    .map((text) => `<span class="normalize-gate-pill">${escapeHtml(text)}</span>`)
    .join(""));

  DOM.normalizeGateModalOverlay.style.display = "flex";
}

export function closeNormalizeGateModal() {
  DOM.normalizeGateModalOverlay.style.display = "none";
}

// The normalize workflow is a real run (normalize_plan -> start_execution), so it arms the run
// lock and Stop like any other launch -- it used to bypass both, letting a second action stop
// it mid-write while the UI showed no run at all (audit M2/H7). Progress still streams as
// normal agent events.
export async function handleNormalizeGateConfirm() {
  closeNormalizeGateModal();
  if (state.isExecuting) {
    showToast("A run is already in progress.", "info");
    return;
  }
  showToast("Formatting plan via Architect...", "info");
  emit("run:begin");
  try {
    const res = await api.normalize_plan();
    if (res && res.success === false) emit("run:abort", { message: res.error || "A run is already in progress." });
  } catch (err) {
    emit("run:abort", { message: `Could not format the plan: ${(err && err.message) || err}` });
  }
}

// Asks the backend whether the plan just switched to can be read as a milestone plan.
// Read-only, and a bridge without the method leaves the plan open exactly as before.
export async function checkPlanStructureGate(planName) {
  try {
    const report = await api.validate_plan_structure();
    if (report && report.structured === false) openNormalizeGateModal(planName, report);
  } catch (_err) {
    /* the gate is advisory: a failed check never blocks the switch */
  }
}

function renderExistingPlansList() {
  setText(DOM.existingPlansList, "");
  const plans = Array.isArray(state.availablePlans) ? state.availablePlans : [];
  if (plans.length === 0) {
    setHtml(DOM.existingPlansList, `<span style="font-size: 11px; color: #64748b;">No existing .md files found in workspace.</span>`);
    return;
  }

  plans.forEach(planName => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = `plan-chip ${planName === state.activePlan ? "active" : ""}`;
    chip.textContent = planName;
    chip.addEventListener("click", async () => {
      closeSwitchPlanModal();
      if (state.isExecuting) {
        showToast("A run is in progress; switch plans after it finishes.", "info");
        return;
      }
      // Mirrors switchActivePlan: the await needs a try/catch, or a rejection is silent and
      // the switch just does not happen (audit M1).
      try {
        const res = await api.set_active_plan(planName);
        if (res && res.error) {
          showToast(res.error, "error");
          return;
        }
        applyPlanData(res);
        showToast(`Switched active plan to: ${planName}`, "success");
        // The gate is advisory and read-only: it only opens a modal when the file it just
        // switched to cannot be read as a milestone plan.
        checkPlanStructureGate(planName);
      } catch (err) {
        showToast(`Could not switch plan: ${(err && err.message) || err}`, "error");
      }
    });
    DOM.existingPlansList.appendChild(chip);
  });
}

export async function handleCreatePlanSubmit() {
  const planName = DOM.inputNewPlanName.value.trim() || "PLAN.md";
  const idea = DOM.inputProjectIdea.value.trim() || "Custom Software Project";

  closeCreatePlanModal();
  showToast(`Generating structured plan: ${planName}...`, "info");

  try {
    const res = await api.create_plan_file(planName, idea);
    applyPlanData(res);
    showToast(`Active plan set to: ${res.filename}`, "success");
  } catch (err) {
    showToast(`Error creating plan: ${err.message}`, "error");
  }
}
