// ui/js/plan-tree.js — Plan tree rendering from plan.json, progress meter, and plan step extraction.
// ============================================================================
// Dynamic Plan Tree Tracker (Rendered Exclusively from plan.json)
// ============================================================================
function applyPlanData(planData) {
  if (!planData) return;
  state.activePlan = planData.filename || "PLAN.md";
  state.availablePlans = planData.plans || [state.activePlan];
  state.planJson = planData.plan_json || null;
  state.planTree = (state.planJson && state.planJson.steps) ? state.planJson.steps : (planData.tree || []);

  if (DOM.txtTopActivePlanName) DOM.txtTopActivePlanName.textContent = state.activePlan;
  if (DOM.txtSidebarPlanName) DOM.txtSidebarPlanName.textContent = state.activePlan;
  if (DOM.txtBottomActivePlan) DOM.txtBottomActivePlan.textContent = state.activePlan;

  const hasTasks = state.planTree && state.planTree.length > 0;
  // `exists` is only carried by the load path (get_active_plan). Every plan_updated
  // event and every save/sync return omits it, so treating an absent flag as "missing"
  // used to lock the action panel after any save, switch or sync. Only an explicit
  // false counts as missing; an absent flag leaves the decision to the task count.
  const planMissing = planData.exists === false;
  updateStrictPlanLock(!hasTasks || planMissing);

  renderPlanTree();
  updateNextStepButtonPreview();

  // The workbench renders the plan's own markdown, so it is fed from this same funnel:
  // every plan load, switch, save and sync arrives here.
  refreshWorkbenchChrome();
  renderPlanDocument(planData.content);
}

function updateStrictPlanLock(isLocked) {
  if (DOM.actionPanelLockOverlay) {
    DOM.actionPanelLockOverlay.style.display = isLocked ? "flex" : "none";
  }
  const actionBtns = document.querySelectorAll(".action-btn");
  actionBtns.forEach(btn => {
    btn.disabled = isLocked;
    btn.style.opacity = isLocked ? "0.35" : "1";
    btn.style.pointerEvents = isLocked ? "none" : "auto";
  });
}

function updateNextStepButtonPreview() {
  if (!state.planTree) return;
  const topPending = state.planTree.find(t => t.status === "pending");
  if (DOM.lblNextStepTarget) {
    if (topPending) {
      const shortTitle = topPending.title.length > 20 ? topPending.title.substring(0, 19) + "..." : topPending.title;
      DOM.lblNextStepTarget.textContent = shortTitle;
      DOM.lblNextStepTarget.title = `Next: ${topPending.title}`;
    } else {
      DOM.lblNextStepTarget.textContent = "All tasks complete";
      DOM.lblNextStepTarget.title = "All steps finished";
    }
  }
}

