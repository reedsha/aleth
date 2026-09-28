// ui/js/plan-tree.js — Plan tree rendering from plan.json, progress meter, and plan step extraction.
// ============================================================================
// Dynamic Plan Tree Tracker (Rendered Exclusively from plan.json)
// ============================================================================
function applyPlanData(planData) {
  if (!planData) return;
  state.activePlan = planData.filename || "PLAN.md";
  // Only a payload that actually states the set of plan files may replace it. A
  // `plan_updated` event carries the plan's *content*, not the file set, so it has no
  // `plans` field -- overwriting the list with `[activePlan]` here is what collapsed the
  // switcher to a single entry after a Laya re-tag (or any save/sync/rollback). Keep the
  // previously loaded list when the field is absent; seed it from the active plan only
  // when nothing is known yet.
  if (Array.isArray(planData.plans)) {
    state.availablePlans = planData.plans;
  } else if (!Array.isArray(state.availablePlans) || state.availablePlans.length === 0) {
    state.availablePlans = state.activePlan ? [state.activePlan] : [];
  }
  state.planJson = planData.plan_json || null;
  state.planTree = (state.planJson && state.planJson.steps) ? state.planJson.steps : (planData.tree || []);

  if (DOM.txtTopActivePlanName) DOM.txtTopActivePlanName.textContent = state.activePlan;
  if (DOM.txtSidebarPlanName) DOM.txtSidebarPlanName.textContent = state.activePlan;

  const hasTasks = state.planTree && state.planTree.length > 0;
  // `exists` is only carried by the load path (get_active_plan). Every plan_updated
  // event and every save/sync return omits it, so treating an absent flag as "missing"
  // used to lock the action panel after any save, switch or sync. Only an explicit
  // false counts as missing; an absent flag leaves the decision to the task count.
  const planMissing = planData.exists === false;
  updateStrictPlanLock(!hasTasks || planMissing);

  renderPlanTree();
  // The sidebar's Plans tab draws the same list this funnel just refreshed, so it is redrawn
  // here rather than only when its tab is opened. Guarded: plan-tree.js loads before sidebar.js.
  if (typeof renderSidebarPlans === "function") renderSidebarPlans();

  // The workbench renders the plan's own markdown, so it is fed from this same funnel:
  // every plan load, switch, save and sync arrives here.
  refreshWorkbenchChrome();
  renderPlanDocument(planData.content);
}

