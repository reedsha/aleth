// ui/js/wire.js — DOM event wiring and application bootstrap. Loaded last.

// Surfaces a startup failure that would otherwise be invisible: the packaged app
// runs with debug=False, so an exception here leaves a fully rendered window with
// no click handlers at all ("open, but nothing responds") and no way to tell why.
import { abortRunUi, beginRunUi, closeActionDrawer, finalizeWorkflow, handleActionParamConfirm, handleStopClick, openActionDrawer } from "./actions.js";
import { hideTaggingPanel, showTaggingPanel } from "./agent-events.js";
import { closePromptEditor, saveCurrentSystemPrompt, updateEditorMetrics } from "./agents.js";
import { applyAgentsData, initFallbackMode, onPyWebViewReady, startEventStream, updateWorkspaceUI } from "./bootstrap.js";
import { on as onBus } from "./bus.js";
import { initCodeSurfaces } from "./code-surface.js";
import { initCommandPalette, openCommandPalette } from "./command-palette.js";
import { clearConsole, detachConsole, initTerminalConsole, toggleConsole, toggleConsoleOverlay } from "./console.js";
import { initDockResize } from "./dock.js";
import { initDOMElements } from "./dom.js";
import { initEnvironmentPanel } from "./env.js";
import { reportStartupFailure, showToast } from "./notify.js";
import { closeCreatePlanModal, closeNormalizeGateModal, closeSwitchPlanModal, handleCreatePlanSubmit, handleNormalizeGateConfirm, openCreatePlanModal, openSwitchPlanModal } from "./plan-modals.js";
import { applyPlanData } from "./plan-tree.js";
import { closePreview, initPreview, refreshPreview, togglePreview } from "./preview.js";
import { closeResultView, handleResultViewClick, initResultView, openResultView } from "./result-view.js";
import { initSettingsPanel } from "./settings.js";
import { initSidebars, openPlansTab, renderSidebarPlans, setSidebarTab, switchActivePlan } from "./sidebar.js";
import { DOM, setAgentCardOpen, state, unresolvedElements } from "./store.js";
import { simulateWorkflow } from "./visuals.js";
import { closeDagPanel, toggleDagPanel } from "./dag.js";
import { editTaskInWorkbench, handleWorkbenchDiscard, handleWorkbenchEdit, handleWorkbenchInput, handleWorkbenchShowDag, handleWorkbenchShowRaw, handleWorkbenchShowTree, initWorkbench, refreshWorkbenchChrome, renderPlanDocument, syncWorkbenchGutterScroll } from "./workbench.js";
import { closeAuditModal, closeRollbackModal, closeWorkspaceFilesModal, handleAuditResolution, handleConfirmRollback, openAuditModal, openPlanSummaryPanel, openRollbackModal, openWorkspaceFilesModal, renderFilteredWorkspaceFiles } from "./workspace.js";