function renderPlanTree() {
  DOM.planTreeContainer.innerHTML = "";

  const sections = (state.planJson && state.planJson.sections && state.planJson.sections.length > 0)
    ? state.planJson.sections
    : null;

  const total = state.planTree ? state.planTree.length : 0;
  const completed = state.planTree ? state.planTree.filter(t => t.status === "completed").length : 0;
  updateProgressMeter(completed, total);

  if (!state.planTree || state.planTree.length === 0) {
    const hasSelectedFile = Boolean(state.activePlan && state.activePlan.trim());
    if (hasSelectedFile) {
      DOM.planTreeContainer.innerHTML = `
        <div class="empty-plan-tree-card">
          <div class="empty-tree-icon">📄</div>
          <div class="empty-tree-title">No plan steps detected</div>
          <div class="empty-tree-desc">Active file: <code>${escapeHtml(state.activePlan)}</code></div>
          <button class="btn-primary" id="btnTreeExtract" style="font-size: 11.5px; padding: 8px 14px; width: 100%; justify-content: center;">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" style="margin-right: 6px;">
              <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
            </svg>
            <span>Extract Steps from ${escapeHtml(state.activePlan)}</span>
          </button>
        </div>
      `;
      const btnExtract = document.getElementById("btnTreeExtract");
      if (btnExtract) {
        btnExtract.addEventListener("click", () => handleExtractPlanSteps(state.activePlan));
      }
    } else {
      DOM.planTreeContainer.innerHTML = `
        <div class="empty-plan-tree-card">
          <div class="empty-tree-icon">📋</div>
          <div class="empty-tree-title">No plan file selected</div>
          <div class="empty-tree-desc">Select an existing plan file (⇄) or create a new structured roadmap.</div>
          <button class="btn-primary" id="btnTreeEmptyCreate" style="font-size: 11.5px; padding: 8px 14px; width: 100%; justify-content: center;">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" style="margin-right: 6px;">
              <line x1="12" y1="5" x2="12" y2="19"/>
              <line x1="5" y1="12" x2="19" y2="12"/>
            </svg>
            <span>Create New Plan</span>
          </button>
        </div>
      `;
      const btnEmpty = document.getElementById("btnTreeEmptyCreate");
      if (btnEmpty) btnEmpty.addEventListener("click", openCreatePlanModal);
    }
    return;
  }

  // Render from sections if available, or grouped by section from steps
  const sectionsToRender = sections || (() => {
    const grouped = {};
    state.planTree.forEach(step => {
      const sec = step.section || "General";
      if (!grouped[sec]) grouped[sec] = [];
      grouped[sec].push(step);
    });
    return Object.keys(grouped).map((secTitle, idx) => ({
      id: `sec-${idx+1}`,
      title: secTitle,
      tasks: grouped[secTitle]
    }));
  })();

  sectionsToRender.forEach(section => {
    const sectionEl = document.createElement("div");
    sectionEl.className = "plan-tree-section";

    const headerEl = document.createElement("div");
    headerEl.className = "plan-tree-section-header";
    headerEl.textContent = section.title.toUpperCase();
    sectionEl.appendChild(headerEl);

    (section.tasks || []).forEach(step => {
      const itemEl = document.createElement("div");
      itemEl.className = "plan-tree-item";
      itemEl.id = `tree_${step.id}`;

      let iconHtml = '<div class="step-status-icon pending">○</div>';
      if (step.status === "completed") {
        iconHtml = '<div class="step-status-icon completed">✓</div>';
      } else if (step.status === "in_progress") {
        iconHtml = '<div class="step-status-icon in_progress">●</div>';
      }

      const uiBadge = step.is_ui ? '<span class="ui-task-badge">UI</span>' : '';

      // Inline action buttons
      let inlineActionBtn = '';
      if (step.status === "pending") {
        inlineActionBtn = `<button class="btn-inline-execute" data-task-id="${escapeHtml(step.id)}" data-task-title="${escapeHtml(step.title)}" title="Execute this specific task">⚡ Run</button>`;
      } else if (step.status === "completed") {
        inlineActionBtn = `<button class="btn-inline-rollback" data-task-id="${escapeHtml(step.id)}" title="Rollback this completed task to pending">↺ Revert</button>`;
      }

      const hasDetails = (step.details && step.details.length > 0) || (step.files && step.files.length > 0);
      const toggleArrow = hasDetails ? '<span class="step-toggle-arrow">▶</span>' : '';

      let detailsHtml = "";
      if (hasDetails) {
        const bullets = [];
        if (step.details) {
          step.details.forEach(d => bullets.push(`<div class="step-detail-bullet">${escapeHtml(d)}</div>`));
        }
        if (step.files && step.files.length > 0) {
          bullets.push(`<div class="step-detail-bullet" style="color: #38bdf8;"><strong>Files:</strong> ${escapeHtml(step.files.join(", "))}</div>`);
        }
        detailsHtml = `
          <div class="step-details-drawer">
            ${bullets.join("")}
          </div>
        `;
      }

      itemEl.innerHTML = `
        <div class="plan-tree-item-row">
          ${iconHtml}
          ${uiBadge}
          <span class="step-title ${step.status === "completed" ? "completed" : ""}">${escapeHtml(step.title)}</span>
          ${inlineActionBtn}
          ${toggleArrow}
        </div>
        ${detailsHtml}
      `;

      // Accordion toggle on row click (excluding clicks on inline action buttons)
      itemEl.addEventListener("click", (e) => {
        if (e.target.closest(".btn-inline-execute") || e.target.closest(".btn-inline-rollback")) return;
        itemEl.classList.toggle("expanded");
      });

      // Inline execute button click handler
      const execBtn = itemEl.querySelector(".btn-inline-execute");
      if (execBtn) {
        execBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          openActionParamModal("next_step", {
            targetTaskId: step.id,
            targetTaskTitle: step.title
          });
        });
      }

      // Inline rollback button click handler
      const rollbackBtn = itemEl.querySelector(".btn-inline-rollback");
      if (rollbackBtn) {
        rollbackBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          openRollbackModal(step);
        });
      }

      sectionEl.appendChild(itemEl);
    });

    DOM.planTreeContainer.appendChild(sectionEl);
  });
}

function updateProgressMeter(completed, total) {
  const percent = total > 0 ? Math.round((completed / total) * 100) : 0;
  if (DOM.txtPlanProgressRatio) DOM.txtPlanProgressRatio.textContent = `${completed}/${total} (${percent}%)`;
  if (DOM.planProgressBarFill) DOM.planProgressBarFill.style.width = `${percent}%`;
  if (DOM.txtDockProgress) DOM.txtDockProgress.textContent = `${completed}/${total} Tasks (${percent}%)`;
}

async function handleExtractPlanSteps(planName) {
  const targetFile = planName || state.activePlan || "PLAN.md";
  showToast(`Extracting plan steps from ${targetFile}...`, "info");
  if (window.pywebview && window.pywebview.api) {
    try {
      const res = await window.pywebview.api.extract_plan_steps(targetFile);
      if (res && res.steps_count > 0) {
        showToast(`✓ Extracted ${res.steps_count} plan step(s) from ${targetFile}`, "success");
      } else {
        showToast(`No tasks found in ${targetFile}. You can generate structured milestones.`, "info");
        openCreatePlanModal();
      }
    } catch (err) {
      showToast(`Extraction notice: ${err.message}`, "error");
    }
  } else {
    showToast(`Extracted steps from ${targetFile} (preview mode)`, "success");
  }
}
