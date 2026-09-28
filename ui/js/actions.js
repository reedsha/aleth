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

  // Reset inputs. `prefill` lets a caller hand the drawer a directive it is asking the
  // user to confirm -- the result view's "Add to Plan" routes a proposal through this same
  // Update Plan form rather than through a second, thinner endpoint.
  if (DOM.inputActionCustomInstructions) DOM.inputActionCustomInstructions.value = extraParams.prefill || "";
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
      // A fix opened from a failed card names that task and seeds the description with the
      // recorded failure, so the drawer shows what it is fixing rather than an empty form.
      if (state.targetTaskId) {
        if (DOM.inputBugDescription && extraParams.bugPrefill) {
          DOM.inputBugDescription.value = extraParams.bugPrefill;
        }
        DOM.paramTargetBadge.textContent = "Failed Task";
        DOM.paramTargetTitle.textContent = state.targetTaskTitle || state.targetTaskId;
        DOM.paramTargetTaskCard.style.display = "flex";
      }
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

async function executeConfirmedTask(promptText, actionType = "custom", actionParams = {}) {
  const text = promptText || state.pendingPrompt;
  if (!text) return;

  state.isExecuting = true;
  state.pendingPrompt = text;

  // Reveal Stop in the top bar for the duration of the run
  if (DOM.btnStopRun) DOM.btnStopRun.style.display = "inline-flex";
  // The bento header's live tile reports the run state, so it moves with it.
  if (typeof updateBentoStats === "function") updateBentoStats();

  // Pulse Action Dock
  if (DOM.actionDockCard) {
    DOM.actionDockCard.classList.remove("pulsing");
    void DOM.actionDockCard.offsetWidth;
    DOM.actionDockCard.classList.add("pulsing");
  }

  // Disable action buttons during run
  setActionButtonsDisabled(true);

  // A new run supersedes whatever the last one left on screen, including the payloads the
  // result view holds, so the previous result cannot be reopened over this one.
  if (typeof resetResultView === "function") resetResultView();

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
  // The terminal event is the backend's to send: the runner emits `workflow_complete`
  // (status `stopped`) when the run actually unwinds, and `stop_execution` emits one if no
  // run is live. Fabricating it here flipped the UI to "Halted" while the backend kept
  // writing files, so the two disagreed about whether the run was over.
  if (window.pywebview && window.pywebview.api) {
    try {
      await window.pywebview.api.stop_execution();
      showToast("Stopping the active run\u2026", "info");
    } catch (err) {
      console.error("[Execution] Stop failed:", err);
    }
  } else {
    handleAgentEvent({
      type: "workflow_complete",
      status: "stopped",
      message: "Workflow stopped by user"
    });
  }
}

// The six dock buttons are gone, so querying .action-btn would silently no-op and a
// second action could be launched mid-run. The palette items and its trigger are the same
// controls now, so the run lock has to reach them instead.
function setActionButtonsDisabled(disabled) {
  const controls = document.querySelectorAll(".palette-item, #btnCommandPalette");
  controls.forEach(el => {
    el.disabled = disabled;
    el.style.opacity = disabled ? "0.4" : "1";
    el.style.pointerEvents = disabled ? "none" : "auto";
  });
}

function finalizeWorkflow(status) {
  state.isExecuting = false;

  if (DOM.btnStopRun) DOM.btnStopRun.style.display = "none";

  const hasTasks = state.planTree && state.planTree.length > 0;
  updateStrictPlanLock(!hasTasks);

  if (hasTasks) {
    setActionButtonsDisabled(false);
  }

  DOM.btnCloseArchitect.disabled = false;
  DOM.btnCloseCoder.disabled = false;

  DOM.systemStatusDot.className = "status-dot ready";
  DOM.systemStatusLabel.textContent = status === "stopped" ? "Halted" : "Ready";

  // A run that rewrote the interface should not leave a stale one on screen. The preview
  // only reads the file from disk, so re-reading it can never be wrong, whatever the run
  // ended up doing.
  if (state.previewOpen && typeof refreshPreview === "function") {
    refreshPreview();
  }

  showToast(status === "stopped" ? "Task halted by user" : "Task concluded successfully!", status === "stopped" ? "info" : "success");
  // The run is over, which is a fact the bento header's live tile states.
  if (typeof updateBentoStats === "function") updateBentoStats();
}
