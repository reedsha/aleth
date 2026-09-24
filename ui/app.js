/**
 * DeepAgents Studio Frontend Application Logic
 * Integrates JSON-Anchored Plan Tracker (Dual-Sync Hydration Engine),
 * Action Control Panel (Intent-Driven Gatekeeper Architecture),
 * Action Parameter Modal with Custom Instructions, and 50/50 Split Multi-Agent Orchestration.
 */

// Global State
const state = {
  mainAgents: [],
  coderAgents: [],
  workspaceDir: "./my_project_workspace",
  activePlan: "PLAN.md",
  planTree: [],
  planJson: null,
  availablePlans: [],
  activeAgentForEditor: null,
  isExecuting: false,
  autoScrollArchitect: true,
  autoScrollCoder: true,
  architectCardOpen: false,
  coderCardOpen: false,
  pendingPrompt: "",
  selectedAction: null,
  targetTaskId: null,
  targetTaskTitle: null
};

// DOM Cache
const DOM = {};

document.addEventListener("DOMContentLoaded", () => {
  initDOMElements();
  initOrbAnimation();
  initEventListeners();
  initAutoScrollListeners();

  // PyWebView Bridge initialization
  if (window.pywebview) {
    onPyWebViewReady();
  } else {
    window.addEventListener("pywebviewready", onPyWebViewReady);
    setTimeout(() => {
      if (!window.pywebview) {
        initFallbackMode();
      }
    }, 400);
  }
});

