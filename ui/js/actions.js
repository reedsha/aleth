// ui/js/actions.js — Action drawer, confirm flow, and execution lifecycle.
// ============================================================================
// Action Drawer Flow (Critical Rule: All Actions Prompt for Params)
// ============================================================================
function openActionDrawer(actionType, extraParams = {}) {
  if (state.isExecuting) return;
  if (!DOM.actionDrawerPanel) return;

  // A second click on the toolbar button whose form is already open folds the drawer away
  // rather than resetting the fields that were typed into it. A call that names a task (a
  // task card's own Execute button) is not a repeat of the toolbar button, so it always
  // opens onto that task.
  if (!extraParams.targetTaskId && isDockDrawerOpen() && state.selectedAction === actionType) {
    closeActionDrawer();
    return;
  }

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
        DOM.paramTargetBadge.textContent = isUiTask(targetTask) ? "UI Task" : "Pending";
        DOM.paramTargetTitle.textContent = targetTask.title;
        DOM.paramTargetTaskCard.style.display = "flex";

        // Multimodal UI Vision Section (Module 6)
        if (isUiTask(targetTask) && DOM.paramUiVisionSection) {
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

  setDockDrawerOpen(true, actionType);
  // The fields were reset above while the drawer was hidden, and assigning .value raises no
  // event, so the highlighted copy that sits behind each field has to be rebuilt by hand.
  if (typeof repaintCodeSurfaces === "function") repaintCodeSurfaces();
}

function closeActionDrawer() {
  setDockDrawerOpen(false);
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
    if (targetTask && isUiTask(targetTask)) {
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

  closeActionDrawer();
  executeConfirmedTask(prompt, actionType, actionParams);
}

// ============================================================================
// Execution Lifecycle
// ============================================================================
// Which directory of snapshots a run's file edits land in. fix_bug and custom snapshot
// under fixed keys, next_step under the target task's id, and the three read-only intents
// (update_plan, analyze, recommend) write no files at all, so there is nothing to show.
// The fixed keys are shared across runs of the same intent, so their pane reflects the
// most recent run that wrote there rather than one particular run.
function diffKeyForRun(actionType, actionParams) {
  if (actionType === "next_step") {
    return (actionParams && actionParams.targetTaskId) || state.targetTaskId || null;
  }
  if (actionType === "fix_bug") return "bugfix";
  if (actionType === "custom") return "custom";
  return null;
}

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

  // The stage splits now so the tracked-edits pane is in place while the agents work; it
  // is filled in when they finish, because the snapshots only exist once files are written.
  // Without the bridge (the browser layout preview) there is nothing to read, so the pane
  // is left closed rather than opened onto an error.
  state.lastRunDiffKey = diffKeyForRun(actionType, actionParams);
  if (state.lastRunDiffKey && window.pywebview && window.pywebview.api) {
    openDiffPane(state.lastRunDiffKey, true);
  } else {
    state.lastRunDiffKey = null;
    closeDiffPane();
  }

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

  // Leave the run's real file edits on screen the moment it stops, so what changed is
  // visible in the stage rather than only as a summary line on a card.
  if (status !== "stopped" && status !== "error" && state.lastRunDiffKey &&
      window.pywebview && window.pywebview.api) {
    openDiffPane(state.lastRunDiffKey);
  }

  // A run that rewrote the interface should not leave a stale one on screen. The preview
  // only reads the file from disk, so re-reading it can never be wrong, whatever the run
  // ended up doing.
  if (state.previewOpen && typeof refreshPreview === "function") {
    refreshPreview();
  }

  showToast(status === "stopped" ? "Task halted by user" : "Task concluded successfully!", status === "stopped" ? "info" : "success");
}
