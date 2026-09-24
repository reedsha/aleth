// ui/js/plan-modals.js — Switch-plan and create-plan modals.
// ============================================================================
// Plan Setup & Creation Modals (Split: Switch vs Create)
// ============================================================================
function openSwitchPlanModal() {
  renderExistingPlansList();
  DOM.switchPlanModalOverlay.style.display = "flex";
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

function renderExistingPlansList() {
  DOM.existingPlansList.innerHTML = "";
  if (!state.availablePlans || state.availablePlans.length === 0) {
    DOM.existingPlansList.innerHTML = `<span style="font-size: 11px; color: #64748b;">No existing .md files found in workspace.</span>`;
    return;
  }

  state.availablePlans.forEach(planName => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = `plan-chip ${planName === state.activePlan ? "active" : ""}`;
    chip.textContent = planName;
    chip.addEventListener("click", async () => {
      closeSwitchPlanModal();
      if (window.pywebview && window.pywebview.api) {
        const res = await window.pywebview.api.set_active_plan(planName);
        applyPlanData(res);
        showToast(`Switched active plan to: ${planName}`, "success");
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
            tasks: [{ id: "task-1", title: `Initialize plan for "${idea.substring(0, 35)}"`, status: "completed", is_ui: false, details: [] }]
          },
          {
            id: "sec-2",
            title: "2. Core Implementation",
            tasks: [{ id: "task-2", title: "Build core application modules", status: "pending", is_ui: false, details: [] }]
          }
        ],
        steps: [
          { id: "task-1", section: "1. Architecture & Setup", title: `Initialize plan for "${idea.substring(0, 35)}"`, status: "completed", is_ui: false, details: [] },
          { id: "task-2", section: "2. Core Implementation", title: "Build core application modules", status: "pending", is_ui: false, details: [] }
        ]
      }
    });
    showToast(`Active plan set to: ${planName}`, "success");
  }
}