function initDOMElements() {
  DOM.emptyStateContainer = document.getElementById("emptyStateContainer");
  DOM.executionStage = document.getElementById("executionStage");
  DOM.cardsGrid = document.getElementById("cardsGrid");

  DOM.cardArchitect = document.getElementById("cardArchitect");
  DOM.architectLogs = document.getElementById("architectLogs");
  DOM.architectStreamContent = document.getElementById("architectStreamContent");
  DOM.architectStatusBadge = document.getElementById("architectStatusBadge");
  DOM.btnCloseArchitect = document.getElementById("btnCloseArchitect");
  DOM.architectErrorBadge = document.getElementById("architectErrorBadge");
  DOM.architectErrorMsg = document.getElementById("architectErrorMsg");
  DOM.btnRetryArchitect = document.getElementById("btnRetryArchitect");
  DOM.architectSummary = document.getElementById("architectSummary");

  DOM.cardCoder = document.getElementById("cardCoder");
  DOM.coderLogs = document.getElementById("coderLogs");
  DOM.coderStreamContent = document.getElementById("coderStreamContent");
  DOM.coderStatusBadge = document.getElementById("coderStatusBadge");
  DOM.coderCardName = document.getElementById("coderCardName");
  DOM.coderCardModel = document.getElementById("coderCardModel");
  DOM.btnCloseCoder = document.getElementById("btnCloseCoder");
  DOM.coderErrorBadge = document.getElementById("coderErrorBadge");
  DOM.coderErrorMsg = document.getElementById("coderErrorMsg");
  DOM.btnRetryCoder = document.getElementById("btnRetryCoder");
  DOM.coderSummary = document.getElementById("coderSummary");

  // Action Control Panel (Replacing Chatbox)
  DOM.actionControlPanelContainer = document.getElementById("actionControlPanelContainer");
  DOM.actionPanelLockOverlay = document.getElementById("actionPanelLockOverlay");
  DOM.actionDockCard = document.getElementById("actionDockCard");
  DOM.btnActionFixBug = document.getElementById("btnActionFixBug");
  DOM.btnActionNextStep = document.getElementById("btnActionNextStep");
  DOM.btnActionUpdatePlan = document.getElementById("btnActionUpdatePlan");
  DOM.btnActionAnalyze = document.getElementById("btnActionAnalyze");
  DOM.btnActionRecommend = document.getElementById("btnActionRecommend");
  DOM.btnActionCustom = document.getElementById("btnActionCustom");
  DOM.lblNextStepTarget = document.getElementById("lblNextStepTarget");
  DOM.readonlyPlanPill = document.getElementById("readonlyPlanPill");
  DOM.txtBottomActivePlan = document.getElementById("txtBottomActivePlan");
  DOM.txtDockProgress = document.getElementById("txtDockProgress");
  DOM.btnDockStop = document.getElementById("btnDockStop");

  // Action Parameter Modal
  DOM.actionParamModalOverlay = document.getElementById("actionParamModalOverlay");
  DOM.btnCloseParamModal = document.getElementById("btnCloseParamModal");
  DOM.btnCancelParamModal = document.getElementById("btnCancelParamModal");
  DOM.btnConfirmActionParam = document.getElementById("btnConfirmActionParam");
  DOM.btnConfirmActionText = document.getElementById("btnConfirmActionText");
  DOM.paramModalIcon = document.getElementById("paramModalIcon");
  DOM.paramModalTitle = document.getElementById("paramModalTitle");
  DOM.paramModalSubtitle = document.getElementById("paramModalSubtitle");
  DOM.paramActionTypeBadge = document.getElementById("paramActionTypeBadge");
  DOM.paramPresetDescription = document.getElementById("paramPresetDescription");
  DOM.paramTargetTaskCard = document.getElementById("paramTargetTaskCard");
  DOM.paramTargetBadge = document.getElementById("paramTargetBadge");
  DOM.paramTargetTitle = document.getElementById("paramTargetTitle");
  DOM.paramBugSection = document.getElementById("paramBugSection");
  DOM.inputBugDescription = document.getElementById("inputBugDescription");
  DOM.inputBugAttachment = document.getElementById("inputBugAttachment");
  DOM.lblAttachmentName = document.getElementById("lblAttachmentName");
  DOM.bugValidationMsg = document.getElementById("bugValidationMsg");
  DOM.lblActionCustomInstructions = document.getElementById("lblActionCustomInstructions");
  DOM.customValidationMsg = document.getElementById("customValidationMsg");
  DOM.inputActionCustomInstructions = document.getElementById("inputActionCustomInstructions");

  // Workspace & Left Sidebar
  DOM.txtWorkspacePath = document.getElementById("txtWorkspacePath");
  DOM.btnSelectWorkspace = document.getElementById("btnSelectWorkspace");
  DOM.sidebarMainAgentsList = document.getElementById("sidebarMainAgentsList");
  DOM.sidebarCoderAgentsList = document.getElementById("sidebarCoderAgentsList");
  DOM.countMainAgents = document.getElementById("countMainAgents");
  DOM.countCoderAgents = document.getElementById("countCoderAgents");

  // Top Status Bar & Right Plan Sidebar
  DOM.txtTopActivePlanName = document.getElementById("txtTopActivePlanName");
  DOM.topActivePlanBadge = document.getElementById("topActivePlanBadge");
  DOM.txtSidebarPlanName = document.getElementById("txtSidebarPlanName");
  DOM.btnSwitchPlan = document.getElementById("btnSwitchPlan");
  DOM.btnCreatePlanModal = document.getElementById("btnCreatePlanModal");
  DOM.txtPlanProgressRatio = document.getElementById("txtPlanProgressRatio");
  DOM.planProgressBarFill = document.getElementById("planProgressBarFill");
  DOM.planTreeContainer = document.getElementById("planTreeContainer");

  // Plan Management Modals (Split: Switch vs Create)
  DOM.switchPlanModalOverlay = document.getElementById("switchPlanModalOverlay");
  DOM.btnCloseSwitchPlanModal = document.getElementById("btnCloseSwitchPlanModal");
  DOM.createPlanModalOverlay = document.getElementById("createPlanModalOverlay");
  DOM.btnCloseCreatePlanModal = document.getElementById("btnCloseCreatePlanModal");
  DOM.existingPlansList = document.getElementById("existingPlansList");
  DOM.inputNewPlanName = document.getElementById("inputNewPlanName");
  DOM.inputProjectIdea = document.getElementById("inputProjectIdea");
  DOM.btnSubmitCreatePlan = document.getElementById("btnSubmitCreatePlan");

  // Prompt Editor
  DOM.chatWorkspaceView = document.getElementById("chatWorkspaceView");
  DOM.promptEditorView = document.getElementById("promptEditorView");
  DOM.btnBackToChat = document.getElementById("btnBackToChat");
  DOM.editorAgentDisplayName = document.getElementById("editorAgentDisplayName");
  DOM.editorRolePill = document.getElementById("editorRolePill");
  DOM.editorModelBadge = document.getElementById("editorModelBadge");
  DOM.editorFilePathBadge = document.getElementById("editorFilePathBadge");
  DOM.editorToolsList = document.getElementById("editorToolsList");
  DOM.txtSystemPrompt = document.getElementById("txtSystemPrompt");
  DOM.editorLineNumbers = document.getElementById("editorLineNumbers");
  DOM.promptCharCount = document.getElementById("promptCharCount");
  DOM.btnSavePrompt = document.getElementById("btnSavePrompt");
  DOM.btnResetPrompt = document.getElementById("btnResetPrompt");

  // Files Modal & Status Bar
  DOM.btnNavFiles = document.getElementById("btnNavFiles");
  DOM.filesModalOverlay = document.getElementById("filesModalOverlay");
  DOM.btnCloseFilesModal = document.getElementById("btnCloseFilesModal");
  DOM.btnCloseFilesModalFooter = document.getElementById("btnCloseFilesModalFooter");
  DOM.inputSearchWorkspaceFiles = document.getElementById("inputSearchWorkspaceFiles");
  DOM.txtFilesCountBadge = document.getElementById("txtFilesCountBadge");
  DOM.modalFilesList = document.getElementById("modalFilesList");

  // Multimodal UI Vision Section (Module 6)
  DOM.paramUiVisionSection = document.getElementById("paramUiVisionSection");
  DOM.inputUiImageAttachment = document.getElementById("inputUiImageAttachment");
  DOM.lblUiAttachmentName = document.getElementById("lblUiAttachmentName");
  DOM.uiImagePreviewCard = document.getElementById("uiImagePreviewCard");
  DOM.imgUiPreview = document.getElementById("imgUiPreview");
  DOM.btnRemoveUiPreview = document.getElementById("btnRemoveUiPreview");

  // Tiered Prompt Editor (Module 7)
  DOM.btnToggleCoreRules = document.getElementById("btnToggleCoreRules");
  DOM.coreRulesContent = document.getElementById("coreRulesContent");
  DOM.txtCoreRulesPre = document.getElementById("txtCoreRulesPre");
  DOM.lblCoreToggle = document.getElementById("lblCoreToggle");

  // Codebase & Plan Sync Audit Modal (Module 5)
  DOM.btnAuditPlanSync = document.getElementById("btnAuditPlanSync");
  DOM.auditModalOverlay = document.getElementById("auditModalOverlay");
  DOM.btnCloseAuditModal = document.getElementById("btnCloseAuditModal");
  DOM.btnCloseAuditModalFooter = document.getElementById("btnCloseAuditModalFooter");
  DOM.auditStatusPill = document.getElementById("auditStatusPill");
  DOM.auditStatusSummary = document.getElementById("auditStatusSummary");
  DOM.badgeMissingFilesCount = document.getElementById("badgeMissingFilesCount");
  DOM.badgeExistingFilesCount = document.getElementById("badgeExistingFilesCount");
  DOM.listAuditMissing = document.getElementById("listAuditMissing");
  DOM.listAuditExisting = document.getElementById("listAuditExisting");
  DOM.txtUntrackedFiles = document.getElementById("txtUntrackedFiles");
  DOM.btnSyncPlanToCode = document.getElementById("btnSyncPlanToCode");
  DOM.btnForceCodeToPlan = document.getElementById("btnForceCodeToPlan");

  // Rollback Modal (Module 8)
  DOM.rollbackModalOverlay = document.getElementById("rollbackModalOverlay");
  DOM.btnCloseRollbackModal = document.getElementById("btnCloseRollbackModal");
  DOM.btnCancelRollback = document.getElementById("btnCancelRollback");
  DOM.rollbackTargetTitle = document.getElementById("rollbackTargetTitle");
  DOM.rollbackFilesList = document.getElementById("rollbackFilesList");
  DOM.chkRollbackConfirm = document.getElementById("chkRollbackConfirm");
  DOM.btnConfirmRollbackAction = document.getElementById("btnConfirmRollbackAction");

  DOM.systemStatusDot = document.getElementById("systemStatusDot");
  DOM.systemStatusLabel = document.getElementById("systemStatusLabel");
  DOM.toastContainer = document.getElementById("toastContainer");
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

// ============================================================================
// PyWebView Integration & Plan Initialization
// ============================================================================
async function onPyWebViewReady() {
  console.log("[PyWebView] API connected");
  window.onAgentEvent = handleAgentEvent;

  try {
    const agentsData = await window.pywebview.api.get_agents();
    applyAgentsData(agentsData);

    const wsInfo = await window.pywebview.api.get_workspace_info();
    if (wsInfo && wsInfo.workspace_dir) {
      updateWorkspaceUI(wsInfo.workspace_dir);
    }

    // Load active plan with plan.json machine state
    const planData = await window.pywebview.api.get_active_plan();
    applyPlanData(planData);

    if (!planData.exists && (!planData.plans || planData.plans.length === 0)) {
      openCreatePlanModal();
    }
  } catch (err) {
    console.warn("[PyWebView] Init notice:", err);
  }
}

function initFallbackMode() {
  applyAgentsData({
    main_agents: [
      {
        id: "software-architect",
        name: "software-architect",
        display_name: "Lead Software Architect",
        type: "main",
        role: "Coordinator",
        model: "openai:policy/architect",
        system_prompt: "Lead Architect: Planner, verifier, and delegator. Gatekeeper routing intercepting all action intents.",
        file_path: "agents/architect.py",
        tools: ["read_file", "write_file", "append_to_file", "execute_restricted_command"]
      }
    ],
    coder_agents: [
      {
        id: "coder-deep",
        name: "coder-deep",
        display_name: "Senior Backend Coder",
        type: "coder",
        role: "Sub-Agent",
        model: "openai:policy/coder-deep",
        description: "Core algorithms, complex backend logic, and security.",
        system_prompt: "Senior Backend Coder with full shell access. Implementation specialist.",
        file_path: "agents/coders.py",
        tools: ["read_file", "write_file", "append_to_file", "execute_shell_command"]
      },
      {
        id: "coder-standard",
        name: "coder-standard",
        display_name: "Junior Developer",
        type: "coder",
        role: "Sub-Agent",
        model: "openai:policy/coder-standard",
        description: "Tests, boilerplate, documentation, and simple CRUD endpoints.",
        system_prompt: "Junior developer with full shell access. Tests and boilerplate specialist.",
        file_path: "agents/coders.py",
        tools: ["read_file", "write_file", "append_to_file", "execute_shell_command"]
      }
    ],
    workspace_dir: "./my_project_workspace"
  });

  applyPlanData({
    filename: "PLAN.md",
    exists: true,
    plans: ["PLAN.md"],
    plan_json: {
      version: "1.0",
      plan_file: "PLAN.md",
      title: "FastAPI Cloud Weather Microservice",
      sections: [
        {
          id: "sec-1",
          title: "1. Architecture & Setup",
          tasks: [
            { id: "task-1", title: "Formulate system roadmap and data models", status: "completed", is_ui: false, details: ["Lead Architect: Defined requirements", "Dynamic plan: PLAN.md"] },
            { id: "task-2", title: "Scaffold project environment", status: "completed", is_ui: false, details: ["Assigned: coder-deep", "Created: main.py"] }
          ]
        },
        {
          id: "sec-2",
          title: "2. Core Implementation",
          tasks: [
            { id: "task-3", title: "Build weather service REST endpoints", status: "pending", is_ui: false, details: ["Assigned: coder-deep", "File: weather_api.py"] },
            { id: "task-4", title: "Build weather UI dashboard", status: "pending", is_ui: true, details: ["Frontend visualization component"] },
            { id: "task-5", title: "Implement query caching and validation", status: "pending", is_ui: false, details: [] }
          ]
        },
        {
          id: "sec-3",
          title: "3. Testing & Verification",
          tasks: [
            { id: "task-6", title: "Construct automated pytest suite", status: "pending", is_ui: false, details: ["Assigned: coder-standard", "File: test_weather_api.py"] },
            { id: "task-7", title: "Audit deliverables and deployment readiness", status: "pending", details: [] }
          ]
        }
      ],
      steps: [
        { id: "task-1", section: "1. Architecture & Setup", title: "Formulate system roadmap and data models", status: "completed", is_ui: false, details: [] },
        { id: "task-2", section: "1. Architecture & Setup", title: "Scaffold project environment", status: "completed", is_ui: false, details: [] },
        { id: "task-3", section: "2. Core Implementation", title: "Build weather service REST endpoints", status: "pending", is_ui: false, details: [] },
        { id: "task-4", section: "2. Core Implementation", title: "Build weather UI dashboard", status: "pending", is_ui: true, details: [] },
        { id: "task-5", section: "2. Core Implementation", title: "Implement query caching and validation", status: "pending", is_ui: false, details: [] },
        { id: "task-6", section: "3. Testing & Verification", title: "Construct automated pytest suite", status: "pending", is_ui: false, details: [] },
        { id: "task-7", section: "3. Testing & Verification", title: "Audit deliverables and deployment readiness", status: "pending", details: [] }
      ]
    }
  });
}

function applyAgentsData(data) {
  if (!data) return;
  state.mainAgents = data.main_agents || [];
  state.coderAgents = data.coder_agents || [];
  if (data.workspace_dir) {
    updateWorkspaceUI(data.workspace_dir);
  }
  renderSidebarAgents();
}

function updateWorkspaceUI(dirPath) {
  state.workspaceDir = dirPath;
  const parts = dirPath.split(/[/\\]/);
  const folderName = parts[parts.length - 1] || dirPath;
  DOM.txtWorkspacePath.textContent = folderName;
  DOM.txtWorkspacePath.title = dirPath;
}

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
  updateStrictPlanLock(!hasTasks || !planData.exists);

  renderPlanTree();
  updateNextStepButtonPreview();
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

// ============================================================================
// Action Parameter Modal Flow (Critical Rule: All Actions Prompt for Params)
// ============================================================================
function openActionParamModal(actionType, extraParams = {}) {
  if (state.isExecuting) return;
  state.selectedAction = actionType;
  state.targetTaskId = extraParams.targetTaskId || null;
  state.targetTaskTitle = extraParams.targetTaskTitle || null;

  // Reset inputs
  if (DOM.inputActionCustomInstructions) DOM.inputActionCustomInstructions.value = "";
  if (DOM.inputBugDescription) DOM.inputBugDescription.value = "";
  if (DOM.inputBugAttachment) DOM.inputBugAttachment.value = "";
  if (DOM.lblAttachmentName) DOM.lblAttachmentName.textContent = "No file attached";
  if (DOM.bugValidationMsg) DOM.bugValidationMsg.style.display = "none";
  if (DOM.customValidationMsg) DOM.customValidationMsg.style.display = "none";

  if (DOM.paramBugSection) DOM.paramBugSection.style.display = "none";
  if (DOM.paramTargetTaskCard) DOM.paramTargetTaskCard.style.display = "none";
  if (DOM.paramUiVisionSection) DOM.paramUiVisionSection.style.display = "none";
  if (DOM.inputUiImageAttachment) DOM.inputUiImageAttachment.value = "";
  if (DOM.lblUiAttachmentName) DOM.lblUiAttachmentName.textContent = "No image selected";
  if (DOM.uiImagePreviewCard) DOM.uiImagePreviewCard.style.display = "none";

  // Reset custom instructions label and placeholder to default optional
  if (DOM.lblActionCustomInstructions) {
    DOM.lblActionCustomInstructions.textContent = "CUSTOM INSTRUCTIONS / CONSTRAINTS (OPTIONAL)";
  }
  if (DOM.inputActionCustomInstructions) {
    DOM.inputActionCustomInstructions.placeholder = "Append custom constraints, code style rules, specific libraries, or context to this action...";
  }
  if (DOM.btnConfirmActionParam) {
    DOM.btnConfirmActionParam.disabled = false;
  }

  switch (actionType) {
    case "fix_bug":
      DOM.paramModalIcon.textContent = "🐛";
      DOM.paramModalTitle.textContent = "Fix Bug";
      DOM.paramModalSubtitle.textContent = "Architect Bug Diagnosis & Surgical Patching";
      DOM.paramActionTypeBadge.textContent = "Codebase Inspection -> Patch";
      DOM.paramPresetDescription.textContent = "The Software Architect will inspect the codebase using file tools to diagnose what the bug is, locate offending lines, formulate an atomic fix plan, and delegate implementation to a Coder sub-agent.";
      DOM.btnConfirmActionText.textContent = "Diagnose & Fix Bug";
      DOM.paramBugSection.style.display = "block";
      setTimeout(() => DOM.inputBugDescription && DOM.inputBugDescription.focus(), 60);
      break;

    case "next_step":
      DOM.paramModalIcon.textContent = "⚡";
      DOM.paramModalTitle.textContent = "Execute Next Step";
      DOM.paramModalSubtitle.textContent = "Milestone Task Implementation";
      DOM.paramActionTypeBadge.textContent = "Architect Gatekeeper -> Coder";
      DOM.paramPresetDescription.textContent = "Architect evaluates requirements against the active plan, prepares workspace context, and delegates task implementation to a specialized Coder.";
      DOM.btnConfirmActionText.textContent = "Execute Step";

      // Find top pending task if not explicitly passed
      const targetTask = state.targetTaskId 
        ? state.planTree.find(t => t.id === state.targetTaskId) 
        : state.planTree.find(t => t.status === "pending");

      if (targetTask) {
        state.targetTaskId = targetTask.id;
        state.targetTaskTitle = targetTask.title;
        DOM.paramTargetBadge.textContent = targetTask.is_ui ? "UI Task" : "Pending";
        DOM.paramTargetTitle.textContent = targetTask.title;
        DOM.paramTargetTaskCard.style.display = "flex";

        // Multimodal UI Vision Section (Module 6)
        if (targetTask.is_ui && DOM.paramUiVisionSection) {
          DOM.paramUiVisionSection.style.display = "block";
        }
      }
      setTimeout(() => DOM.inputActionCustomInstructions && DOM.inputActionCustomInstructions.focus(), 60);
      break;

    case "update_plan":
      DOM.paramModalIcon.textContent = "✏️";
      DOM.paramModalTitle.textContent = "Update / Edit Plan";
      DOM.paramModalSubtitle.textContent = "Machine State Refinement (No Coder)";
      DOM.paramActionTypeBadge.textContent = "Administrative Bypass";
      DOM.paramPresetDescription.textContent = "Architect directly refines plan.json tasks and milestones based on your input, immediately compiling changes into PLAN.md.";
      DOM.btnConfirmActionText.textContent = "Update Plan";
      setTimeout(() => DOM.inputActionCustomInstructions && DOM.inputActionCustomInstructions.focus(), 60);
      break;

    case "analyze":
      DOM.paramModalIcon.textContent = "🔍";
      DOM.paramModalTitle.textContent = "Analyze Codebase";
      DOM.paramModalSubtitle.textContent = "Architect Architecture Inspection (No Coder)";
      DOM.paramActionTypeBadge.textContent = "Direct Architect Query";
      DOM.paramPresetDescription.textContent = "Architect inspects workspace files, checks interfaces and constraints, and produces a thorough structural report.";
      DOM.btnConfirmActionText.textContent = "Run Analysis";
      setTimeout(() => DOM.inputActionCustomInstructions && DOM.inputActionCustomInstructions.focus(), 60);
      break;

    case "recommend":
      DOM.paramModalIcon.textContent = "💡";
      DOM.paramModalTitle.textContent = "Get Recommendation";
      DOM.paramModalSubtitle.textContent = "Architect Strategic Guidance (No Coder)";
      DOM.paramActionTypeBadge.textContent = "Advisory Loop";
      DOM.paramPresetDescription.textContent = "Architect reviews current milestone completion in plan.json and workspace code to propose strategic next steps.";
      DOM.btnConfirmActionText.textContent = "Get Recommendations";
      setTimeout(() => DOM.inputActionCustomInstructions && DOM.inputActionCustomInstructions.focus(), 60);
      break;

    case "custom":
    default:
      DOM.paramModalIcon.textContent = "💬";
      DOM.paramModalTitle.textContent = "Custom Action";
      DOM.paramModalSubtitle.textContent = "Architect Smart Gatekeeper Routing";
      DOM.paramActionTypeBadge.textContent = "Dynamic Intent Dispatch";
      DOM.paramPresetDescription.textContent = "Software Architect intercepts your custom instruction, determines required actions, and handles directly or delegates to Coders.";
      DOM.btnConfirmActionText.textContent = "Execute Directive";
      if (DOM.lblActionCustomInstructions) {
        DOM.lblActionCustomInstructions.innerHTML = 'CUSTOM DIRECTIVE / INSTRUCTION <span class="required-star">*Required</span>';
      }
      if (DOM.inputActionCustomInstructions) {
        DOM.inputActionCustomInstructions.placeholder = "Enter your custom instruction, directive, or question for the software architect...";
      }
      if (DOM.btnConfirmActionParam) {
        DOM.btnConfirmActionParam.disabled = true;
      }
      setTimeout(() => DOM.inputActionCustomInstructions && DOM.inputActionCustomInstructions.focus(), 60);
      break;
  }

  DOM.actionParamModalOverlay.style.display = "flex";
}

function closeActionParamModal() {
  DOM.actionParamModalOverlay.style.display = "none";
}

async function handleActionParamConfirm() {
  const actionType = state.selectedAction || "custom";
  const customInstructions = DOM.inputActionCustomInstructions ? DOM.inputActionCustomInstructions.value.trim() : "";

  const actionParams = {
    targetTaskId: state.targetTaskId,
    targetTaskTitle: state.targetTaskTitle,
    customInstructions: customInstructions
  };

  let prompt = "";

  if (actionType === "fix_bug") {
    const bugDesc = DOM.inputBugDescription ? DOM.inputBugDescription.value.trim() : "";
    const hasAttachment = DOM.inputBugAttachment && DOM.inputBugAttachment.files && DOM.inputBugAttachment.files.length > 0;
    if (!bugDesc && !hasAttachment) {
      if (DOM.bugValidationMsg) DOM.bugValidationMsg.style.display = "block";
      return;
    }
    if (DOM.bugValidationMsg) DOM.bugValidationMsg.style.display = "none";
    actionParams.bugDescription = bugDesc;
    if (hasAttachment) {
      actionParams.bugAttachment = DOM.inputBugAttachment.files[0].name;
    }
    prompt = `[ACTION: FIX_BUG]\nBug Report: ${bugDesc}`;
    if (hasAttachment) {
      prompt += `\nAttachment: ${DOM.inputBugAttachment.files[0].name}`;
    }
    if (customInstructions) {
      prompt += `\nAdditional Constraints: ${customInstructions}`;
    }
  } else if (actionType === "next_step") {
    const taskName = state.targetTaskTitle || "top pending task";
    const targetTask = state.targetTaskId ? state.planTree.find(t => t.id === state.targetTaskId) : null;
    if (targetTask && targetTask.is_ui) {
      actionParams.isUi = true;
      if (DOM.inputUiImageAttachment && DOM.inputUiImageAttachment.files && DOM.inputUiImageAttachment.files.length > 0) {
        actionParams.uiImagePath = DOM.inputUiImageAttachment.files[0].name;
      }
    }
    prompt = `[ACTION: EXECUTE_NEXT_STEP]\nTarget Task: ${taskName}`;
    if (actionParams.uiImagePath) {
      prompt += `\nUI Reference: ${actionParams.uiImagePath}`;
    }
    if (customInstructions) {
      prompt += `\nCustom Instructions: ${customInstructions}`;
    }
  } else if (actionType === "update_plan") {
    prompt = `[ACTION: UPDATE_PLAN]\nInstructions: ${customInstructions || "Review and update roadmap milestones"}`;
  } else if (actionType === "analyze") {
    prompt = `[ACTION: ANALYZE_CODEBASE]\nQuery: ${customInstructions || "Analyze overall workspace codebase and architecture"}`;
  } else if (actionType === "recommend") {
    prompt = `[ACTION: RECOMMEND_NEXT_STEPS]\nFocus: ${customInstructions || "Propose next strategic architectural steps"}`;
  } else {
    // Custom action: strictly require instructions
    if (!customInstructions) {
      if (DOM.customValidationMsg) DOM.customValidationMsg.style.display = "block";
      if (DOM.btnConfirmActionParam) DOM.btnConfirmActionParam.disabled = true;
      return;
    }
    if (DOM.customValidationMsg) DOM.customValidationMsg.style.display = "none";
    prompt = customInstructions;
  }

  closeActionParamModal();
  executeConfirmedTask(prompt, actionType, actionParams);
}

// ============================================================================
// Execution Lifecycle
// ============================================================================
async function executeConfirmedTask(promptText, actionType = "custom", actionParams = {}) {
  const text = promptText || state.pendingPrompt;
  if (!text) return;

  state.isExecuting = true;
  state.pendingPrompt = text;

  // Show dock stop button
  if (DOM.btnDockStop) DOM.btnDockStop.style.display = "inline-flex";

  // Pulse Action Dock
  if (DOM.actionDockCard) {
    DOM.actionDockCard.classList.remove("pulsing");
    void DOM.actionDockCard.offsetWidth;
    DOM.actionDockCard.classList.add("pulsing");
  }

  // Disable action buttons during run
  setActionButtonsDisabled(true);

  // Hide empty state and show execution stage
  DOM.emptyStateContainer.classList.add("hidden");
  setTimeout(() => {
    DOM.emptyStateContainer.style.display = "none";
    DOM.executionStage.style.display = "block";
  }, 200);

  DOM.architectStreamContent.innerHTML = "";
  DOM.coderStreamContent.innerHTML = "";
  DOM.architectSummary.style.display = "none";
  DOM.coderSummary.style.display = "none";
  DOM.architectErrorBadge.style.display = "none";
  DOM.coderErrorBadge.style.display = "none";

  DOM.btnCloseArchitect.disabled = true;
  DOM.btnCloseCoder.disabled = true;

  DOM.systemStatusDot.className = "status-dot running";
  DOM.systemStatusLabel.textContent = "Multi-Agent Active";

  if (window.pywebview && window.pywebview.api) {
    try {
      await window.pywebview.api.start_execution(text, actionType, actionParams);
    } catch (err) {
      console.error("[Execution] Start failed:", err);
      handleAgentEvent({
        type: "agent_error",
        agent: "software-architect",
        error: `Could not launch task: ${err.message}`,
        can_retry: true
      });
    }
  } else {
    simulateWorkflow(text);
  }
}

async function handleStopClick() {
  if (window.pywebview && window.pywebview.api) {
    try {
      await window.pywebview.api.stop_execution();
    } catch (err) {
      console.error("[Execution] Stop failed:", err);
    }
  }
  handleAgentEvent({
    type: "workflow_complete",
    status: "stopped",
    message: "Workflow stopped by user"
  });
}

function setActionButtonsDisabled(disabled) {
  const btns = document.querySelectorAll(".action-btn");
  btns.forEach(btn => {
    btn.disabled = disabled;
    btn.style.opacity = disabled ? "0.4" : "1";
    btn.style.pointerEvents = disabled ? "none" : "auto";
  });
}

function finalizeWorkflow(status) {
  state.isExecuting = false;

  if (DOM.btnDockStop) DOM.btnDockStop.style.display = "none";

  const hasTasks = state.planTree && state.planTree.length > 0;
  updateStrictPlanLock(!hasTasks);

  if (hasTasks) {
    setActionButtonsDisabled(false);
  }

  DOM.btnCloseArchitect.disabled = false;
  DOM.btnCloseCoder.disabled = false;

  DOM.systemStatusDot.className = "status-dot ready";
  DOM.systemStatusLabel.textContent = status === "stopped" ? "Halted" : "Ready";

  showToast(status === "stopped" ? "Task halted by user" : "Task concluded successfully!", status === "stopped" ? "info" : "success");
}

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

// ============================================================================
// Left Sidebar Dynamic Agent Navigation
// ============================================================================
function renderSidebarAgents() {
  DOM.sidebarMainAgentsList.innerHTML = "";
  DOM.sidebarCoderAgentsList.innerHTML = "";

  if (DOM.countMainAgents) DOM.countMainAgents.textContent = state.mainAgents.length;
  if (DOM.countCoderAgents) DOM.countCoderAgents.textContent = state.coderAgents.length;

  state.mainAgents.forEach((agent) => {
    const item = document.createElement("div");
    item.className = "agent-list-item";
    item.id = `navAgent_${agent.id}`;
    item.title = `${agent.display_name} - Click to view/edit system prompt`;
    item.innerHTML = `
      <div class="agent-item-icon main">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polygon points="12 2 2 7 12 12 22 7 12 2"/>
          <polyline points="2 17 12 22 22 17"/>
          <polyline points="2 12 12 17 22 12"/>
        </svg>
      </div>
      <div class="agent-item-info">
        <span class="agent-item-name">${agent.display_name}</span>
        <span class="agent-item-sub">Coordinator</span>
      </div>
      <span class="agent-status-indicator"></span>
    `;
    item.addEventListener("click", () => openPromptEditor(agent.id));
    DOM.sidebarMainAgentsList.appendChild(item);
  });

  state.coderAgents.forEach((agent) => {
    const item = document.createElement("div");
    item.className = "agent-list-item";
    item.id = `navAgent_${agent.id}`;
    item.title = `${agent.display_name} - Click to view/edit system prompt`;
    item.innerHTML = `
      <div class="agent-item-icon coder">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="16 18 22 12 16 6"/>
          <polyline points="8 6 2 12 8 18"/>
        </svg>
      </div>
      <div class="agent-item-info">
        <span class="agent-item-name">${agent.display_name}</span>
        <span class="agent-item-sub">Sub-Agent</span>
      </div>
      <span class="agent-status-indicator"></span>
    `;
    item.addEventListener("click", () => openPromptEditor(agent.id));
    DOM.sidebarCoderAgentsList.appendChild(item);
  });
}

// ============================================================================
// Agent Settings / Prompt Editor View
// ============================================================================
function openPromptEditor(agentId) {
  const all = [...state.mainAgents, ...state.coderAgents];
  const agent = all.find((a) => a.id === agentId);
  if (!agent) return;

  state.activeAgentForEditor = agent;

  DOM.editorAgentDisplayName.textContent = agent.display_name;
  DOM.editorRolePill.textContent = agent.type === "main" ? "Coordinator" : "Sub-Agent";
  DOM.editorRolePill.className = `role-pill ${agent.type === "main" ? "main-pill" : "coder-pill"}`;
  DOM.editorModelBadge.textContent = agent.model;
  DOM.editorFilePathBadge.textContent = agent.file_path ? agent.file_path.split(/[/\\]/).slice(-2).join("/") : "agents/";

  // Tiered Prompt Editor (Module 7):
  // Tier 1 – Protected Core Rules (read-only pre)
  if (DOM.txtCoreRulesPre) {
    DOM.txtCoreRulesPre.textContent = agent.core_prompt || agent.system_prompt || "";
  }
  // Tier 2 – Custom Developer Directives (editable textarea)
  DOM.txtSystemPrompt.value = agent.custom_instructions || "";

  // Reset core rules accordion to collapsed
  if (DOM.coreRulesContent) DOM.coreRulesContent.style.display = "none";
  if (DOM.lblCoreToggle) DOM.lblCoreToggle.textContent = "Show Protected Rules ▼";

  DOM.editorToolsList.innerHTML = "";
  (agent.tools || []).forEach(toolName => {
    const pill = document.createElement("span");
    pill.className = "tool-pill";
    pill.textContent = toolName;
    DOM.editorToolsList.appendChild(pill);
  });

  updateEditorMetrics();

  DOM.chatWorkspaceView.classList.remove("active");
  DOM.chatWorkspaceView.style.display = "none";
  DOM.promptEditorView.style.display = "flex";

  document.querySelectorAll(".agent-list-item").forEach((el) => el.classList.remove("active-agent"));
  const navItem = document.getElementById(`navAgent_${agent.id}`);
  if (navItem) navItem.classList.add("active-agent");
}

function closePromptEditor() {
  DOM.promptEditorView.style.display = "none";
  DOM.chatWorkspaceView.style.display = "flex";
  DOM.chatWorkspaceView.classList.add("active");
  document.querySelectorAll(".agent-list-item").forEach((el) => el.classList.remove("active-agent"));
}

function updateEditorMetrics() {
  const text = DOM.txtSystemPrompt.value;
  const lines = text.split("\n");
  DOM.promptCharCount.textContent = `${text.length} chars • ${lines.length} lines`;
  DOM.editorLineNumbers.innerHTML = lines.map((_, i) => i + 1).join("<br>");
}

async function saveCurrentSystemPrompt() {
  if (!state.activeAgentForEditor) return;
  const agentId = state.activeAgentForEditor.id;
  const newPrompt = DOM.txtSystemPrompt.value;

  try {
    let res;
    if (window.pywebview && window.pywebview.api) {
      // Tiered editing: only save custom directives, never overwrite core rules
      res = await window.pywebview.api.save_system_prompt(agentId, newPrompt, true);
    } else {
      res = { success: true, message: "Saved in preview mode" };
    }

    if (res.success) {
      state.activeAgentForEditor.custom_instructions = newPrompt;
      showToast("✓ Custom directives saved! Core architecture rules remain protected.", "success");
    } else {
      showToast(`Error: ${res.error || "Failed to save"}`, "error");
    }
  } catch (err) {
    showToast(`Error saving prompt: ${err.message}`, "error");
  }
}

// ============================================================================
// Event Listeners
// ============================================================================
function initEventListeners() {
  // Action Control Panel Intent Buttons
  if (DOM.btnActionFixBug) DOM.btnActionFixBug.addEventListener("click", () => openActionParamModal("fix_bug"));
  if (DOM.btnActionNextStep) DOM.btnActionNextStep.addEventListener("click", () => openActionParamModal("next_step"));
  if (DOM.btnActionUpdatePlan) DOM.btnActionUpdatePlan.addEventListener("click", () => openActionParamModal("update_plan"));
  if (DOM.btnActionAnalyze) DOM.btnActionAnalyze.addEventListener("click", () => openActionParamModal("analyze"));
  if (DOM.btnActionRecommend) DOM.btnActionRecommend.addEventListener("click", () => openActionParamModal("recommend"));
  if (DOM.btnActionCustom) DOM.btnActionCustom.addEventListener("click", () => openActionParamModal("custom"));

  // Dock Stop Button
  if (DOM.btnDockStop) DOM.btnDockStop.addEventListener("click", handleStopClick);

  // Action Parameter Modal Controls
  if (DOM.btnCloseParamModal) DOM.btnCloseParamModal.addEventListener("click", closeActionParamModal);
  if (DOM.btnCancelParamModal) DOM.btnCancelParamModal.addEventListener("click", closeActionParamModal);
  if (DOM.btnConfirmActionParam) DOM.btnConfirmActionParam.addEventListener("click", handleActionParamConfirm);

  // File upload input for Fix Bug
  if (DOM.inputBugAttachment) {
    DOM.inputBugAttachment.addEventListener("change", () => {
      if (DOM.inputBugAttachment.files && DOM.inputBugAttachment.files.length > 0) {
        DOM.lblAttachmentName.textContent = DOM.inputBugAttachment.files[0].name;
        if (DOM.bugValidationMsg) DOM.bugValidationMsg.style.display = "none";
      } else {
        DOM.lblAttachmentName.textContent = "No file attached";
      }
    });
  }

  // Live input validation for Custom Action instructions
  if (DOM.inputActionCustomInstructions) {
    DOM.inputActionCustomInstructions.addEventListener("input", () => {
      if (state.selectedAction === "custom") {
        const hasVal = DOM.inputActionCustomInstructions.value.trim().length > 0;
        if (DOM.btnConfirmActionParam) DOM.btnConfirmActionParam.disabled = !hasVal;
        if (hasVal && DOM.customValidationMsg) {
          DOM.customValidationMsg.style.display = "none";
        }
      }
    });
  }

  // Workspace folder button (Bug-free dialog closes on first click)
  DOM.btnSelectWorkspace.addEventListener("click", async () => {
    if (window.pywebview && window.pywebview.api) {
      try {
        const res = await window.pywebview.api.select_workspace();
        if (res && res.workspace_dir && !res.cancelled) {
          updateWorkspaceUI(res.workspace_dir);
          showToast(`Workspace set to: ${DOM.txtWorkspacePath.textContent}`, "success");
          const planData = await window.pywebview.api.get_active_plan();
          applyPlanData(planData);
        }
      } catch (err) {
        showToast("Error opening workspace dialog", "error");
      }
    } else {
      showToast("Workspace selector available in desktop app window", "info");
    }
  });

  // Plan Modals: Switch (arrows) and Create (+) are now separate
  DOM.btnSwitchPlan.addEventListener("click", openSwitchPlanModal);
  DOM.btnCreatePlanModal.addEventListener("click", openCreatePlanModal);
  DOM.btnCloseSwitchPlanModal.addEventListener("click", closeSwitchPlanModal);
  DOM.btnCloseCreatePlanModal.addEventListener("click", closeCreatePlanModal);
  DOM.btnSubmitCreatePlan.addEventListener("click", handleCreatePlanSubmit);

  // Card Close [X] Buttons
  DOM.btnCloseArchitect.addEventListener("click", () => {
    DOM.cardArchitect.style.display = "none";
    state.architectCardOpen = false;
    checkAllCardsClosed();
  });

  DOM.btnCloseCoder.addEventListener("click", () => {
    DOM.cardCoder.style.display = "none";
    state.coderCardOpen = false;
    if (state.architectCardOpen) {
      DOM.cardArchitect.style.flex = "1";
    }
    checkAllCardsClosed();
  });

  // Retry buttons
  DOM.btnRetryArchitect.addEventListener("click", () => {
    DOM.architectErrorBadge.style.display = "none";
    if (window.pywebview && window.pywebview.api) {
      window.pywebview.api.retry_execution("software-architect", state.pendingPrompt);
    }
  });

  DOM.btnRetryCoder.addEventListener("click", () => {
    DOM.coderErrorBadge.style.display = "none";
    if (window.pywebview && window.pywebview.api) {
      window.pywebview.api.retry_execution("coder", state.pendingPrompt);
    }
  });

  // Prompt Editor Navigation
  DOM.btnBackToChat.addEventListener("click", closePromptEditor);
  DOM.txtSystemPrompt.addEventListener("input", updateEditorMetrics);
  DOM.btnSavePrompt.addEventListener("click", saveCurrentSystemPrompt);
  DOM.btnResetPrompt.addEventListener("click", () => {
    if (state.activeAgentForEditor) {
      DOM.txtSystemPrompt.value = state.activeAgentForEditor.custom_instructions || "";
      updateEditorMetrics();
      showToast("Reset to loaded custom directives", "info");
    }
  });

  // Files Modal
  if (DOM.btnNavFiles) DOM.btnNavFiles.addEventListener("click", openWorkspaceFilesModal);
  if (DOM.btnCloseFilesModal) DOM.btnCloseFilesModal.addEventListener("click", closeWorkspaceFilesModal);
  if (DOM.btnCloseFilesModalFooter) DOM.btnCloseFilesModalFooter.addEventListener("click", closeWorkspaceFilesModal);
  if (DOM.inputSearchWorkspaceFiles) {
    DOM.inputSearchWorkspaceFiles.addEventListener("input", (e) => {
      renderFilteredWorkspaceFiles(e.target.value);
    });
  }
  if (DOM.filesModalOverlay) {
    DOM.filesModalOverlay.addEventListener("click", (e) => {
      if (e.target === DOM.filesModalOverlay) closeWorkspaceFilesModal();
    });
  }

  // ── Module 5: Codebase & Plan Sync Audit Modal ──
  if (DOM.btnAuditPlanSync) DOM.btnAuditPlanSync.addEventListener("click", openAuditModal);
  if (DOM.btnCloseAuditModal) DOM.btnCloseAuditModal.addEventListener("click", closeAuditModal);
  if (DOM.btnCloseAuditModalFooter) DOM.btnCloseAuditModalFooter.addEventListener("click", closeAuditModal);
  if (DOM.btnSyncPlanToCode) DOM.btnSyncPlanToCode.addEventListener("click", () => handleAuditResolution("plan_to_code"));
  if (DOM.btnForceCodeToPlan) DOM.btnForceCodeToPlan.addEventListener("click", () => handleAuditResolution("code_to_plan"));
  if (DOM.auditModalOverlay) {
    DOM.auditModalOverlay.addEventListener("click", (e) => {
      if (e.target === DOM.auditModalOverlay) closeAuditModal();
    });
  }

  // ── Module 6: Multimodal UI Vision Image Upload & Preview ──
  if (DOM.inputUiImageAttachment) {
    DOM.inputUiImageAttachment.addEventListener("change", () => {
      if (DOM.inputUiImageAttachment.files && DOM.inputUiImageAttachment.files.length > 0) {
        const file = DOM.inputUiImageAttachment.files[0];
        DOM.lblUiAttachmentName.textContent = file.name;
        const reader = new FileReader();
        reader.onload = (e) => {
          DOM.imgUiPreview.src = e.target.result;
          DOM.uiImagePreviewCard.style.display = "block";
        };
        reader.readAsDataURL(file);
      } else {
        DOM.lblUiAttachmentName.textContent = "No image selected";
        DOM.uiImagePreviewCard.style.display = "none";
      }
    });
  }
  if (DOM.btnRemoveUiPreview) {
    DOM.btnRemoveUiPreview.addEventListener("click", () => {
      DOM.inputUiImageAttachment.value = "";
      DOM.lblUiAttachmentName.textContent = "No image selected";
      DOM.imgUiPreview.src = "";
      DOM.uiImagePreviewCard.style.display = "none";
    });
  }

  // ── Module 7: Tiered Prompt Editor – Core Rules Accordion Toggle ──
  if (DOM.btnToggleCoreRules) {
    DOM.btnToggleCoreRules.addEventListener("click", () => {
      const isVisible = DOM.coreRulesContent.style.display !== "none";
      DOM.coreRulesContent.style.display = isVisible ? "none" : "block";
      DOM.lblCoreToggle.textContent = isVisible ? "Show Protected Rules ▼" : "Hide Protected Rules ▲";
    });
  }

  // ── Module 8: Rollback Modal ──
  if (DOM.btnCloseRollbackModal) DOM.btnCloseRollbackModal.addEventListener("click", closeRollbackModal);
  if (DOM.btnCancelRollback) DOM.btnCancelRollback.addEventListener("click", closeRollbackModal);
  if (DOM.chkRollbackConfirm) {
    DOM.chkRollbackConfirm.addEventListener("change", () => {
      DOM.btnConfirmRollbackAction.disabled = !DOM.chkRollbackConfirm.checked;
    });
  }
  if (DOM.btnConfirmRollbackAction) DOM.btnConfirmRollbackAction.addEventListener("click", handleConfirmRollback);
}

function checkAllCardsClosed() {
  if (!state.architectCardOpen && !state.coderCardOpen) {
    DOM.executionStage.style.display = "none";
    DOM.emptyStateContainer.style.display = "flex";
    DOM.emptyStateContainer.classList.remove("hidden");
  }
}

function initAutoScrollListeners() {
  DOM.architectLogs.addEventListener("scroll", () => {
    const el = DOM.architectLogs;
    state.autoScrollArchitect = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  });

  DOM.coderLogs.addEventListener("scroll", () => {
    const el = DOM.coderLogs;
    state.autoScrollCoder = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  });
}

// ============================================================================
// Two-Way Event Handler (window.onAgentEvent)
// ============================================================================
function handleAgentEvent(event) {
  if (!event || !event.type) return;

  switch (event.type) {
    case "workflow_started":
      break;

    case "architect_spawn":
      DOM.cardArchitect.style.display = "flex";
      DOM.cardArchitect.style.flex = "1";
      DOM.cardCoder.style.display = "none";
      state.architectCardOpen = true;
      state.coderCardOpen = false;
      DOM.architectStatusBadge.textContent = "Planning";
      DOM.architectStatusBadge.className = "status-badge";
      DOM.btnCloseArchitect.disabled = true;
      break;

    case "log":
      appendLog(event.agent, event.text, event.log_type);
      break;

    case "tool_call":
      appendToolCall(event.agent, event.tool, event.args, event.description);
      break;

    case "tool_result":
      appendToolResult(event.agent, event.tool, event.result);
      break;

    case "delegation":
      triggerDelegationAnimation(event.target_agent, event.target_name);
      break;

    case "coder_spawn":
      spawnCoderCard(event.agent, event.name, event.model);
      break;

    case "coder_summary":
      renderCardSummary(DOM.coderSummary, event.summary, false);
      DOM.coderStatusBadge.textContent = "Completed";
      DOM.coderStatusBadge.className = "status-badge coder-status completed";
      break;

    case "architect_summary":
      renderCardSummary(DOM.architectSummary, event.summary, true);
      DOM.architectStatusBadge.textContent = "Verified";
      DOM.architectStatusBadge.className = "status-badge completed";
      break;

    case "plan_updated":
      // Guard against stale events: if an active plan is already set and this event
      // carries a different filename, ignore it to prevent reverting a recent switch.
      if (state.activePlan && event.filename && event.filename !== state.activePlan) {
        console.warn(`[plan_updated] Ignoring stale event for "${event.filename}" (active: "${state.activePlan}")`);
        break;
      }
      applyPlanData(event);
      break;


    case "agent_error":
      renderErrorBadge(event.agent, event.error, event.can_retry);
      break;

    case "workflow_stopped":
    case "workflow_complete":
      finalizeWorkflow(event.status);
      break;

    case "workspace_changed":
      updateWorkspaceUI(event.workspace_dir);
      break;

    case "agents_updated":
      applyAgentsData(event.agents);
      break;
  }
}

function appendLog(agentId, text, logType = "thinking") {
  const isArchitect = agentId === "software-architect" || agentId.includes("architect");
  const targetContent = isArchitect ? DOM.architectStreamContent : DOM.coderStreamContent;
  const targetContainer = isArchitect ? DOM.architectLogs : DOM.coderLogs;
  const shouldAutoScroll = isArchitect ? state.autoScrollArchitect : state.autoScrollCoder;

  const span = document.createElement("span");
  if (logType === "decision") {
    span.className = "log-line-decision";
  }
  span.textContent = text;
  targetContent.appendChild(span);

  if (shouldAutoScroll) {
    targetContainer.scrollTop = targetContainer.scrollHeight;
  }
}

function appendToolCall(agentId, toolName, args, description) {
  const isArchitect = agentId === "software-architect" || agentId.includes("architect");
  const targetContent = isArchitect ? DOM.architectStreamContent : DOM.coderStreamContent;
  const targetContainer = isArchitect ? DOM.architectLogs : DOM.coderLogs;

  const toolDiv = document.createElement("div");
  toolDiv.className = "log-line-tool";
  let argDetail = "";
  if (args) {
    if (args.filename) argDetail = ` [${args.filename}]`;
    else if (args.command) argDetail = ` > ${args.command}`;
    else if (args.plan_file) argDetail = ` [${args.plan_file}]`;
  }
  toolDiv.innerHTML = `<strong>TOOL:</strong> ${escapeHtml(toolName)}${escapeHtml(argDetail)} — <em>${escapeHtml(description || "Executing")}</em>`;
  targetContent.appendChild(toolDiv);

  if (isArchitect ? state.autoScrollArchitect : state.autoScrollCoder) {
    targetContainer.scrollTop = targetContainer.scrollHeight;
  }
}

function appendToolResult(agentId, toolName, result) {
  const isArchitect = agentId === "software-architect" || agentId.includes("architect");
  const targetContent = isArchitect ? DOM.architectStreamContent : DOM.coderStreamContent;
  const targetContainer = isArchitect ? DOM.architectLogs : DOM.coderLogs;

  const resDiv = document.createElement("div");
  resDiv.style.color = "#10b981";
  resDiv.style.fontSize = "11px";
  resDiv.style.marginBottom = "6px";
  resDiv.textContent = `✓ ${result || "Done"}`;
  targetContent.appendChild(resDiv);

  if (isArchitect ? state.autoScrollArchitect : state.autoScrollCoder) {
    targetContainer.scrollTop = targetContainer.scrollHeight;
  }
}

function triggerDelegationAnimation(targetCoderId, targetName) {
  const navItem = document.getElementById(`navAgent_${targetCoderId}`);
  if (navItem) {
    navItem.classList.add("delegation-highlight");
    setTimeout(() => {
      navItem.classList.remove("delegation-highlight");
    }, 2200);
  }
  showToast(`Delegating task to ${targetName}...`, "info");
}

function spawnCoderCard(coderId, coderName, coderModel) {
  DOM.cardArchitect.style.flex = "1";
  DOM.cardCoder.style.display = "flex";
  DOM.cardCoder.style.flex = "1";
  state.coderCardOpen = true;

  DOM.coderCardName.textContent = coderName || "Coder Sub-Agent";
  DOM.coderCardModel.textContent = coderModel || "Full Shell Access";
  DOM.coderStatusBadge.textContent = "Implementing";
  DOM.coderStatusBadge.className = "status-badge coder-status";
  DOM.btnCloseCoder.disabled = true;

  DOM.cardCoder.style.animation = "fadeIn 0.35s ease forwards";
}

function renderCardSummary(container, summary, includeProposals = false) {
  if (!container || !summary) return;

  let proposalsHtml = "";
  if (includeProposals && summary.proposals && summary.proposals.length > 0) {
    proposalsHtml = `
      <div class="proposals-block">
        <div class="proposals-title">PROPOSED NEXT STEPS:</div>
        <ul class="proposals-list">
          ${summary.proposals.map(p => `<li>${escapeHtml(p)}</li>`).join("")}
        </ul>
      </div>
    `;
  }

  container.innerHTML = `
    <div class="summary-title">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
        <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>
        <polyline points="22 4 12 14.01 9 11.01"/>
      </svg>
      <span>${escapeHtml(summary.title || "Summary of Work")}</span>
    </div>
    <ul class="summary-list">
      ${(summary.deliverables || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}
    </ul>
    ${proposalsHtml}
  `;
  container.style.display = "block";
}

function renderErrorBadge(agentId, errorMsg, canRetry) {
  const isArchitect = agentId === "software-architect" || agentId.includes("architect");
  const badge = isArchitect ? DOM.architectErrorBadge : DOM.coderErrorBadge;
  const msgEl = isArchitect ? DOM.architectErrorMsg : DOM.coderErrorMsg;

  msgEl.textContent = errorMsg;
  badge.style.display = "flex";

  const statusBadge = isArchitect ? DOM.architectStatusBadge : DOM.coderStatusBadge;
  statusBadge.textContent = "Warning";
  statusBadge.style.borderColor = "#ef4444";
  statusBadge.style.color = "#fca5a5";
}

let cachedWorkspaceFiles = [];

async function openWorkspaceFilesModal() {
  if (DOM.modalFilesList) DOM.modalFilesList.innerHTML = "<div style='color: #64748b; font-size: 12px; padding: 20px; text-align: center;'>Loading workspace files...</div>";
  if (DOM.filesModalOverlay) DOM.filesModalOverlay.style.display = "flex";
  if (DOM.inputSearchWorkspaceFiles) DOM.inputSearchWorkspaceFiles.value = "";

  let files = [];
  if (window.pywebview && window.pywebview.api) {
    try {
      const info = await window.pywebview.api.get_workspace_info();
      files = info.files || [];
    } catch (e) {
      files = [];
    }
  } else {
    files = [
      { name: "main.py", path: "main.py", size: 4096 },
      { name: "PLAN.md", path: "PLAN.md", size: 1024 },
      { name: "PROGRESS.md", path: "PROGRESS.md", size: 1536 },
      { name: "test_main.py", path: "test_main.py", size: 2048 }
    ];
  }

  cachedWorkspaceFiles = files;
  renderFilteredWorkspaceFiles("");
  setTimeout(() => DOM.inputSearchWorkspaceFiles && DOM.inputSearchWorkspaceFiles.focus(), 60);
}

function renderFilteredWorkspaceFiles(query = "") {
  if (!DOM.modalFilesList) return;
  const q = (query || "").toLowerCase().trim();
  const filtered = q
    ? cachedWorkspaceFiles.filter(f => (f.path || f.name || "").toLowerCase().includes(q))
    : cachedWorkspaceFiles;

  if (DOM.txtFilesCountBadge) {
    DOM.txtFilesCountBadge.textContent = `${filtered.length} of ${cachedWorkspaceFiles.length} file(s)`;
  }

  if (filtered.length === 0) {
    DOM.modalFilesList.innerHTML = `<div style="color: #64748b; font-size: 13px; text-align: center; padding: 30px;">${q ? "No files match filter." : "No files in workspace yet."}</div>`;
    return;
  }

  // Cap initial render to 250 items to keep DOM instant and responsive
  const displayLimit = 250;
  const itemsToRender = filtered.slice(0, displayLimit);
  let html = itemsToRender.map(f => {
    const sizeStr = f.size ? `${(f.size / 1024).toFixed(1)} KB` : "0 KB";
    const filePath = escapeHtml(f.path || f.name);
    return `
      <div class="file-row">
        <span style="color: #38bdf8; font-family: var(--font-mono); font-size: 11.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${filePath}">📄 ${filePath}</span>
        <span style="color: #64748b; font-size: 10.5px; font-family: var(--font-mono); flex-shrink: 0;">${sizeStr}</span>
      </div>
    `;
  }).join("");

  if (filtered.length > displayLimit) {
    html += `<div style="color: #64748b; font-size: 11px; text-align: center; padding: 8px;">Showing first ${displayLimit} of ${filtered.length} files. Use filter search above for specific files.</div>`;
  }

  DOM.modalFilesList.innerHTML = html;
}

function closeWorkspaceFilesModal() {
  if (DOM.filesModalOverlay) DOM.filesModalOverlay.style.display = "none";
}

// ============================================================================
// Module 5: Codebase & Plan Sync Audit Modal
// ============================================================================
async function openAuditModal() {
  // Show modal immediately with scanning state
  DOM.auditModalOverlay.style.display = "flex";
  if (DOM.auditStatusPill) {
    DOM.auditStatusPill.textContent = "Scanning...";
    DOM.auditStatusPill.className = "audit-status-pill scanning";
  }
  if (DOM.auditStatusSummary) DOM.auditStatusSummary.textContent = "Analyzing workspace files vs plan.json...";
  if (DOM.listAuditMissing) DOM.listAuditMissing.innerHTML = "<em style='color:#64748b;font-size:11px;'>Loading...</em>";
  if (DOM.listAuditExisting) DOM.listAuditExisting.innerHTML = "<em style='color:#64748b;font-size:11px;'>Loading...</em>";
  if (DOM.badgeMissingFilesCount) DOM.badgeMissingFilesCount.textContent = "…";
  if (DOM.badgeExistingFilesCount) DOM.badgeExistingFilesCount.textContent = "…";
  if (DOM.txtUntrackedFiles) DOM.txtUntrackedFiles.textContent = "Scanning...";

  // Disable resolution buttons until results are loaded
  if (DOM.btnSyncPlanToCode) DOM.btnSyncPlanToCode.disabled = true;
  if (DOM.btnForceCodeToPlan) DOM.btnForceCodeToPlan.disabled = true;

  try {
    let audit;
    if (window.pywebview && window.pywebview.api) {
      audit = await window.pywebview.api.audit_codebase_sync();
    } else {
      // Fallback demo data
      audit = {
        in_sync: false,
        discrepancy_count: 2,
        completed_missing_files: [
          { task_id: "task-2", title: "Scaffold project environment", section: "1. Architecture & Setup", missing_files: ["main.py"] }
        ],
        pending_existing_files: [
          { task_id: "task-3", title: "Build weather service REST endpoints", section: "2. Core Implementation", existing_files: ["weather_api.py"] }
        ],
        untracked_files: ["README.md", "requirements.txt"],
        summary: "Detected 2 discrepancy(ies): 1 completed task(s) missing deliverables, 1 pending task(s) already have deliverables."
      };
    }

    if (audit.error) {
      showToast(`Audit error: ${audit.error}`, "error");
      closeAuditModal();
      return;
    }

    renderAuditResults(audit);
  } catch (err) {
    showToast(`Audit failed: ${err.message}`, "error");
    closeAuditModal();
  }
}

function renderAuditResults(audit) {
  // Status pill
  if (DOM.auditStatusPill) {
    if (audit.in_sync) {
      DOM.auditStatusPill.textContent = "In Sync ✓";
      DOM.auditStatusPill.className = "audit-status-pill in-sync";
    } else {
      DOM.auditStatusPill.textContent = `${audit.discrepancy_count} Discrepancies`;
      DOM.auditStatusPill.className = "audit-status-pill discrepancy";
    }
  }
  if (DOM.auditStatusSummary) DOM.auditStatusSummary.textContent = audit.summary || "";

  // Column 1: Completed tasks with missing files
  const missingItems = audit.completed_missing_files || [];
  if (DOM.badgeMissingFilesCount) DOM.badgeMissingFilesCount.textContent = missingItems.length;
  if (DOM.listAuditMissing) {
    if (missingItems.length === 0) {
      DOM.listAuditMissing.innerHTML = "<span style='color:#64748b;font-size:11px;'>No completed tasks with missing deliverables.</span>";
    } else {
      DOM.listAuditMissing.innerHTML = missingItems.map(item => `
        <div class="audit-item">
          <div class="audit-item-title">${escapeHtml(item.title)}</div>
          <div class="audit-item-section">${escapeHtml(item.section || "")}</div>
          <div class="audit-item-files">${(item.missing_files || []).map(f => `<code>${escapeHtml(f)}</code>`).join(", ")}</div>
        </div>
      `).join("");
    }
  }

  // Column 2: Pending tasks with existing files
  const existingItems = audit.pending_existing_files || [];
  if (DOM.badgeExistingFilesCount) DOM.badgeExistingFilesCount.textContent = existingItems.length;
  if (DOM.listAuditExisting) {
    if (existingItems.length === 0) {
      DOM.listAuditExisting.innerHTML = "<span style='color:#64748b;font-size:11px;'>No pending tasks with pre-existing code.</span>";
    } else {
      DOM.listAuditExisting.innerHTML = existingItems.map(item => `
        <div class="audit-item">
          <div class="audit-item-title">${escapeHtml(item.title)}</div>
          <div class="audit-item-section">${escapeHtml(item.section || "")}</div>
          <div class="audit-item-files">${(item.existing_files || []).map(f => `<code>${escapeHtml(f)}</code>`).join(", ")}</div>
        </div>
      `).join("");
    }
  }

  // Untracked files
  const untracked = audit.untracked_files || [];
  if (DOM.txtUntrackedFiles) {
    if (untracked.length === 0) {
      DOM.txtUntrackedFiles.innerHTML = "<span style='color:#64748b;'>None</span>";
    } else {
      const maxShow = 40;
      const preview = untracked.slice(0, maxShow).map(f => `<code>${escapeHtml(f)}</code>`).join(" ");
      const extra = untracked.length > maxShow ? ` <span style='color:#64748b; font-size:10.5px;'>...and ${untracked.length - maxShow} more</span>` : "";
      DOM.txtUntrackedFiles.innerHTML = preview + extra;
    }
  }

  // Enable resolution buttons only if there are discrepancies
  if (DOM.btnSyncPlanToCode) DOM.btnSyncPlanToCode.disabled = audit.in_sync;
  if (DOM.btnForceCodeToPlan) DOM.btnForceCodeToPlan.disabled = audit.in_sync;
}

async function handleAuditResolution(resolutionType) {
  if (DOM.btnSyncPlanToCode) DOM.btnSyncPlanToCode.disabled = true;
  if (DOM.btnForceCodeToPlan) DOM.btnForceCodeToPlan.disabled = true;

  const label = resolutionType === "plan_to_code" ? "Syncing plan to codebase" : "Forcing code to match plan";
  showToast(`${label}...`, "info");

  try {
    let res;
    if (window.pywebview && window.pywebview.api) {
      res = await window.pywebview.api.resolve_sync(resolutionType);
    } else {
      res = { success: true, message: `${label} completed (preview mode)` };
    }

    if (res.success !== false) {
      showToast(`✓ ${label} completed successfully!`, "success");
      // Plan data will be refreshed via plan_updated event from backend
      closeAuditModal();
    } else {
      showToast(`Resolution failed: ${res.error || "Unknown error"}`, "error");
      if (DOM.btnSyncPlanToCode) DOM.btnSyncPlanToCode.disabled = false;
      if (DOM.btnForceCodeToPlan) DOM.btnForceCodeToPlan.disabled = false;
    }
  } catch (err) {
    showToast(`Resolution error: ${err.message}`, "error");
    if (DOM.btnSyncPlanToCode) DOM.btnSyncPlanToCode.disabled = false;
    if (DOM.btnForceCodeToPlan) DOM.btnForceCodeToPlan.disabled = false;
  }
}

function closeAuditModal() {
  DOM.auditModalOverlay.style.display = "none";
}

// ============================================================================
// Module 8: Rollback Modal (Severe Confirmation)
// ============================================================================
let rollbackTargetTask = null;

function openRollbackModal(task) {
  rollbackTargetTask = task;

  // Populate task title
  if (DOM.rollbackTargetTitle) DOM.rollbackTargetTitle.textContent = task.title || "Unknown Task";

  // Populate affected deliverables list
  if (DOM.rollbackFilesList) {
    const files = task.files || [];
    // Also extract from details
    const detailFiles = [];
    if (task.details) {
      task.details.forEach(d => {
        const found = d.match(/`([^`]+\.[a-zA-Z0-9]+)`/g);
        if (found) found.forEach(f => detailFiles.push(f.replace(/`/g, "")));
      });
    }
    const allFiles = [...new Set([...files, ...detailFiles])];

    if (allFiles.length === 0) {
      DOM.rollbackFilesList.innerHTML = "<li style='color:#64748b;'>No specific deliverables recorded for this task.</li>";
    } else {
      DOM.rollbackFilesList.innerHTML = allFiles.map(f =>
        `<li><code>${escapeHtml(f)}</code></li>`
      ).join("");
    }
  }

  // Reset confirmation state
  if (DOM.chkRollbackConfirm) DOM.chkRollbackConfirm.checked = false;
  if (DOM.btnConfirmRollbackAction) DOM.btnConfirmRollbackAction.disabled = true;

  DOM.rollbackModalOverlay.style.display = "flex";
}

