// ui/js/wire.js — DOM event wiring and application bootstrap. Loaded last.

// Surfaces a startup failure that would otherwise be invisible: the packaged app
// runs with debug=False, so an exception here leaves a fully rendered window with
// no click handlers at all ("open, but nothing responds") and no way to tell why.
function reportStartupFailure(message) {
  console.error("[DeepAgents UI]", message);
  // The document head installs a reporter before any module runs. Sharing it keeps
  // every failure in one stack of banners instead of several overlapping at the same
  // position, where all but the last message would be unreadable.
  if (typeof window !== "undefined" && typeof window.__deepAgentsReport === "function") {
    window.__deepAgentsReport(message);
    return;
  }
  try {
    const banner = document.createElement("div");
    banner.textContent = "[DeepAgents UI] " + message;
    banner.style.cssText = "position:fixed;left:12px;right:12px;bottom:12px;z-index:99999;" +
      "padding:10px 14px;background:#7f1d1d;color:#fff;font:12px/1.5 monospace;" +
      "border-radius:8px;white-space:pre-wrap;pointer-events:none";
    (document.body || document.documentElement).appendChild(banner);
  } catch (_err) {
    /* the console line above is the last resort */
  }
}

// One failing startup step must never take the remaining steps down with it. Failures
// are normalised into one message shape, and an async step's rejection is caught too:
// the window "error" listener never sees promise rejections, so the bridge handshake
// could otherwise fail in complete silence.
function runStartupStep(label, step) {
  const fail = (err) => reportStartupFailure(`${label} failed: ${(err && err.message) || err}`);
  try {
    const result = step();
    if (result && typeof result.then === "function") result.then(undefined, fail);
  } catch (err) {
    fail(err);
  }
}

// Wiring must never abort part-way. These calls used to be bare
// `DOM.x.addEventListener(...)`, so a single element that was missing or had not been
// cached skipped every listener below it, leaving a rendered but inert window with no
// indication of which control was responsible.
function on(element, event, handler) {
  if (!element || typeof element.addEventListener !== "function") {
    reportStartupFailure(`cannot wire "${event}": element is missing`);
    return;
  }
  element.addEventListener(event, handler);
}

document.addEventListener("DOMContentLoaded", () => {
  // Interactivity is wired before anything that renders content. The workbench renders
  // the whole active plan, so a plan the parser chokes on used to be able to throw here
  // before the listeners below were registered -- leaving the window rendered but inert.
  // Each step is isolated, so the workbench failing costs nothing but the workbench.
  runStartupStep("DOM lookup", initDOMElements);
  runStartupStep("Event wiring", initEventListeners);
  runStartupStep("Auto-scroll wiring", initAutoScrollListeners);
  runStartupStep("Dock resize", initDockResize);
  runStartupStep("Plan workbench", initWorkbench);
  runStartupStep("Code surfaces", initCodeSurfaces);
  runStartupStep("Terminal console", initTerminalConsole);
  runStartupStep("Diff pane", initDiffPane);
  runStartupStep("Live preview", initPreview);
  runStartupStep("Sidebar panels", initSidebars);
  runStartupStep("Environment panel", initEnvironmentPanel);

  // PyWebView Bridge initialization
  if (window.pywebview) {
    runStartupStep("Bridge startup", onPyWebViewReady);
  } else {
    // pywebview injects its bridge after this script has run, so the event can beat the
    // timer. Whichever wins, the other is cancelled: a bridge that arrives promptly must
    // not also run the fallback path, which now only reports or seeds opt-in demo data.
    const fallbackTimer = setTimeout(() => {
      if (!window.pywebview) {
        runStartupStep("Bridge fallback", initFallbackMode);
      }
    }, 400);
    window.addEventListener("pywebviewready", () => {
      clearTimeout(fallbackTimer);
      runStartupStep("Bridge startup", onPyWebViewReady);
    });
  }
});
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
  on(DOM.btnSelectWorkspace, "click", async () => {
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
  on(DOM.btnSwitchPlan, "click", openSwitchPlanModal);
  on(DOM.btnCreatePlanModal, "click", openCreatePlanModal);
  on(DOM.btnCloseSwitchPlanModal, "click", closeSwitchPlanModal);
  on(DOM.btnCloseCreatePlanModal, "click", closeCreatePlanModal);
  on(DOM.btnSubmitCreatePlan, "click", handleCreatePlanSubmit);

  // Card Close [X] Buttons
  on(DOM.btnCloseArchitect, "click", () => {
    DOM.cardArchitect.style.display = "none";
    state.architectCardOpen = false;
    checkAllCardsClosed();
  });

  on(DOM.btnCloseCoder, "click", () => {
    DOM.cardCoder.style.display = "none";
    state.coderCardOpen = false;
    if (state.architectCardOpen) {
      DOM.cardArchitect.style.flex = "1";
    }
    checkAllCardsClosed();
  });

  // Retry buttons
  on(DOM.btnRetryArchitect, "click", () => {
    DOM.architectErrorBadge.style.display = "none";
    if (window.pywebview && window.pywebview.api) {
      window.pywebview.api.retry_execution("software-architect", state.pendingPrompt);
    }
  });

  on(DOM.btnRetryCoder, "click", () => {
    DOM.coderErrorBadge.style.display = "none";
    if (window.pywebview && window.pywebview.api) {
      window.pywebview.api.retry_execution("coder", state.pendingPrompt);
    }
  });

  // Prompt Editor Navigation
  on(DOM.btnBackToChat, "click", closePromptEditor);
  on(DOM.txtSystemPrompt, "input", updateEditorMetrics);
  on(DOM.btnSavePrompt, "click", saveCurrentSystemPrompt);
  on(DOM.btnResetPrompt, "click", () => {
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

  // ── Plan Workbench: the active plan as a read/write document ──
  on(DOM.btnWorkbenchEdit, "click", handleWorkbenchEdit);
  on(DOM.btnWorkbenchSave, "click", handleWorkbenchSave);
  on(DOM.btnWorkbenchDiscard, "click", handleWorkbenchDiscard);
  on(DOM.planEditorInput, "input", handleWorkbenchInput);
  on(DOM.planEditorInput, "scroll", syncWorkbenchGutterScroll);

  // ── Dock console & tracked-edits pane ──
  on(DOM.btnConsoleClear, "click", clearConsole);
  on(DOM.btnDiffPaneClose, "click", closeDiffPane);

  // ── Live preview ──
  on(DOM.btnTogglePreview, "click", togglePreview);
  on(DOM.btnPreviewReload, "click", refreshPreview);
  on(DOM.btnPreviewRetry, "click", refreshPreview);
  on(DOM.btnPreviewClose, "click", closePreview);
}

function checkAllCardsClosed() {
  if (!state.architectCardOpen && !state.coderCardOpen) {
    DOM.executionStage.style.display = "none";
    DOM.emptyStateContainer.style.display = "flex";
    DOM.emptyStateContainer.classList.remove("hidden");
  }
}

function initAutoScrollListeners() {
  on(DOM.architectLogs, "scroll", () => {
    const el = DOM.architectLogs;
    state.autoScrollArchitect = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  });

  on(DOM.coderLogs, "scroll", () => {
    const el = DOM.coderLogs;
    state.autoScrollCoder = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  });
}
