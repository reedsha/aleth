// ui/js/agent-events.js — Inbound agent event stream renderer (assigned to window.onAgentEvent).
// ============================================================================
// Two-Way Event Handler (window.onAgentEvent)
// ============================================================================
function handleAgentEvent(event) {
  if (!event || !event.type) return;

  // Every inbound event also lands in the dock console, which keeps a plain transcript
  // of the run for the pane an IDE user already watches.
  echoAgentEvent(event);

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