function closeRollbackModal() {
  DOM.rollbackModalOverlay.style.display = "none";
  rollbackTargetTask = null;
}

async function handleConfirmRollback() {
  if (!rollbackTargetTask) return;

  const taskId = rollbackTargetTask.id;
  const taskTitle = rollbackTargetTask.title;

  // Disable button to prevent double-click
  if (DOM.btnConfirmRollbackAction) DOM.btnConfirmRollbackAction.disabled = true;

  showToast(`Rolling back "${taskTitle}"...`, "info");

  try {
    let res;
    if (window.pywebview && window.pywebview.api) {
      res = await window.pywebview.api.rollback_task(taskId);
    } else {
      // Fallback: simulate rollback in local state
      const step = state.planTree.find(t => t.id === taskId);
      if (step) step.status = "pending";
      renderPlanTree();
      updateNextStepButtonPreview();
      res = { success: true };
    }

    if (res.success) {
      const restoredMsg = (res.restored_files && res.restored_files.length > 0)
        ? ` Restored ${res.restored_files.length} file(s).`
        : "";
      showToast(`✓ "${taskTitle}" rolled back to Pending.${restoredMsg}`, "success");
      closeRollbackModal();
      // Plan will be refreshed via plan_updated event from backend
    } else {
      showToast(`Rollback failed: ${res.error || "Unknown error"}`, "error");
      if (DOM.btnConfirmRollbackAction) DOM.btnConfirmRollbackAction.disabled = false;
    }
  } catch (err) {
    showToast(`Rollback error: ${err.message}`, "error");
    if (DOM.btnConfirmRollbackAction) DOM.btnConfirmRollbackAction.disabled = false;
  }
}