function updateStrictPlanLock(isLocked) {
  if (DOM.actionPanelLockOverlay) {
    DOM.actionPanelLockOverlay.style.display = isLocked ? "flex" : "none";
  }
  // The lock notice is the dock head's only permanent content now that the status row has
  // moved to the top bar, so the head has to know to keep itself on screen for it: without
  // this class the container is display:none at idle and the notice would have nowhere to
  // draw. See .action-control-panel-container.plan-locked in ui/css/dock.css.
  if (DOM.actionControlPanelContainer) {
    DOM.actionControlPanelContainer.classList.toggle("plan-locked", !!isLocked);
  }
  // The dock's six buttons are gone, so this must reach the palette and its trigger instead
  // -- querying .action-btn here would silently lock nothing.
  const controls = document.querySelectorAll(".palette-item, #btnCommandPalette");
  controls.forEach(el => {
    el.disabled = isLocked;
    el.style.opacity = isLocked ? "0.35" : "1";
    el.style.pointerEvents = isLocked ? "none" : "auto";
  });
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

// The four states the plan format defines. Anything else behaves as pending, which is
// what the previous renderer did too, so an unknown status cannot change behaviour.
function planStepState(step) {
  return (step.status === "completed" || step.status === "in_progress" || step.status === "failed")
    ? step.status
    : "pending";
}

function planStatusIcon(stepState) {
  if (stepState === "completed") return '<div class="step-status-icon completed">✓</div>';
  if (stepState === "in_progress") return '<div class="step-status-icon in_progress">●</div>';
  // A failed task needs attention, so its ring carries a cross rather than a tick.
  if (stepState === "failed") return '<div class="step-status-icon failed">✕</div>';
  // A pending task is the absence of a state, so the ring is left empty.
  return '<div class="step-status-icon pending"></div>';
}

// The plan's UI tag, from the vocabulary in tools/task_tags.py. UI-ness is the tag and
// nothing else -- the boolean (`is_ui`) a plan file used to carry is gone, so the tree
// and the delegation path read the same field and cannot disagree.
const UI_TAG = "FE";

function isUiTask(step) {
  return !!step && step.tag === UI_TAG;
}

// Domain pills are drawn only from data the plan actually carries. `tag` is the plan's
// own tag vocabulary (tools/task_tags.py).
// A tag outside the coloured set below falls back to the base pill, which is a complete
// style rather than a broken one -- the tree never invents a domain for a task.
function planTagPill(tag) {
  if (!tag) return "";
  const slug = String(tag).toLowerCase().replace(/[^a-z0-9]+/g, "-");
  return `<span class="task-domain-pill pill-${slug}">${escapeHtml(String(tag))}</span>`;
}

function planDomainPills(step) {
  return planTagPill(step.tag);
}

// A phase card states the domains its tasks belong to, in first-seen order and without
// repetition: the phase's own tag set, which its title alone does not carry.
function planSectionPills(tasks) {
  const seen = [];
  (tasks || []).forEach(task => {
    const tag = task.tag || "";
    if (tag && seen.indexOf(tag) === -1) seen.push(tag);
  });
  return seen.slice(0, 3).map(planTagPill).join("");
}

// Deliverables sit under the title rather than inside the collapsed details drawer, so
// they can be read without expanding the card.
function planFileChips(step) {
  return (step.files || []).filter(Boolean).map(file =>
    `<span class="task-file-chip" title="${escapeHtml(file)}">📄 ${escapeHtml(file)}</span>`
  ).join("");
}

// The Living Behavioral Ledger: the `🟢 Behavioral Log:` lines the compiler writes beneath
// a completed checkbox, which the parser reads back into `behavioral_log`. They are
// evidence about work that is already done rather than instructions, so they render as
// their own muted block beside the claim they belong to -- never mixed into the task's
// own detail notes, which are things still to do.
function planBehavioralLogHtml(entries, extraClass) {
  const logs = (entries || []).filter(e => e !== null && e !== undefined && String(e).trim() !== "");
  if (logs.length === 0) return "";
  const rows = logs
    .map(e => `<div class="behavioral-log-row">${planInlineMarkup(escapeHtml(String(e)))}</div>`)
    .join("");
  return `<div class="behavioral-log-block${extraClass ? " " + extraClass : ""}">${rows}</div>`;
}

// Sub-steps are the indented `- [ ]` items the parser folds into their parent task
// (tools/plan_parser.py). They are deliberately not tasks: they take no `task-N` id and
// no slot in the progress meter, so they are rendered inside the card they belong to,
// behind the same accordion, with a mini progress bar for the group.
function planSubStepsHtml(subSteps) {
  const subs = (subSteps || []).filter(s => s && String(s.title || "").trim() !== "");
  if (subs.length === 0) return "";

  const done = subs.filter(s => planStepState(s) === "completed").length;
  const percent = Math.round((done / subs.length) * 100);
  const rows = subs.map(sub => {
    const subState = planStepState(sub);
    // A sub-step carries the same tag / files / details the parser gives a task, so they are
    // shown with the same pills and chips rather than being persisted and left invisible
    // (audit L3).
    const pills = planDomainPills(sub);
    const fileChips = planFileChips(sub);
    const meta = (pills || fileChips) ? `<div class="task-card-meta">${pills}${fileChips}</div>` : "";
    const details = (sub.details || []).filter(d => d !== null && d !== undefined && String(d).trim() !== "");
    return `
      <div class="sub-step-row plan-substep-item">
        ${planStatusIcon(subState)}
        <div class="sub-step-title${subState === "completed" ? " completed" : (subState === "failed" ? " failed" : "")}">${planInlineMarkup(escapeHtml(String(sub.title)))}</div>
      </div>
      ${meta}
      ${planBehavioralLogHtml(sub.behavioral_log, "behavioral-log-sub")}
      ${details.map(d => `<div class="step-detail-bullet">${planInlineMarkup(escapeHtml(String(d)))}</div>`).join("")}
    `;
  }).join("");

  return `
    <div class="sub-step-progress">
      <div class="sub-step-progress-track"><div class="sub-step-progress-fill" style="width: ${percent}%"></div></div>
      <div class="sub-step-progress-label">${done}/${subs.length}</div>
    </div>
    <div class="sub-step-list">${rows}</div>
  `;
}

// The plan's `## 🌍 Global State Summary` block is captured by the parser into
// `state_summary` rather than becoming a task-less section. It is the standing context
// every milestone slice is anchored on, so it is surfaced at the top of the tree too --
// not only in the workbench document.
function planStateSummaryHtml(planJson) {
  const summary = planJson && planJson.state_summary;
  if (!summary) return "";

  const bullets = (summary.bullets || [])
    .filter(b => b !== null && b !== undefined && String(b).trim() !== "")
    .map(b => `<div class="plan-summary-bullet">${planInlineMarkup(escapeHtml(String(b)))}</div>`)
    .join("");
  if (!bullets) return "";

  return `
    <div class="plan-summary-card">
      <div class="plan-summary-header">${escapeHtml(String(summary.title || "Global State Summary"))}</div>
      <div class="plan-summary-body">${bullets}</div>
    </div>
  `;
}

// --- Bento header -----------------------------------------------------------
// The header is a fixed-height dashboard, so each tile reports one thing in a form that
// cannot reflow the grid: the summary is clamped to a few lines, and the live stats are
// chips rather than a sentence.

// The clamped preview of the plan's standing context. Plain text: the tile holds three
// lines, and the rail renders the formatted version of the same `state_summary`.
function updateBentoSummary(planJson) {
  if (!DOM.bentoSummaryPreview) return;
  const summary = planJson && planJson.state_summary;
  const bullets = ((summary && summary.bullets) || [])
    .filter(b => b !== null && b !== undefined && String(b).trim() !== "");
  DOM.bentoSummaryPreview.textContent = bullets.length
    ? bullets.map(b => String(b).trim()).join(" ")
    : "No global state summary in this plan.";
}

// Live system stats, drawn only from what the frontend actually tracks: the run state, the
// agents generating work right now, and the registry sizes. No token or cost figure reaches
// the event stream, so the tile does not invent one.
function updateBentoStats() {
  if (!DOM.bentoStatsChips) return;
  const chips = [];
  const running = !!state.isExecuting;
  chips.push(running
    ? '<span class="bento-chip state-active">Running</span>'
    : '<span class="bento-chip">Idle</span>');

  const thinking = state.activeAgentIds ? state.activeAgentIds.size : 0;
  if (thinking > 0) chips.push(`<span class="bento-chip state-active">${thinking} thinking</span>`);

  const architects = state.mainAgents ? state.mainAgents.length : 0;
  const coders = state.coderAgents ? state.coderAgents.length : 0;
  chips.push(`<span class="bento-chip">${architects} architect${architects === 1 ? "" : "s"}</span>`);
  chips.push(`<span class="bento-chip">${coders} coder${coders === 1 ? "" : "s"}</span>`);

  const tasks = state.planTree ? state.planTree.length : 0;
  chips.push(`<span class="bento-chip">${tasks} task${tasks === 1 ? "" : "s"}</span>`);

  DOM.bentoStatsChips.innerHTML = chips.join("");
}

// The progress tile's status badge: one chip naming the plan's state, derived from the same
// counts the bar is drawn from, so the badge and the bands cannot disagree.
function updateBentoProgressChip(completed, total, inProgress, failed) {
  const chip = DOM.bentoProgressChip;
  if (!chip) return;
  let label = "No tasks";
  let cls = "bento-chip";
  if (total > 0) {
    if (failed > 0) { label = `${failed} failed`; cls = "bento-chip state-failed"; }
    else if (inProgress > 0) { label = "In Progress"; cls = "bento-chip state-active"; }
    else if (completed >= total) { label = "Complete"; cls = "bento-chip state-complete"; }
    else { label = "Pending"; }
  }
  chip.className = cls;
  chip.textContent = label;
}

const PLAN_EXECUTE_ICON = '<svg width="9" height="9" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><polygon points="6 4 20 12 6 20 6 4"/></svg>';
const PLAN_ROLLBACK_ICON = '<svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 7v6h6"/><path d="M3.5 13a9 9 0 1 0 2.6-7.1L3 8"/></svg>';
const PLAN_STOP_ICON = '<svg width="9" height="9" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="1.5"/></svg>';
const PLAN_EDIT_ICON = '<svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';
const PLAN_FIX_ICON = '<svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><ellipse cx="12" cy="13" rx="4.5" ry="6"/><path d="M9.5 7 8 5M14.5 7 16 5M9 13H2.5M15 13h6.5M9.5 18 8 20M14.5 18 16 20"/></svg>';

// One state, one primary affordance, plus a constant Edit action: editing the plan source is
// available whatever the task's state, so it is appended to every card rather than swapped in
// per state. The bridge has no per-task stop -- only the global one -- so Stop is offered only
// while a run is genuinely active; otherwise the card reports the state rather than offering a
// button that would lie.
function planInlineActions(step, stepState) {
  let primary = "";
  // While a run is live, the card for the task it was launched against hosts the inline Stop.
  // The control used to hang off an `in_progress` status the workflow never writes, so it was
  // wired but could never appear (audit M16). Keyed on run state, it can.
  if (state.isExecuting && state.targetTaskId && String(step.id) === String(state.targetTaskId)) {
    primary = `<button class="btn-inline-stop" title="Stop the running agent execution">${PLAN_STOP_ICON}<span>Stop</span></button>`;
  } else if (stepState === "pending") {
    primary = `<button class="btn-inline-execute" data-task-id="${escapeHtml(step.id)}" data-task-title="${escapeHtml(step.title)}" title="Execute this specific task">${PLAN_EXECUTE_ICON}<span>Execute</span></button>`;
  } else if (stepState === "completed") {
    // A completed task can be rolled back to pending; that is the only action its
    // recorded snapshot state is still needed for.
    primary = `<button class="btn-inline-rollback" data-task-id="${escapeHtml(step.id)}" title="Roll this completed task back to pending">${PLAN_ROLLBACK_ICON}<span>Rollback</span></button>`;
  } else if (stepState === "in_progress") {
    primary = '<span class="task-state-chip running">Running</span>';
  } else if (stepState === "failed") {
    // A failed task is re-run the same way a pending one is. It is not a completed task, so
    // Rollback is not offered -- there is no completed state to roll back from. Fix is the
    // other recovery, and the only one that names the task: it diagnoses and patches the code
    // that made the task fail, so the verdict can land back on this task rather than nowhere.
    primary = `<button class="btn-inline-execute" data-task-id="${escapeHtml(step.id)}" data-task-title="${escapeHtml(step.title)}" title="Retry this task">${PLAN_EXECUTE_ICON}<span>Retry</span></button>`;
    primary += `<button class="btn-inline-fix" data-task-id="${escapeHtml(step.id)}" data-task-title="${escapeHtml(step.title)}" title="Diagnose and fix the code that failed">${PLAN_FIX_ICON}<span>Fix</span></button>`;
  }
  const edit = `<button class="btn-inline-edit" data-task-id="${escapeHtml(step.id)}" title="Edit this task in the plan source">${PLAN_EDIT_ICON}<span>Edit</span></button>`;
  return primary + edit;
}

function renderPlanTree() {
  DOM.planTreeContainer.innerHTML = "";

  const sections = (state.planJson && state.planJson.sections && state.planJson.sections.length > 0)
    ? state.planJson.sections
    : null;

  // Prefer plan.json's own metrics: the tree is derived from the same `steps` list, so the two
  // cannot disagree, and this keeps the backend's figure the single source of the meter
  // (audit L4). The derived counts stay as the fallback for a payload without metrics.
  const metrics = state.planJson && state.planJson.metrics;
  const total = metrics ? (metrics.total_tasks || 0) : (state.planTree ? state.planTree.length : 0);
  const completed = metrics ? (metrics.completed_tasks || 0) : (state.planTree ? state.planTree.filter(t => t.status === "completed").length : 0);
  const inProgress = metrics ? (metrics.in_progress_tasks || 0) : (state.planTree ? state.planTree.filter(t => t.status === "in_progress").length : 0);
  const failed = metrics ? (metrics.failed_tasks || 0) : (state.planTree ? state.planTree.filter(t => t.status === "failed").length : 0);
  updateProgressMeter(completed, total, inProgress, failed);
  // The header is part of the same funnel: every plan load, save, sync and rollback that
  // redraws the tree also refreshes the tiles above it.
  updateBentoSummary(state.planJson);
  updateBentoStats();

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

  // Standing context is the bento header's left tile now (with the full text in the rail),
  // not a card inside the tree, so the milestones below open the list itself.
  const collapsedPhases = state.collapsedPlanPhases;
  const expandedTasks = state.expandedPlanTasks;

  sectionsToRender.forEach((section, sectionIndex) => {
    const sectionEl = document.createElement("div");
    sectionEl.className = "plan-tree-section";
    const phaseId = section.id || `sec-${sectionIndex + 1}`;
    sectionEl.dataset.phaseId = phaseId;
    // A phase defaults to open, so only the phases the user folded away are recorded.
    if (collapsedPhases.has(phaseId)) sectionEl.classList.add("collapsed");

    const tasks = section.tasks || [];
    const secDone = tasks.filter(t => planStepState(t) === "completed").length;

    const headerEl = document.createElement("button");
    headerEl.type = "button";
    headerEl.className = "plan-phase-header";
    headerEl.setAttribute("aria-expanded", String(!sectionEl.classList.contains("collapsed")));
    // Uppercase the raw text first: uppercasing an escaped "&amp;" would break the entity.
    headerEl.innerHTML = `
      <span class="phase-chevron" aria-hidden="true">▼</span>
      <span class="phase-title">${planInlineMarkup(escapeHtml(String(section.title).toUpperCase()))}</span>
      ${planSectionPills(tasks)}
      <span class="phase-count">${secDone}/${tasks.length}</span>
    `;
    headerEl.addEventListener("click", () => togglePlanPhase(sectionEl));
    sectionEl.appendChild(headerEl);

    // Level 2 lives in its own body, so folding the phase away is one class on the card.
    const bodyEl = document.createElement("div");
    bodyEl.className = "plan-phase-body";
    sectionEl.appendChild(bodyEl);

    tasks.forEach(step => {
      const stepState = planStepState(step);
      const itemEl = document.createElement("div");
      // `plan-tree-item` is the cross-module contract (see the startup harness);
      // `plan-step-card` names this as level 2 of the accordion tree.
      itemEl.className = `plan-tree-item plan-step-card step-${stepState}`;
      itemEl.id = `tree_${step.id}`;
      itemEl.dataset.taskId = step.id;
      // A step defaults to closed, so only the steps the user opened are recorded; the set is
      // re-applied here because the tree is rebuilt wholesale on every plan refresh.
      if (expandedTasks.has(step.id)) itemEl.classList.add("expanded");
      // Keyboard reachable: the tree is a list, so it takes a list's keys (see handlePlanTreeKey).
      itemEl.tabIndex = 0;

      const details = (step.details || []).filter(d => d !== null && d !== undefined && String(d).trim() !== "");
      const hasDetails = details.length > 0;
      const subStepsHtml = planSubStepsHtml(step.sub_steps);
      const hasSubSteps = subStepsHtml !== "";
      const behavioralLogHtml = planBehavioralLogHtml(step.behavioral_log);
      // A card opens for any of the three things it can hold: its sub-step breakdown,
      // its ledger of what already ran, and its own detail bullets.
      const expandable = hasDetails || hasSubSteps || behavioralLogHtml !== "";
      const pills = planDomainPills(step);
      const fileChips = planFileChips(step);
      const metaHtml = (pills || fileChips) ? `<div class="task-card-meta">${pills}${fileChips}</div>` : "";
      const drawerHtml = expandable ? `
        <div class="step-details-drawer">
          ${subStepsHtml}
          ${behavioralLogHtml}
          ${details.map(d => `<div class="step-detail-bullet">${planInlineMarkup(escapeHtml(String(d)))}</div>`).join("")}
        </div>
      ` : "";

      itemEl.innerHTML = `
        <div class="plan-tree-item-row">
          ${planStatusIcon(stepState)}
          <div class="step-title${stepState === "completed" ? " completed" : (stepState === "failed" ? " failed" : "")}">${planInlineMarkup(escapeHtml(step.title))}</div>
          <div class="task-card-actions">
            ${planInlineActions(step, stepState)}
            ${expandable ? '<span class="step-toggle-arrow">▶</span>' : ''}
          </div>
        </div>
        ${metaHtml}
        ${drawerHtml}
      `;

      // Accordion toggle on row click (excluding clicks on inline action buttons). The open
      // state is written through to `state` so a refresh cannot silently fold the card.
      itemEl.addEventListener("click", (e) => {
        if (e.target.closest(".btn-inline-execute") ||
            e.target.closest(".btn-inline-rollback") ||
            e.target.closest(".btn-inline-stop") ||
            e.target.closest(".btn-inline-fix") ||
            e.target.closest(".btn-inline-edit")) return;
        togglePlanStepCard(itemEl);
      });

      itemEl.addEventListener("keydown", handlePlanTreeKey);

      // Inline execute button click handler
      const execBtn = itemEl.querySelector(".btn-inline-execute");
      if (execBtn) {
        execBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          openActionDrawer("next_step", {
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

      // Inline fix: only on a failed card. It opens the Fix Bug drawer aimed at this task and
      // seeds the bug description with the recorded failure, so the fix answers the failure
      // that is actually on the card rather than an empty form.
      const fixBtn = itemEl.querySelector(".btn-inline-fix");
      if (fixBtn) {
        fixBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          const failure = (step.details || []).find(
            (d) => typeof d === "string" && d.startsWith("Verification failed:")
          );
          openActionDrawer("fix_bug", {
            targetTaskId: step.id,
            targetTaskTitle: step.title,
            bugPrefill: failure
              ? failure.replace("Verification failed: ", "")
              : `Task '${step.title}' failed verification.`
          });
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

      // Inline edit: every card carries it. Editing always means the raw source, so this is a
      // jump into the workbench's edit mode, scrolled to this task -- not a second editor.
      const editBtn = itemEl.querySelector(".btn-inline-edit");
      if (editBtn) {
        editBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          editTaskInWorkbench(step);
        });
      }

      bodyEl.appendChild(itemEl);
    });

    DOM.planTreeContainer.appendChild(sectionEl);
  });
}

// The two accordion toggles. Each writes through to `state` as well as the DOM, because
// renderPlanTree() rebuilds the tree wholesale on every plan load, save, sync and rollback;
// a class set only on a card would be lost on the next one -- the same trap the sidebar's
// thinking dots had.
function togglePlanPhase(sectionEl) {
  const phaseId = sectionEl.dataset.phaseId;
  if (!phaseId) return;
  const collapsed = sectionEl.classList.toggle("collapsed");
  if (collapsed) state.collapsedPlanPhases.add(phaseId);
  else state.collapsedPlanPhases.delete(phaseId);
  const header = sectionEl.querySelector(".plan-phase-header");
  if (header) header.setAttribute("aria-expanded", String(!collapsed));
}

function togglePlanStepCard(itemEl) {
  const open = itemEl.classList.toggle("expanded");
  const taskId = itemEl.dataset.taskId;
  if (taskId) {
    if (open) state.expandedPlanTasks.add(taskId);
    else state.expandedPlanTasks.delete(taskId);
  }
  return open;
}

// Keyboard path over the cards: Up/Down move focus between them, Enter runs the focused
// card's primary action -- Execute when the task is pending, otherwise expand/collapse. The
// inline buttons keep their own keyboard behaviour, so the keys are ignored when they are the
// event target.
function handlePlanTreeKey(event) {
  const key = event.key;
  if (key !== "ArrowDown" && key !== "ArrowUp" && key !== "Enter") return;
  if (event.target.closest(".btn-inline-execute") ||
      event.target.closest(".btn-inline-rollback") ||
      event.target.closest(".btn-inline-stop") ||
      event.target.closest(".btn-inline-fix") ||
      event.target.closest(".btn-inline-edit")) return;

  const current = event.target.closest(".plan-tree-item");
  if (!current) return;

  if (key === "ArrowDown" || key === "ArrowUp") {
    // Only the cards actually on screen take focus: a card inside a folded phase is still in
    // the DOM, and focusing one would look like the arrow key had stopped working.
    const items = Array.from(document.querySelectorAll(".plan-tree-item"))
      .filter(el => el.offsetParent !== null);
    const at = items.indexOf(current);
    const next = items[at + (key === "ArrowDown" ? 1 : -1)];
    if (next && next.focus) next.focus();
    event.preventDefault();
    return;
  }

  const exec = current.querySelector(".btn-inline-execute");
  if (exec) exec.click();
  else togglePlanStepCard(current);
  event.preventDefault();
}

function updateProgressMeter(completed, total, inProgress = 0, failed = 0) {
  const percent = total > 0 ? Math.round((completed / total) * 100) : 0;
  // Every band is a fraction of the same total, so done + active + failed can never overfill
  // the track; whatever is left shows through as the track's own neutral colour.
  const doneWidth = total > 0 ? (completed / total) * 100 : 0;
  const activeWidth = total > 0 ? (inProgress / total) * 100 : 0;
  const failedWidth = total > 0 ? (failed / total) * 100 : 0;
  if (DOM.txtPlanProgressRatio) DOM.txtPlanProgressRatio.textContent = `${completed}/${total} (${percent}%)`;
  if (DOM.planProgressBarFill) DOM.planProgressBarFill.style.width = `${doneWidth}%`;
  if (DOM.planProgressBarActive) DOM.planProgressBarActive.style.width = `${activeWidth}%`;
  if (DOM.planProgressBarFailed) DOM.planProgressBarFailed.style.width = `${failedWidth}%`;
  updateBentoProgressChip(completed, total, inProgress, failed);
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
