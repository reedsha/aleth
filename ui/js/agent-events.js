// ui/js/agent-events.js — Inbound agent event stream renderer.
// ============================================================================
// Inbound Event Handler (invoked by ui/js/bridge-bus.js, the strict bus sink)
// ============================================================================
import { emit } from "./bus.js";
import { blockOnEngineFailure } from "./connection.js";
import { echoAgentEvent, hideConsole, revealConsoleForAttention, showConsole } from "./console.js";
import { escapeHtml } from "./dom.js";
import { showToast } from "./notify.js";
import { applyPlanData, updateBentoStats } from "./plan-tree.js";
import { mountResultView } from "./result-view.js";
import { setHtml } from "./safe-dom.js";
import { refreshSidebarWorkspaceTree } from "./sidebar.js";
import { DOM, capturePlanTreeBeforeUpdate, markRunRefused, setAgentCardOpen, setConsolePinned, setRunSummary, state } from "./store.js";

export function handleAgentEvent(event) {
  if (!event || !event.type) return;

  // Every inbound event also lands in the dock console, which keeps a plain transcript
  // of the run for the pane an IDE user already watches.
  echoAgentEvent(event);

  switch (event.type) {
    case "workflow_started":
      // A fresh run clears the previous run's pin -- the transcript that was kept up because
      // that run failed has been superseded -- and opens the console for the duration of this
      // run. A run that ends cleanly folds it away again (see workflow_complete).
      setConsolePinned(false);
      showConsole();
      break;

    case "architect_spawn":
      DOM.cardArchitect.style.display = "flex";
      DOM.cardArchitect.style.flex = "1";
      DOM.cardCoder.style.display = "none";
      setAgentCardOpen("architect", true);
      setAgentCardOpen("coder", false);
      DOM.architectStatusBadge.textContent = "Planning";
      DOM.architectStatusBadge.className = "status-badge";
      DOM.btnCloseArchitect.disabled = true;
      setAgentThinking(event.agent || "software-architect", true);
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
      // The architect is handing off rather than thinking: while the coder works, only the coder
      // pulses. The architect's pulse stops here and resumes on coder_summary, when it takes the
      // work back for verification.
      setAgentThinking(event.from_agent || "software-architect", false);
      triggerDelegationAnimation(event.target_agent, event.target_name);
      break;

    case "coder_spawn":
      spawnCoderCard(event.agent, event.name, event.model);
      setAgentThinking(event.agent, true);
      break;

    case "coder_summary":
      renderCardSummary(DOM.coderSummary, event.summary, false);
      DOM.coderStatusBadge.textContent = "Completed";
      DOM.coderStatusBadge.className = "status-badge coder-status completed";
      setAgentThinking(event.agent, false);
      // The coder is done and the architect takes the work back to verify it, so the main agent's
      // pulse resumes for that window (cleared again on architect_summary).
      setAgentThinking("software-architect", true);
      break;

    case "architect_summary":
      renderCardSummary(DOM.architectSummary, event.summary, true);
      // A failed verification is not an approval, so the badge must not claim "Verified".
      if (isFailureSummary(event.summary)) {
        DOM.architectStatusBadge.textContent = "Failed";
        DOM.architectStatusBadge.className = "status-badge failed";
      } else {
        DOM.architectStatusBadge.textContent = "Verified";
        DOM.architectStatusBadge.className = "status-badge completed";
      }
      setAgentThinking(event.agent || "software-architect", false);
      // The terminal workflow_complete event carries no summary, so the result view's
      // renderers read it from here (analyze's findings and file list, recommend's
      // proposals, fix_bug's root cause).
      setRunSummary(event.summary);
      // Two summaries are refusals rather than completions: the fix_bug sufficiency gate (an
      // empty report) and the plan Normalization Gate (a document it could not read). Both end
      // the run a moment later, and the reason lives only in the transcript, so the console is
      // revealed and pinned rather than folding away when the run finishes.
      if (isRefusalSummary(event.summary)) {
        markRunRefused();
        revealConsoleForAttention();
      } else if (isFailureSummary(event.summary)) {
        // A failed verification is not a refusal -- the run finished -- but the reason it failed
        // lives only in the transcript, so it is pinned for the same reason an error is.
        revealConsoleForAttention();
      }
      break;

    case "plan_updated":
      // Guard against stale events: if an active plan is already set and this event
      // carries a different filename, ignore it to prevent reverting a recent switch.
      if (state.activePlan && event.filename && event.filename !== state.activePlan) {
        console.warn(`[plan_updated] Ignoring stale event for "${event.filename}" (active: "${state.activePlan}")`);
        break;
      }
      // The result view's roadmap diff needs the tree as it was *before* this event;
      // applyPlanData() overwrites state.planTree on the next line, so it has to be
      // captured here rather than reconstructed afterwards.
      capturePlanTreeBeforeUpdate();
      applyPlanData(event);
      break;


    case "laya_tagging_started":
      showTaggingPanel();
      break;

    case "laya_tagging_progress":
      updateTaggingProgress(event.done, event.total, event.title);
      break;

    case "laya_tagging_done":
      // The tree re-renders from the separate plan_updated event, so this only reports
      // the outcome of the pass itself.
      hideTaggingPanel();
      // The pass is over, so the trigger is usable again (audit M10).
      if (DOM.btnRetagUi) DOM.btnRetagUi.disabled = false;
      if (event.success) {
        const changed = Array.isArray(event.changed) ? event.changed : [];
        // The word list is the fallback, not the intended engine: re-tagging with it only
        // re-derives the four-keyword guess the tags already had. Name the engine that
        // actually answered, so the result cannot be misread as the checkpoint's.
        const engine = event.engine === "model"
          ? "Laya checkpoint"
          : "the word list (set LAYA_BACKEND=model to use the checkpoint)";
        showToast(`Re-tagged ${changed.length} task${changed.length === 1 ? "" : "s"} via ${engine}`, "success");
      } else {
        showToast(event.error || "Re-tagging failed", "error");
      }
      break;

    case "agent_error":
      renderErrorBadge(event.agent, event.error);
      setAgentThinking(event.agent, false);
      // An error is not the end of the run, but it is the moment the transcript becomes worth
      // reading; pinning keeps it on screen for the completion that follows.
      revealConsoleForAttention();
      break;

    case "workflow_stopped":
      // A halt is a human decision, so the transcript stays up to show how far the run got.
      revealConsoleForAttention();
      clearAllAgentThinking();
      emit("run:finalize", { status: event.status });
      break;

    case "workflow_complete":
      clearAllAgentThinking();
      emit("run:finalize", { status: event.status });
      // The backend reports a user halt as workflow_complete(status="stopped") as well, and
      // that is the same keep-it-up case; a run that was not pinned by an error folds the
      // console back to the idle dock.
      if (event.status === "stopped") revealConsoleForAttention();
      else if (!state.consolePinned) hideConsole();
      // The result view is the point of the run, so what the action produced is mounted
      // here -- except after a halt, where the output is partial and "Halted" is the
      // answer the user asked for, so the transcript stays the surface instead.
      // Only a clean finish mounts the result view. A halt resolved to the transcript, and
      // a crashed run resolved to the error it reported, so neither has a result to show
      // (audit H3).
      if (event.status === "finished" && typeof mountResultView === "function") {
        mountResultView();
      }
      break;

    case "intent_failed":
      // Terminal, and the engine will not retry it: the only thing left is to tell the user and
      // stop accepting new work until they have seen it. The gate itself lives in
      // ui/js/connection.js, which is also what the startup ledger read feeds -- a reload must not
      // forget a failure.
      revealConsoleForAttention();
      clearAllAgentThinking();
      blockOnEngineFailure({
        intent_id: event.intent_id || "",
        action_type: event.action_type || "",
        error: event.error || "the run failed",
      });
      break;

    case "workspace_changed":
      emit("workspace:update", event.workspace_dir);
      // The workspace itself changed, so the file listing behind the tree is stale.
      refreshSidebarWorkspaceTree();
      break;

    case "agents_updated":
      emit("agents:apply", event.agents);
      break;

    // The Artifact Gate's events. They carry no rendering of their own -- the DAG
    // (ui/js/dag.js) is the surface for them -- so they are republished on the internal bus
    // rather than handled here. `artifact_planned` carries the whole artifact, so the diff
    // surface needs no second fetch; `task_state_updated` is what recolours a node, which
    // is why a state change is never assumed from an approval.
    case "artifact_planned":
      emit("dag:artifact-planned", event.artifact);
      break;

    case "artifact_approved":
      emit("dag:artifact-approved", event);
      break;

    case "task_state_updated":
      emit("dag:task-state", event);
      break;
  }
}