function showToast(message, type = "info") {
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

function initOrbAnimation() {
  const canvas = document.getElementById("orbCanvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  const cx = w / 2;
  const cy = h / 2;
  const radius = 88;

  let angle = 0;

  function render() {
    angle += 0.012;
    ctx.clearRect(0, 0, w, h);

    ctx.save();
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.clip();

    ctx.fillStyle = "#030408";
    ctx.fill();

    const grad1 = ctx.createLinearGradient(
      cx + Math.cos(angle) * 70,
      cy + Math.sin(angle) * 70,
      cx - Math.cos(angle) * 70,
      cy - Math.sin(angle) * 70
    );
    grad1.addColorStop(0, "rgba(56, 189, 248, 0.95)");
    grad1.addColorStop(0.25, "rgba(147, 51, 234, 0.9)");
    grad1.addColorStop(0.5, "rgba(236, 72, 153, 0.95)");
    grad1.addColorStop(0.75, "rgba(249, 115, 22, 0.85)");
    grad1.addColorStop(1, "rgba(37, 99, 235, 0.9)");

    ctx.fillStyle = grad1;
    ctx.beginPath();
    ctx.ellipse(
      cx + Math.sin(angle * 1.5) * 14,
      cy + Math.cos(angle * 1.2) * 14,
      radius * 0.95,
      radius * 0.75,
      angle * 0.8,
      0,
      Math.PI * 2
    );
    ctx.fill();

    const grad2 = ctx.createRadialGradient(
      cx + Math.cos(angle * 0.7) * 35,
      cy + Math.sin(angle * 0.7) * 35,
      8,
      cx,
      cy,
      radius
    );
    grad2.addColorStop(0, "rgba(255, 255, 255, 0.85)");
    grad2.addColorStop(0.2, "rgba(96, 165, 250, 0.8)");
    grad2.addColorStop(0.55, "rgba(10, 15, 30, 0.92)");
    grad2.addColorStop(0.85, "rgba(192, 132, 252, 0.75)");
    grad2.addColorStop(1, "rgba(6, 182, 212, 0.4)");

    ctx.fillStyle = grad2;
    ctx.beginPath();
    ctx.arc(cx, cy, radius * 0.92, 0, Math.PI * 2);
    ctx.fill();

    ctx.beginPath();
    ctx.moveTo(cx - 50, cy + 30);
    ctx.bezierCurveTo(
      cx - 20 + Math.sin(angle * 2) * 20,
      cy - 60 + Math.cos(angle) * 20,
      cx + 40 + Math.cos(angle * 1.8) * 20,
      cy - 10 + Math.sin(angle) * 15,
      cx + 60,
      cy + 40
    );
    ctx.strokeStyle = "rgba(255, 255, 255, 0.35)";
    ctx.lineWidth = 14;
    ctx.lineCap = "round";
    ctx.filter = "blur(6px)";
    ctx.stroke();
    ctx.filter = "none";

    ctx.restore();

    ctx.save();
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.strokeStyle = "rgba(255, 255, 255, 0.18)";
    ctx.lineWidth = 1.5;
    ctx.stroke();
    ctx.restore();

    requestAnimationFrame(render);
  }

  render();
}

function simulateWorkflow(prompt) {
  setTimeout(() => {
    handleAgentEvent({ type: "architect_spawn" });
    handleAgentEvent({ type: "log", agent: "software-architect", text: `> Processing directive: "${prompt}"\n> Anchoring state via ${state.activePlan} (plan.json)...\n` });
  }, 400);

  setTimeout(() => {
    handleAgentEvent({ type: "log", agent: "software-architect", text: "> Evaluated complexity: Core Architecture.\n> Gatekeeper Routing: Spawning 'Senior Backend Coder' (coder-deep).\n", log_type: "decision" });
    handleAgentEvent({ type: "delegation", target_agent: "coder-deep", target_name: "Senior Backend Coder" });
  }, 1400);

  setTimeout(() => {
    handleAgentEvent({ type: "coder_spawn", agent: "coder-deep", name: "Senior Backend Coder", model: "Full Shell Access" });
    handleAgentEvent({ type: "log", agent: "coder-deep", text: `> Sub-agent active.\n> Inspecting ${state.activePlan} requirements...\n` });
  }, 2200);

  setTimeout(() => {
    handleAgentEvent({ type: "tool_call", agent: "coder-deep", tool: "execute_shell_command", args: { command: "python --version" }, description: "Checking runtime" });
    handleAgentEvent({ type: "tool_result", agent: "coder-deep", tool: "execute_shell_command", result: "Python 3.12.3" });
    handleAgentEvent({ type: "tool_call", agent: "coder-deep", tool: "write_file", args: { filename: "weather_api.py" }, description: "Writing code" });
    handleAgentEvent({ type: "tool_result", agent: "coder-deep", tool: "write_file", result: "Created weather_api.py" });
    handleAgentEvent({ type: "coder_summary", agent: "coder-deep", summary: { title: "Sub-Agent Implementation Complete", deliverables: ["Created weather_api.py", "Added automated tests", `Logged milestone to plan.json and ${state.activePlan}`] } });
  }, 3200);

  setTimeout(() => {
    handleAgentEvent({ type: "architect_summary", agent: "software-architect", summary: { title: "Lead Architect Verification & Handoff", deliverables: [`Audited standardized updates in ${state.activePlan}`, "Syntax and interfaces validated"], proposals: ["Integrate live external API provider", "Add persistent database storage", "Containerize service with Docker"] } });
    handleAgentEvent({ type: "workflow_complete", status: "completed" });
  }, 4200);
}
