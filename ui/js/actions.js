// ui/js/actions.js — Action drawer, confirm flow, and execution lifecycle.
// ============================================================================
// Action Drawer Flow (Critical Rule: All Actions Prompt for Params)
// ============================================================================
import { handleAgentEvent } from "./agent-events.js";
import { api } from "./api-client.js";
import { repaintCodeSurfaces } from "./code-surface.js";
import { clearTokenBurn } from "./console.js";
import { engineAvailable } from "./connection.js";
import { isDockDrawerOpen, setDockDrawerOpen } from "./dock.js";
import { followActiveRun, leaveRun } from "./live-run.js";
import { showToast } from "./notify.js";
import { isUiTask, renderPlanTree, updateBentoStats, updateStrictPlanLock } from "./plan-tree.js";
import { refreshPreview } from "./preview.js";
import { resetResultView } from "./result-view.js";
import { setHtml, setText } from "./safe-dom.js";
import { DOM, setActiveIntentId, state } from "./store.js";

// Focusing a field the drawer just revealed cannot happen in the same task: the panel has
// no box until layout runs, so a bare focus() is silently dropped. This used to be six
// separate 60 ms timeouts -- a fixed guess that raced the layout. The next animation frame
// runs after style and layout, before paint, so the field receives focus the moment it is
// focusable. Falls back to an immediate focus where rAF does not exist.
function focusAfterLayout(field) {
  if (!field || typeof field.focus !== "function") return;
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => field.focus());
  else field.focus();
}

export function openActionDrawer(actionType, extraParams = {}) {
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
  // Cleared on every open: it is set again below only for the plan-wide request.
  state.runWholePlan = false;

  // Reset inputs. `prefill` lets a caller hand the drawer a directive the user is asked to
  // confirm before it runs. No caller uses it today: the result view's "Add to Plan" now
  // writes through the bridge's `add_plan_task` and never opens this drawer. The hook is
  // kept for a future caller that wants the Update Plan form's confirm step.
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
      focusAfterLayout(DOM.inputBugDescription);
      break;

    // Braced on purpose: a `const` declared directly in a `case` block belongs to the
    // whole `switch`, so `targetTask` would be in scope (and in the temporal dead zone)
    // for every later case. A block keeps it local to this one.
    case "next_step": {
      DOM.paramModalIcon.textContent = "\u26A1";
      DOM.paramModalTitle.textContent = "Execute Next Step";
      DOM.paramModalSubtitle.textContent = "Milestone Task Implementation";
      DOM.paramActionTypeBadge.textContent = "Architect Gatekeeper -> Coder";
      DOM.paramPresetDescription.textContent = "Architect evaluates requirements against the active plan, prepares workspace context, and delegates task implementation to a specialized Coder.";
      DOM.btnConfirmActionText.textContent = "Execute Step";

      // An explicit target is a task card's own Execute: one node. With none, this is the
      // plan-wide request (Phase 18) -- the engine resolves the DAG's runnable nodes itself and
      // stops only at the approval gate. A [UI] task is the exception: it needs its reference
      // mockup, which only a targeted run collects, so it keeps the old single-node behaviour.
      const explicitTarget = Boolean(extraParams.targetTaskId);
      const selected = explicitTarget
        ? state.planTree.find(t => t.id === state.targetTaskId)
        : state.planTree.find(t => t.status === "pending");
      const targetTask = explicitTarget || (selected && isUiTask(selected)) ? selected : null;

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
      } else {
        state.targetTaskId = null;
        state.targetTaskTitle = null;
        state.runWholePlan = true;
        DOM.paramTargetBadge.textContent = "Full Plan";
        DOM.paramTargetTitle.textContent = "All runnable milestones, stopping at approval";
        DOM.paramTargetTaskCard.style.display = "flex";
      }
      focusAfterLayout(DOM.inputActionCustomInstructions);
      break;
    }

    case "update_plan":
      DOM.paramModalIcon.textContent = "✏️";
      DOM.paramModalTitle.textContent = "Update / Edit Plan";
      DOM.paramModalSubtitle.textContent = "Machine State Refinement (No Coder)";
      DOM.paramActionTypeBadge.textContent = "Administrative Bypass";
      DOM.paramPresetDescription.textContent = "Architect directly refines plan.json tasks and milestones based on your input, immediately compiling changes into PLAN.md.";
      DOM.btnConfirmActionText.textContent = "Update Plan";
      focusAfterLayout(DOM.inputActionCustomInstructions);
      break;

    case "analyze":
      DOM.paramModalIcon.textContent = "🔍";
      DOM.paramModalTitle.textContent = "Analyze Codebase";
      DOM.paramModalSubtitle.textContent = "Architect Architecture Inspection (No Coder)";
      DOM.paramActionTypeBadge.textContent = "Direct Architect Query";
      DOM.paramPresetDescription.textContent = "Architect inspects workspace files, checks interfaces and constraints, and produces a thorough structural report.";
      DOM.btnConfirmActionText.textContent = "Run Analysis";
      focusAfterLayout(DOM.inputActionCustomInstructions);
      break;

    case "recommend":
      DOM.paramModalIcon.textContent = "💡";
      DOM.paramModalTitle.textContent = "Get Recommendation";
      DOM.paramModalSubtitle.textContent = "Architect Strategic Guidance (No Coder)";
      DOM.paramActionTypeBadge.textContent = "Advisory Loop";
      DOM.paramPresetDescription.textContent = "Architect reviews current milestone completion in plan.json and workspace code to propose strategic next steps.";
      DOM.btnConfirmActionText.textContent = "Get Recommendations";
      focusAfterLayout(DOM.inputActionCustomInstructions);
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
        setHtml(DOM.lblActionCustomInstructions, 'CUSTOM DIRECTIVE / INSTRUCTION <span class="required-star">*Required</span>');
      }
      if (DOM.inputActionCustomInstructions) {
        DOM.inputActionCustomInstructions.placeholder = "Enter your custom instruction, directive, or question for the software architect...";
      }
      if (DOM.btnConfirmActionParam) {
        DOM.btnConfirmActionParam.disabled = true;
      }
      focusAfterLayout(DOM.inputActionCustomInstructions);
      break;
  }

  setDockDrawerOpen(true, actionType);
  // The fields were reset above while the drawer was hidden, and assigning .value raises no
  // event, so the highlighted copy that sits behind each field has to be rebuilt by hand.
  if (typeof repaintCodeSurfaces === "function") repaintCodeSurfaces();
}