// Two architect summaries are refusals, not approvals: the fix_bug sufficiency gate and the
// plan Normalization Gate. They are identified by their status string because that is the only
// structured field distinguishing them -- the event type is the same architect_summary a
// successful pass sends. Kept in one place so the strings appear once. See the emitters in
// orchestration/workflow/actions_impl.py and orchestration/workflow/actions_admin.py.
const REFUSAL_STATUSES = ["Input Required", "Needs Manual Structure"];

function isRefusalSummary(summary) {
  return !!summary && REFUSAL_STATUSES.indexOf(summary.status) !== -1;
}

// A summary whose status says the work was judged and did not pass: ``next_step`` and
// ``fix_bug`` both end with this status when their verification gate rejects the result. It
// is a *finished* run, unlike a refusal, so the badge reports failure rather than approval.
const FAILURE_STATUSES = ["Verification Failed"];

function isFailureSummary(summary) {
  return !!summary && FAILURE_STATUSES.indexOf(summary.status) !== -1;
}

function appendLog(agentId, text, logType = "thinking") {
  const isArchitect = agentId === "software-architect" || agentId.includes("architect");
  const targetContent = isArchitect ? DOM.architectStreamContent : DOM.coderStreamContent;
  const targetContainer = isArchitect ? DOM.architectLogs : DOM.coderLogs;
  const shouldAutoScroll = isArchitect ? state.autoScrollArchitect : state.autoScrollCoder;

  const span = document.createElement("span");
  if (logType === "decision") {
    span.className = "log-line-decision";
  } else if (logType === "reasoning") {
    // The model's own deliberation, not the orchestrator's narration: rendered in its own
    // muted style so it is never mistaken for a workflow step.
    span.className = "log-line-reasoning";
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
  setHtml(toolDiv, `<strong>TOOL:</strong> ${escapeHtml(toolName)}${escapeHtml(argDetail)} — <em>${escapeHtml(description || "Executing")}</em>`);
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

// The sidebar's status dot never had a state set on it, so a working agent and an idle one
// looked identical. The active set lives on `state` rather than only on the DOM because the
// lists are rebuilt wholesale (agents_updated) and a class set directly on a node would be
// dropped mid-run; renderSidebarAgents() re-applies the set on every rebuild.
function setAgentThinking(agentId, thinking) {
  if (!agentId || !state.activeAgentIds) return;
  if (thinking) state.activeAgentIds.add(agentId);
  else state.activeAgentIds.delete(agentId);
  const navItem = document.getElementById(`navAgent_${agentId}`);
  if (navItem) navItem.classList.toggle("thinking", !!thinking);
  // The bento header reports the live system, and the set of working agents is part of it.
  if (typeof updateBentoStats === "function") updateBentoStats();
}

function clearAllAgentThinking() {
  if (!state.activeAgentIds) return;
  state.activeAgentIds.forEach((agentId) => {
    const navItem = document.getElementById(`navAgent_${agentId}`);
    if (navItem) navItem.classList.remove("thinking");
  });
  state.activeAgentIds.clear();
  if (typeof updateBentoStats === "function") updateBentoStats();
}

function spawnCoderCard(coderId, coderName, coderModel) {
  DOM.cardArchitect.style.flex = "1";
  DOM.cardCoder.style.display = "flex";
  DOM.cardCoder.style.flex = "1";
  setAgentCardOpen("coder", true);

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

  setHtml(container, `
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
  `);
  container.style.display = "block";
}

function renderErrorBadge(agentId, errorMsg) {
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

// The re-tagging panel is a passive read-out of the backend's progress stream. Every
// entry point resets it from the same blank state, so a second run never inherits the
// previous run's count, title or bar width.
export function showTaggingPanel() {
  if (!DOM.taggingOverlay) return;
  DOM.taggingTitle.textContent = "Tagging plan tasks";
  DOM.taggingCount.textContent = "0 / 0";
  DOM.taggingCurrent.textContent = "";
  DOM.taggingFill.style.width = "0%";
  DOM.taggingOverlay.style.display = "flex";
}

export function hideTaggingPanel() {
  if (!DOM.taggingOverlay) return;
  DOM.taggingOverlay.style.display = "none";
}

function updateTaggingProgress(done, total, title) {
  if (!DOM.taggingOverlay) return;
  const completed = Number(done) || 0;
  const count = Number(total) || 0;
  DOM.taggingCount.textContent = `${completed} / ${count}`;
  DOM.taggingCurrent.textContent = title || "";
  DOM.taggingFill.style.width = `${count > 0 ? (completed / count) * 100 : 0}%`;
}
