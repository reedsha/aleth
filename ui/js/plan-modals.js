// ui/js/plan-modals.js — Switch-plan and create-plan modals.
// ============================================================================
// Plan Setup & Creation Modals (Split: Switch vs Create)
// ============================================================================
async function openSwitchPlanModal() {
  if (!DOM.switchPlanModalOverlay) return;
  DOM.switchPlanModalOverlay.style.display = "flex";
  // The set of plan files is a backend fact, not something to remember from the last
  // payload. Fetch it fresh every time the switcher opens: a save, sync or Laya re-tag
  // sends a `plan_updated` event that carries the plan's *content* but no file list, and
  // remembering the list across those is what left the modal showing only the active
  // file. Asking the backend here makes the list correct regardless of what state holds.
  const api = window.pywebview && window.pywebview.api;
  if (api && typeof api.get_plan_files === "function") {
    try {
      const plans = await api.get_plan_files();
      if (Array.isArray(plans)) state.availablePlans = plans;
    } catch (_err) {
      /* offline / no bridge: fall back to the list already in state */
    }
  }
  renderExistingPlansList();
}

function closeSwitchPlanModal() {
  DOM.switchPlanModalOverlay.style.display = "none";
}

function openCreatePlanModal() {
  DOM.inputNewPlanName.value = "PLAN.md";
  DOM.inputProjectIdea.value = "";
  DOM.createPlanModalOverlay.style.display = "flex";
  setTimeout(() => DOM.inputProjectIdea && DOM.inputProjectIdea.focus(), 60);
}

function closeCreatePlanModal() {
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

  DOM.normalizeGateIssues.innerHTML = issues
    .map((text) => `<li>${escapeHtml(text)}</li>`)
    .join("");

  const pills = [
    `sections: ${counts.sections || 0}`,
    `milestones: ${counts.milestones || 0}`,
    `bullets: ${counts.bullets || 0}`,
    planName ? `file: ${planName}` : "",
  ].filter(Boolean);
  DOM.normalizeGateCounts.innerHTML = pills
    .map((text) => `<span class="normalize-gate-pill">${escapeHtml(text)}</span>`)
    .join("");

  DOM.normalizeGateModalOverlay.style.display = "flex";
}

function closeNormalizeGateModal() {
  DOM.normalizeGateModalOverlay.style.display = "none";
}

// The normalize workflow is a real run (normalize_plan -> start_execution), so it arms the run
// lock and Stop like any other launch -- it used to bypass both, letting a second action stop
// it mid-write while the UI showed no run at all (audit M2/H7). Progress still streams as
// normal agent events.
async function handleNormalizeGateConfirm() {
  closeNormalizeGateModal();
  const api = window.pywebview && window.pywebview.api;
  if (!api || typeof api.normalize_plan !== "function") {
    showToast("Formatting is available in the desktop app window", "info");
    return;
  }
  if (state.isExecuting) {
    showToast("A run is already in progress.", "info");
    return;
  }
  showToast("Formatting plan via Architect...", "info");
  beginRunUi();
  try {
    const res = await api.normalize_plan();
    if (res && res.success === false) abortRunUi(res.error || "A run is already in progress.");
  } catch (err) {
    abortRunUi(`Could not format the plan: ${(err && err.message) || err}`);
  }
}

// Asks the backend whether the plan just switched to can be read as a milestone plan.
// Read-only, and a bridge without the method leaves the plan open exactly as before.
async function checkPlanStructureGate(planName) {
  const api = window.pywebview && window.pywebview.api;
  if (!api || !api.validate_plan_structure) return;
  try {
    const report = await api.validate_plan_structure();
    if (report && report.structured === false) openNormalizeGateModal(planName, report);
  } catch (_err) {
    /* the gate is advisory: a failed check never blocks the switch */
  }
}

function renderExistingPlansList() {
  DOM.existingPlansList.innerHTML = "";
  const plans = Array.isArray(state.availablePlans) ? state.availablePlans : [];
  if (plans.length === 0) {
    DOM.existingPlansList.innerHTML = `<span style="font-size: 11px; color: #64748b;">No existing .md files found in workspace.</span>`;
    return;
  }

  plans.forEach(planName => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = `plan-chip ${planName === state.activePlan ? "active" : ""}`;
    chip.textContent = planName;
    chip.addEventListener("click", async () => {
      closeSwitchPlanModal();
      const api = window.pywebview && window.pywebview.api;
      if (!api) return;
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

async function handleCreatePlanSubmit() {
  const planName = DOM.inputNewPlanName.value.trim() || "PLAN.md";
  const idea = DOM.inputProjectIdea.value.trim() || "Custom Software Project";

  closeCreatePlanModal();
  showToast(`Generating structured plan: ${planName}...`, "info");

  if (window.pywebview && window.pywebview.api) {
    try {
      const res = await window.pywebview.api.create_plan_file(planName, idea);
      applyPlanData(res);
      showToast(`Active plan set to: ${res.filename}`, "success");
    } catch (err) {
      showToast(`Error creating plan: ${err.message}`, "error");
    }
  } else {
    applyPlanData({
      filename: planName,
      exists: true,
      plans: [planName],
      plan_json: {
        version: "1.0",
        plan_file: planName,
        title: idea.substring(0, 50),
        sections: [
          {
            id: "sec-1",
            title: "1. Architecture & Setup",
            tasks: [{ id: "task-1", title: `Initialize plan for "${idea.substring(0, 35)}"`, status: "completed", details: [] }]
          },
          {
            id: "sec-2",
            title: "2. Core Implementation",
            tasks: [{ id: "task-2", title: "Build core application modules", status: "pending", details: [] }]
          }
        ],
        steps: [
          { id: "task-1", section: "1. Architecture & Setup", title: `Initialize plan for "${idea.substring(0, 35)}"`, status: "completed", details: [] },
          { id: "task-2", section: "2. Core Implementation", title: "Build core application modules", status: "pending", details: [] }
        ]
      }
    });
    showToast(`Active plan set to: ${planName}`, "success");
  }
}