export function closeActionDrawer() {
  setDockDrawerOpen(false);
}

// The webview sandbox cannot be read by the backend, so an attached log has to be handed
// over with the request. Capped so a huge file cannot bloat the prompt; a binary file yields
// "" and remains a named reference only.
const MAX_ATTACHMENT_CHARS = 8000;

async function readAttachmentText(file) {
  try {
    if (!file || typeof file.text !== "function") return "";
    const text = await file.text();
    return text.length > MAX_ATTACHMENT_CHARS
      ? text.slice(0, MAX_ATTACHMENT_CHARS) + "\n... [attachment truncated]"
      : text;
  } catch (_err) {
    return "";
  }
}

// Reads an attached image as a data URL for the bridge, so a UI mockup can actually be sent to
// a vision-capable model rather than only named. Resolves to "" when the read fails, which
// leaves the text-only request in place.
function readImageDataUrl(file) {
  return new Promise((resolve) => {
    try {
      if (!file || typeof FileReader === "undefined") return resolve("");
      const reader = new FileReader();
      reader.onload = (e) => resolve((e && e.target && e.target.result) || "");
      reader.onerror = () => resolve("");
      reader.readAsDataURL(file);
    } catch (_err) {
      resolve("");
    }
  });
}

export async function handleActionParamConfirm() {
  const actionType = state.selectedAction || "custom";
  const customInstructions = DOM.inputActionCustomInstructions ? DOM.inputActionCustomInstructions.value.trim() : "";

  const actionParams = {
    targetTaskId: state.targetTaskId,
    targetTaskTitle: state.targetTaskTitle,
    customInstructions: customInstructions
  };

  // Assigned on every branch below, including the final `else`, so there is no initialiser to
  // carry a value nothing reads.
  let prompt;
  // The intent the engine is asked for. It differs from the action only for an un-targeted
  // "Execute Next Step": that is the plan-wide request now, so the engine drives the whole DAG
  // (Phase 18) instead of stopping after one node. A task card's own Execute still targets one
  // node and still submits `next_step`.
  let dispatchType = actionType;

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
      const attachmentFile = DOM.inputBugAttachment.files[0];
      actionParams.bugAttachment = attachmentFile.name;
      // Hand the backend the log/report's *text*, not just its name: the name was only a
      // truthiness gate, so attached evidence never reached the diagnosis (audit H8). Empty
      // for a binary image, which stays a named reference.
      actionParams.bugAttachmentContent = await readAttachmentText(attachmentFile);
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
        const imageFile = DOM.inputUiImageAttachment.files[0];
        actionParams.uiImagePath = imageFile.name;
        // The backend cannot read the webview sandbox, so the bytes cross the bridge as a data
        // URL and become a real vision input on the Coder's model request.
        actionParams.uiImageData = await readImageDataUrl(imageFile);
      }
    }
    prompt = `[ACTION: EXECUTE_NEXT_STEP]\nTarget Task: ${taskName}`;
    if (actionParams.uiImagePath) {
      prompt += `\nUI Reference: ${actionParams.uiImagePath}`;
    }
    if (customInstructions) {
      prompt += `\nCustom Instructions: ${customInstructions}`;
    }
    // No specific task: run the plan. The engine resolves and runs the DAG's runnable nodes and
    // stops only where a person is needed (an artifact awaiting approval).
    if (state.runWholePlan) dispatchType = "execute_plan";
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
  executeConfirmedTask(prompt, dispatchType, actionParams);
}

