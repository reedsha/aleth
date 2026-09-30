// ui/js/agents.js — Sidebar agent navigation and the system prompt editor.
// ============================================================================
// Left Sidebar Dynamic Agent Navigation
// ============================================================================
import { escapeHtml } from "./dom.js";
import { showToast } from "./notify.js";
import { setHtml, setText } from "./safe-dom.js";
import { DOM, state } from "./store.js";

export function renderSidebarAgents() {
  setText(DOM.sidebarMainAgentsList, "");
  setText(DOM.sidebarCoderAgentsList, "");

  if (DOM.countMainAgents) DOM.countMainAgents.textContent = state.mainAgents.length;
  if (DOM.countCoderAgents) DOM.countCoderAgents.textContent = state.coderAgents.length;

  state.mainAgents.forEach((agent) => {
    const item = document.createElement("div");
    item.className = "agent-list-item";
    item.id = `navAgent_${agent.id}`;
    item.title = `${agent.display_name} - Click to view/edit system prompt`;
    setHtml(item, `
      <div class="agent-item-icon main">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polygon points="12 2 2 7 12 12 22 7 12 2"/>
          <polyline points="2 17 12 22 22 17"/>
          <polyline points="2 12 12 17 22 12"/>
        </svg>
      </div>
      <div class="agent-item-info">
        <span class="agent-item-name">${escapeHtml(agent.display_name)}</span>
        <span class="agent-item-sub">Coordinator</span>
      </div>
      <span class="agent-status-indicator"></span>
    `);
    if (state.activeAgentIds && state.activeAgentIds.has(agent.id)) item.classList.add("thinking");
    item.addEventListener("click", () => openPromptEditor(agent.id));
    DOM.sidebarMainAgentsList.appendChild(item);
  });

  state.coderAgents.forEach((agent) => {
    const item = document.createElement("div");
    item.className = "agent-list-item";
    item.id = `navAgent_${agent.id}`;
    item.title = `${agent.display_name} - Click to view/edit system prompt`;
    setHtml(item, `
      <div class="agent-item-icon coder">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="16 18 22 12 16 6"/>
          <polyline points="8 6 2 12 8 18"/>
        </svg>
      </div>
      <div class="agent-item-info">
        <span class="agent-item-name">${escapeHtml(agent.display_name)}</span>
        <span class="agent-item-sub">Sub-Agent</span>
      </div>
      <span class="agent-status-indicator"></span>
    `);
    if (state.activeAgentIds && state.activeAgentIds.has(agent.id)) item.classList.add("thinking");
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

  setText(DOM.editorToolsList, "");
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

export function closePromptEditor() {
  DOM.promptEditorView.style.display = "none";
  DOM.chatWorkspaceView.style.display = "flex";
  DOM.chatWorkspaceView.classList.add("active");
  document.querySelectorAll(".agent-list-item").forEach((el) => el.classList.remove("active-agent"));
}

// The gutter is rebuilt only when the line count changes: identical counts produce identical
// numbers, so re-rendering it on every keystroke was wasted work (audit M11).
let lastEditorLineCount = -1;

export function updateEditorMetrics() {
  const text = DOM.txtSystemPrompt.value;
  const lines = text.split("\n");
  DOM.promptCharCount.textContent = `${text.length} chars • ${lines.length} lines`;
  if (lines.length !== lastEditorLineCount) {
    lastEditorLineCount = lines.length;
    setHtml(DOM.editorLineNumbers, lines.map((_, i) => i + 1).join("<br>"));
  }
}

export async function saveCurrentSystemPrompt() {
  if (!state.activeAgentForEditor) return;
  const agentId = state.activeAgentForEditor.id;
  const newPrompt = DOM.txtSystemPrompt.value;

  // Disable while in flight so a double-click cannot issue two writes (audit M10).
  const btn = DOM.btnSavePrompt;
  if (btn) btn.disabled = true;
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
  } finally {
    if (btn) btn.disabled = false;
  }
}
