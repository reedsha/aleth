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

// --- Task cards -------------------------------------------------------------
// Titles and details come from plan.json, which is compiled from the plan markdown, so
// they still carry the source markers ("**bold**", `code`). A task card is a component,
// not a document, so the markers are dropped instead of printed. One pass only, exactly
// like the workbench: a second pass would rescan the spans the first one inserted.
const PLAN_INLINE_RE = /(`[^`\n]*`)|(\*\*[^*\n]*\*\*)|(__[^_\n]*__)/g;

// Takes ALREADY-ESCAPED text and returns markup. Callers stay responsible for the
// escaping order -- escape the raw string first, then apply this.
function planInlineMarkup(escapedText) {
  const paired = escapedText.replace(PLAN_INLINE_RE, (match, code, strong) => {
    const marker = code ? "`" : (strong ? "**" : "__");
    const inner = match.slice(marker.length, -marker.length);
    return code
      ? `<span class="task-md-code">${inner}</span>`
      : `<span class="task-md-strong">${inner}</span>`;
  });
  // An unpaired marker would otherwise print as literal punctuation. The inserted spans
  // contain neither character, so this cleanup cannot reach into them.
  return paired.replace(/\*\*/g, "").replace(/`/g, "");
}

// The three states the plan format defines. Anything else behaves as pending, which is
// what the previous renderer did too, so an unknown status cannot change behaviour.
function planStepState(step) {
  return (step.status === "completed" || step.status === "in_progress") ? step.status : "pending";
}

function planStatusIcon(stepState) {
  if (stepState === "completed") return '<div class="step-status-icon completed">✓</div>';
  if (stepState === "in_progress") return '<div class="step-status-icon in_progress">●</div>';
  // A pending task is the absence of a state, so the ring is left empty.
  return '<div class="step-status-icon pending"></div>';
}

// Domain pills are only ever drawn from data the plan actually carries. `is_ui` is the
// one domain flag in the schema today -- tools/plan_parser.py reads a "[UI]" tag into it
// and writes it back out -- so it is the only pill that can be shown without inventing
// metadata. API and DB pills hook in here, and only here, once the plan schema carries
// a domain for them.
function planDomainPills(step) {
  if (!step.is_ui) return "";
  return '<span class="task-domain-pill pill-ui">UI</span>';
}

// Deliverables sit under the title rather than inside the collapsed details drawer, so
// they can be read without expanding the card.
function planFileChips(step) {
  return (step.files || []).filter(Boolean).map(file =>
    `<span class="task-file-chip" title="${escapeHtml(file)}">📄 ${escapeHtml(file)}</span>`
  ).join("");
}

const PLAN_EXECUTE_ICON = '<svg width="9" height="9" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><polygon points="6 4 20 12 6 20 6 4"/></svg>';
const PLAN_ROLLBACK_ICON = '<svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 7v6h6"/><path d="M3.5 13a9 9 0 1 0 2.6-7.1L3 8"/></svg>';
const PLAN_STOP_ICON = '<svg width="9" height="9" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="1.5"/></svg>';

// One state, one affordance, drawn inline on the card so a task can be acted on without
// opening anything. The bridge has no per-task stop -- only the global one -- so the
// Stop button is offered only while a run is genuinely active; otherwise the card just
// reports that the task is in progress rather than offering a button that would lie.
function planInlineActions(step, stepState) {
  if (stepState === "pending") {
    return `<button class="btn-inline-execute" data-task-id="${escapeHtml(step.id)}" data-task-title="${escapeHtml(step.title)}" title="Execute this specific task">${PLAN_EXECUTE_ICON}<span>Execute</span></button>`;
  }
  if (stepState === "completed") {
    return `<button class="btn-inline-rollback" data-task-id="${escapeHtml(step.id)}" title="Roll this completed task back to pending">${PLAN_ROLLBACK_ICON}<span>Rollback</span></button>`;
  }
  if (stepState === "in_progress") {
    if (state.isExecuting) {
      return `<button class="btn-inline-stop" title="Stop the running agent execution">${PLAN_STOP_ICON}<span>Stop</span></button>`;
    }
    return '<span class="task-state-chip running">Running</span>';
  }
  return "";
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
    // Uppercase the raw text first: uppercasing an escaped "&amp;" would break the entity.
    headerEl.innerHTML = planInlineMarkup(escapeHtml(String(section.title).toUpperCase()));
    sectionEl.appendChild(headerEl);

    (section.tasks || []).forEach(step => {
      const stepState = planStepState(step);
      const itemEl = document.createElement("div");
      itemEl.className = `plan-tree-item step-${stepState}`;
      itemEl.id = `tree_${step.id}`;

      const details = (step.details || []).filter(d => d !== null && d !== undefined && String(d).trim() !== "");
      const hasDetails = details.length > 0;
      const pills = planDomainPills(step);
      const fileChips = planFileChips(step);
      const metaHtml = (pills || fileChips) ? `<div class="task-card-meta">${pills}${fileChips}</div>` : "";
      const detailsHtml = hasDetails ? `
        <div class="step-details-drawer">
          ${details.map(d => `<div class="step-detail-bullet">${planInlineMarkup(escapeHtml(String(d)))}</div>`).join("")}
        </div>
      ` : "";

      itemEl.innerHTML = `
        <div class="plan-tree-item-row">
          ${planStatusIcon(stepState)}
          <div class="step-title${stepState === "completed" ? " completed" : ""}">${planInlineMarkup(escapeHtml(step.title))}</div>
          <div class="task-card-actions">
            ${planInlineActions(step, stepState)}
            ${hasDetails ? '<span class="step-toggle-arrow">▶</span>' : ''}
          </div>
        </div>
        ${metaHtml}
        ${detailsHtml}
      `;

      // Accordion toggle on row click (excluding clicks on inline action buttons)
      itemEl.addEventListener("click", (e) => {
        if (e.target.closest(".btn-inline-execute") ||
            e.target.closest(".btn-inline-rollback") ||
            e.target.closest(".btn-inline-stop")) return;
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

      // Inline stop button (only rendered while a run is active) mirrors the dock's
      // global stop, because that is the only stop the bridge exposes.
      const stopBtn = itemEl.querySelector(".btn-inline-stop");
      if (stopBtn) {
        stopBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          handleStopClick();
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