// ============================================================================
// Execution Lifecycle
// ============================================================================

// The client-side half of the run lock: the state the UI shows while a run is live. Extracted
// so the retry, normalize and reload-recovery paths arm the *same* state a normal launch does
// -- each starts a real backend run, and previously showed "Idle" with no Stop (audit H5/H6/H7).
export function beginRunUi() {
  state.isExecuting = true;
  // Reveal Stop in the top bar for the duration of the run, and Interrupt beside it: a run that can
  // be halted but not steered is half a control (Phase 34).
  if (DOM.btnStopRun) DOM.btnStopRun.style.display = "inline-flex";
  if (DOM.btnInterruptRun) DOM.btnInterruptRun.style.display = "inline-flex";
  // A new run starts a new bill, so the previous run's burn counter goes with it.
  clearTokenBurn();
  // The bento header's live tile reports the run state, so it moves with it.
  if (typeof updateBentoStats === "function") updateBentoStats();

  // Pulse Action Dock
  if (DOM.actionDockCard) {
    DOM.actionDockCard.classList.remove("pulsing");
    void DOM.actionDockCard.offsetWidth;
    DOM.actionDockCard.classList.add("pulsing");
  }

  setActionButtonsDisabled(true);

  // The plan cards key their inline Stop on run state, so they are redrawn when it flips
  // (audit M16). A re-render preserves each card's expanded/collapsed state.
  if (typeof renderPlanTree === "function") renderPlanTree();
}

// Undoes beginRunUi when a launch did not actually start -- the backend refused because a run
// was already live, or the call rejected. Without it the UI kept a Stop button and a
// "Multi-Agent Active" label for a run that never began (audit H7).
export function abortRunUi(message) {
  state.isExecuting = false;
  if (DOM.btnStopRun) DOM.btnStopRun.style.display = "none";
  if (DOM.btnInterruptRun) DOM.btnInterruptRun.style.display = "none";
  // A launch that did not start has no run to follow, so the window returns to the firehose and
  // drops any run-scoped surface (Phase 34).
  leaveRun();
  const hasTasks = state.planTree && state.planTree.length > 0;
  updateStrictPlanLock(!hasTasks);
  if (hasTasks) setActionButtonsDisabled(false);
  DOM.btnCloseArchitect.disabled = false;
  DOM.btnCloseCoder.disabled = false;
  DOM.systemStatusDot.className = "status-dot ready";
  DOM.systemStatusLabel.textContent = "Ready";
  if (typeof updateBentoStats === "function") updateBentoStats();
  // A launch that did not happen takes the inline Stop back down with it.
  if (typeof renderPlanTree === "function") renderPlanTree();
  if (message) showToast(message, "error");
}