// ---------------------------------------------------------------------------
// Bus subscriptions -- the composition layer
// ---------------------------------------------------------------------------
// Feature renderers that would otherwise import a *caller* publish an intent on the bus
// instead (see ui/js/bus.js): plan-tree opens the action drawer, the console folds the
// drawer away, plan-modals arms and unwinds the run lock, and the event handler finalizes a
// run and applies agent/workspace payloads. Each entry below maps an intent to its real
// implementation. The subscriptions are registered here, at module top level, so they exist
// before any event can fire -- wire.js is imported last by main.js, after every module whose
// functions are named here.
//
// This is the only place that knows the wiring; the modules on the other side of the bus do
// not import it, or each other, for these operations.
onBus("action:open", (payload) => {
  const { kind, ...opts } = payload || {};
  openActionDrawer(kind, opts);
});
onBus("action:close-drawer", closeActionDrawer);
onBus("run:stop", handleStopClick);
onBus("run:begin", beginRunUi);
onBus("run:abort", (payload) => abortRunUi((payload && payload.message) || ""));
onBus("run:finalize", (payload) => finalizeWorkflow(payload && payload.status));
onBus("run:simulate", simulateWorkflow);
onBus("agents:apply", applyAgentsData);
onBus("workspace:update", updateWorkspaceUI);
onBus("modal:create-plan", openCreatePlanModal);
onBus("modal:rollback", openRollbackModal);
onBus("sidebar:render-plans", renderSidebarPlans);
onBus("workbench:edit-task", editTaskInWorkbench);
onBus("workbench:refresh-chrome", refreshWorkbenchChrome);
onBus("workbench:render-document", renderPlanDocument);
// The DAG asks for the tree view when it takes focus (ui/js/dag.js): the review surface has
// no business being behind the raw-markdown view.
onBus("workbench:show-tree", handleWorkbenchShowTree);

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
export function on(element, event, handler) {
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
  runStartupStep("Preview", initPreview);
  runStartupStep("Command palette", initCommandPalette);
  runStartupStep("Result view", initResultView);
  runStartupStep("Sidebar panels", initSidebars);
  runStartupStep("Environment panel", initEnvironmentPanel);
  runStartupStep("Settings panel", initSettingsPanel);
  // The engine event stream, over HTTP. Started before the bridge settles because it does not
  // depend on it: the gateway is an ordinary server, and this is the path every event takes
  // once the pywebview push is gone.
  runStartupStep("Event stream", startEventStream);

  // Every element the app caches must have resolved. This replaces the check the document
  // head used to make against a hand-maintained list of ~35 window globals: it asks the
  // cache the wiring actually reads, so it cannot fall out of step with the markup, and it
  // names the missing elements rather than only counting them.
  const unresolved = unresolvedElements();
  if (unresolved.length > 0) {
    reportStartupFailure(`missing DOM elements: ${unresolved.join(", ")}`);
  }

  // PyWebView Bridge initialization. A single settle path decides, exactly once, whether
  // the bridge is there or not. pywebview injects its bridge after this script has run, so
  // either it announces itself with `pywebviewready`, or it does not. Whatever arrives
  // first wins and settles the decision; the other path -- the event and the one fallback
  // timer -- can never run its step a second time. This replaces two timers racing.
  if (window.pywebview) {
    runStartupStep("Bridge startup", onPyWebViewReady);
  } else {
    let bridgeSettled = false;
    const settleBridge = (label, step) => {
      if (bridgeSettled) return;
      bridgeSettled = true;
      runStartupStep(label, step);
    };
    window.addEventListener("pywebviewready", () => {
      settleBridge("Bridge startup", onPyWebViewReady);
    });
    setTimeout(() => {
      // If the event was missed but the bridge is present, treat it as ready; otherwise
      // this is the one explicit fallback path, and it cannot double-fire.
      if (window.pywebview) settleBridge("Bridge startup", onPyWebViewReady);
      else settleBridge("Bridge fallback", initFallbackMode);
    }, 400);
  }
});
// ============================================================================
// Event Listeners
// ============================================================================
function initEventListeners() {
  // Action Control Panel Intent Buttons
  // Command palette: the launcher that replaced the dock's six intent buttons.
  on(DOM.btnCommandPalette, "click", openCommandPalette);

  // Run Stop, now in the top bar (it used to sit in the dock's status row).
  if (DOM.btnStopRun) DOM.btnStopRun.addEventListener("click", handleStopClick);

  // Action Drawer Controls
  if (DOM.btnCloseParamModal) DOM.btnCloseParamModal.addEventListener("click", closeActionDrawer);
  if (DOM.btnCancelParamModal) DOM.btnCancelParamModal.addEventListener("click", closeActionDrawer);
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
      } catch (_err) {
        showToast("Error opening workspace dialog", "error");
      }
    } else {
      showToast("Workspace selector available in desktop app window", "info");
    }
  });

  // Plan Modals: Switch and Create are now separate. Plan selection lives in the left sidebar's
  // Plans tab (wired with the other tabs below); the switcher modal is reached from the tab's own
  // expand button, so its fresh-fetch path is kept without being the primary entry point.
  on(DOM.btnExpandSwitchPlan, "click", openSwitchPlanModal);
  if (DOM.sidebarPlansList) {
    DOM.sidebarPlansList.addEventListener("click", (e) => {
      const chip = e.target.closest(".plan-chip");
      if (chip && chip.dataset.plan) switchActivePlan(chip.dataset.plan);
    });
  }
  on(DOM.btnCreatePlanModal, "click", openCreatePlanModal);
  on(DOM.btnCloseSwitchPlanModal, "click", closeSwitchPlanModal);
  on(DOM.btnCloseCreatePlanModal, "click", closeCreatePlanModal);
  on(DOM.btnSubmitCreatePlan, "click", handleCreatePlanSubmit);

  // Normalization Gate (an imported .md the plan parser cannot read)
  on(DOM.btnCloseNormalizeGate, "click", closeNormalizeGateModal);
  on(DOM.btnSkipNormalize, "click", closeNormalizeGateModal);
  on(DOM.btnConfirmNormalize, "click", handleNormalizeGateConfirm);

  // Plan UI re-tagging. The backend call returns immediately (it starts a background
  // thread), so the panel is shown here and then fed by the laya_tagging_* events.
  if (DOM.btnRetagUi) DOM.btnRetagUi.addEventListener("click", handleRetagUiClick);
  if (DOM.btnCloseTagging) DOM.btnCloseTagging.addEventListener("click", hideTaggingPanel);

  // Card Close [X] Buttons
  on(DOM.btnCloseArchitect, "click", () => {
    DOM.cardArchitect.style.display = "none";
    setAgentCardOpen("architect", false);
    checkAllCardsClosed();
  });

  on(DOM.btnCloseCoder, "click", () => {
    DOM.cardCoder.style.display = "none";
    setAgentCardOpen("coder", false);
    if (state.architectCardOpen) {
      DOM.cardArchitect.style.flex = "1";
    }
    checkAllCardsClosed();
  });

  // Retry buttons
  // Retry starts a real workflow (retry_execution -> start_execution), so it arms the same
  // lock a normal launch does, disables itself while in flight, and surfaces a refusal or a
  // rejection. The fire-and-forget version left unhandled rejections and could queue runs
  // with no Stop ever shown (audit H7).
  const wireRetry = (btn, badge, agentId) => {
    on(btn, "click", async () => {
      if (badge) badge.style.display = "none";
      if (!(window.pywebview && window.pywebview.api)) {
        showToast("Retry is available in the desktop app window", "info");
        return;
      }
      if (state.isExecuting) {
        showToast("A run is already in progress.", "info");
        return;
      }
      btn.disabled = true;
      beginRunUi();
      try {
        const res = await window.pywebview.api.retry_execution(agentId, state.pendingPrompt);
        if (res && res.success === false) abortRunUi(res.error || "A run is already in progress.");
      } catch (err) {
        abortRunUi(`Retry failed: ${(err && err.message) || err}`);
      } finally {
        btn.disabled = false;
      }
    });
  };
  wireRetry(DOM.btnRetryArchitect, DOM.architectErrorBadge, "software-architect");
  wireRetry(DOM.btnRetryCoder, DOM.coderErrorBadge, "coder");

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

  // Left-sidebar tabs. Each button is its own listener rather than one delegated handler, so
  // a missing tab costs only that tab (see `on`).
  on(DOM.tabSidebarAgents, "click", () => setSidebarTab("agents"));
  // Plans goes through openPlansTab() rather than setSidebarTab() so the one entry point that can
  // also be reached from a collapsed rail still expands the pane before revealing the tab.
  on(DOM.tabSidebarPlans, "click", openPlansTab);
  on(DOM.tabSidebarFiles, "click", () => setSidebarTab("files"));
  on(DOM.tabSidebarEnv, "click", () => setSidebarTab("env"));

  // Files: the modal is reached from the sidebar Files tab's own expand button (it stays in the
  // DOM, it is just not the primary entry point).
  on(DOM.btnExpandFilesModal, "click", openWorkspaceFilesModal);
  if (DOM.btnCloseFilesModal) DOM.btnCloseFilesModal.addEventListener("click", closeWorkspaceFilesModal);
  if (DOM.btnCloseFilesModalFooter) DOM.btnCloseFilesModalFooter.addEventListener("click", closeWorkspaceFilesModal);
  if (DOM.inputSearchWorkspaceFiles) {
    let filterTimer = null;
    DOM.inputSearchWorkspaceFiles.addEventListener("input", (e) => {
      // Debounced: this re-filters and re-renders up to 250 rows per keystroke (audit M11).
      clearTimeout(filterTimer);
      const value = e.target.value;
      filterTimer = setTimeout(() => renderFilteredWorkspaceFiles(value), 150);
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

  // ── Plan Workbench: the active plan rendered as a read-only document ──
  on(DOM.btnWorkbenchEdit, "click", handleWorkbenchEdit);
  on(DOM.btnWorkbenchDiscard, "click", handleWorkbenchDiscard);
  on(DOM.planEditorInput, "input", handleWorkbenchInput);
  on(DOM.planEditorInput, "scroll", syncWorkbenchGutterScroll);
  on(DOM.btnWorkbenchTreeView, "click", handleWorkbenchShowTree);
  on(DOM.btnWorkbenchRawMd, "click", handleWorkbenchShowRaw);
  // The bento summary tile's "Expand Detail" opens the right rail in summary mode rather
  // than expanding in place, so the dashboard's fixed grid is never distorted.
  on(DOM.btnBentoExpandSummary, "click", openPlanSummaryPanel);

  // ── Polymorphic result view (the centre stage's per-action output) ──
  on(DOM.btnCloseResultView, "click", closeResultView);
  on(DOM.btnWorkbenchResult, "click", openResultView);
  // Every renderer rewrites the body wholesale, so the buttons inside it (file chips,
  // "Add to Plan") are handled by delegation instead of being re-attached per render.
  on(DOM.resultBody, "click", handleResultViewClick);

  // ── Plan workbench views ──
  if (DOM.btnWorkbenchDagView) DOM.btnWorkbenchDagView.addEventListener("click", handleWorkbenchShowDag);

  // ── Artifact DAG rail panel ──
  if (DOM.btnDagPanel) DOM.btnDagPanel.addEventListener("click", toggleDagPanel);
  if (DOM.btnCloseDagPanel) DOM.btnCloseDagPanel.addEventListener("click", closeDagPanel);

  // ── Dock console ──
  on(DOM.btnConsoleToggle, "click", toggleConsole);
  on(DOM.btnConsoleClear, "click", clearConsole);
  on(DOM.btnConsoleOverlay, "click", toggleConsoleOverlay);
  on(DOM.btnConsoleDetach, "click", detachConsole);

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

// Fire-and-forget re-tagging of the plan's [UI] tags. The bridge method returns at once
// and the pass streams its progress back as laya_tagging_* events, so the panel opens
// before the call is even made and there is nothing here to await or catch.
function handleRetagUiClick() {
  showTaggingPanel();
  if (window.pywebview && window.pywebview.api) {
    // The pass returns at once and streams laya_tagging_* events; the button is disabled
    // until the done event so a second click cannot start a second pass, and a rejected
    // call re-enables it rather than leaving a permanent spinner (audit M10/B1.2).
    if (DOM.btnRetagUi) DOM.btnRetagUi.disabled = true;
    window.pywebview.api.retag_plan_with_laya().catch((err) => {
      if (DOM.btnRetagUi) DOM.btnRetagUi.disabled = false;
      hideTaggingPanel();
      showToast(`Re-tagging failed: ${(err && err.message) || err}`, "error");
    });
  } else {
    showToast("Re-tagging is available in the desktop app window", "info");
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