async function executeConfirmedTask(promptText, actionType = "custom", actionParams = {}) {
  const text = promptText || state.pendingPrompt;
  if (!text) return;

  // The last line of defence before a run starts. The confirm button is disabled while the
  // engine is unreachable (ui/js/connection.js), but a keyboard path or a stale click can still
  // arrive here, and an intent submitted into a dead engine is a run the user believes started.
  if (!engineAvailable()) {
    showToast("The engine is unreachable. Reconnect before starting a run.", "error");
    return;
  }

  state.pendingPrompt = text;
  beginRunUi();

  // A new run supersedes whatever the last one left on screen, including the payloads the
  // result view holds, so the previous result cannot be reopened over this one.
  if (typeof resetResultView === "function") resetResultView();

  // Hide empty state and show execution stage
  DOM.emptyStateContainer.classList.add("hidden");
  setTimeout(() => {
    DOM.emptyStateContainer.style.display = "none";
    DOM.executionStage.style.display = "block";
  }, 200);

  setText(DOM.architectStreamContent, "");
  setText(DOM.coderStreamContent, "");
  DOM.architectSummary.style.display = "none";
  DOM.coderSummary.style.display = "none";
  DOM.architectErrorBadge.style.display = "none";
  DOM.coderErrorBadge.style.display = "none";

  DOM.btnCloseArchitect.disabled = true;
  DOM.btnCloseCoder.disabled = true;

  DOM.systemStatusDot.className = "status-dot running";
  DOM.systemStatusLabel.textContent = "Multi-Agent Active";

  try {
    const res = await api.start_execution(text, actionType, actionParams);
    // The engine answers with the execution id it accepted the run under. The shadow the run
    // writes into is keyed to it (Phase 21), so the UI keeps it to name that run again -- an
    // approval, the merge review, or an interrupt. A staged change with no id is a change no one
    // can act on.
    const intentId = (res && res.intent_id) || null;
    setActiveIntentId(intentId);
    // ...and the window now follows *that* run's stream, so the operator sees its thoughts and can
    // steer it without a second connection to the firehose (Phase 34).
    followActiveRun(intentId);
  } catch (err) {
    console.error("[Execution] Start failed:", err);
    // A launch that rejected never produced a terminal event, so the lock has to be undone
    // here or the UI stays "running" forever.
    abortRunUi(`Could not launch task: ${(err && err.message) || err}`);
    handleAgentEvent({
      type: "agent_error",
      agent: "software-architect",
      error: `Could not launch task: ${err.message}`
    });
  }
}

export async function handleStopClick() {
  // The terminal event is the backend's to send: the runner emits `workflow_complete`
  // (status `stopped`) when the run actually unwinds, and `stop_execution` emits one if no
  // run is live. Fabricating it here flipped the UI to "Halted" while the backend kept
  // writing files, so the two disagreed about whether the run was over.
  try {
    await api.stop_execution();
    showToast("Stopping the active run\u2026", "info");
  } catch (err) {
    // Invisible in the packaged app (debug=False): the user believes the run is stopping
    // when the halt request actually failed (audit H10).
    showToast(`Could not stop the run: ${(err && err.message) || err}`, "error");
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

export function finalizeWorkflow(status) {
  state.isExecuting = false;

  if (DOM.btnStopRun) DOM.btnStopRun.style.display = "none";
  if (DOM.btnInterruptRun) DOM.btnInterruptRun.style.display = "none";
  // The run is over: the window returns to the firehose and clears the steering surface, so a
  // later run does not inherit this one's pause (Phase 34).
  leaveRun();

  const hasTasks = state.planTree && state.planTree.length > 0;
  updateStrictPlanLock(!hasTasks);

  if (hasTasks) {
    setActionButtonsDisabled(false);
  }

  DOM.btnCloseArchitect.disabled = false;
  DOM.btnCloseCoder.disabled = false;

  // The runner reports a crash as `workflow_complete(status="error")`; treating every
  // non-stopped status as success reported a failed run as "Task concluded successfully!"
  // (audit H3). A halt and a crash each get their own label.
  const failed = status === "error";
  DOM.systemStatusDot.className = failed ? "status-dot failed" : "status-dot ready";
  DOM.systemStatusLabel.textContent =
    status === "stopped" ? "Halted" : (failed ? "Run Failed" : "Ready");

  // A run that rewrote the interface should not leave a stale one on screen. The preview
  // only reads the file from disk, so re-reading it can never be wrong, whatever the run
  // ended up doing.
  if (state.previewOpen && typeof refreshPreview === "function") {
    refreshPreview();
  }

  if (status === "stopped") showToast("Task halted by user", "info");
  else if (failed) showToast("The run failed \u2014 see the console for details.", "error");
  else showToast("Task concluded successfully!", "success");
  // The run is over, which is a fact the bento header's live tile states.
  if (typeof updateBentoStats === "function") updateBentoStats();
  // The inline Stop hangs off run state, so the tree is redrawn when the run ends.
  if (typeof renderPlanTree === "function") renderPlanTree();
}
