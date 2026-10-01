// ui/js/bootstrap.js — Gateway handshake, opt-in demo data, agent/workspace hydration.
// ============================================================================
// Startup: the local API gateway
// ============================================================================
import { api, connectEventStream } from "./api-client.js";
import { beginRunUi } from "./actions.js";
import { installBus } from "./bridge-bus.js";
import {
  CONNECTED,
  LOST,
  RECONNECTING,
  blockOnEngineFailure,
  initConnectionIndicator,
  setConnectionState,
} from "./connection.js";
import { clearFatalError, showFatalError } from "./fatal.js";
import { renderSidebarAgents } from "./agents.js";
import { showToast } from "./notify.js";
import { openCreatePlanModal } from "./plan-modals.js";
import { applyPlanData } from "./plan-tree.js";
import { renderSidebarWorkspaceTree } from "./sidebar.js";
import { DOM, state } from "./store.js";
import { refreshWorkbenchChrome } from "./workbench.js";

export async function initFromGateway() {
  try {
    clearFatalError();
    const agentsData = await api.get_agents();
    applyAgentsData(agentsData);

    const wsInfo = await api.get_workspace_info();
    if (wsInfo && wsInfo.workspace_dir) {
      updateWorkspaceUI(wsInfo.workspace_dir);
    }
    // The same payload the file explorer modal reads, grouped into folders here rather
    // than in Python: the listing is already a flat set of workspace-relative paths.
    renderSidebarWorkspaceTree((wsInfo && wsInfo.files) || []);

    // Load active plan with plan.json machine state
    const planData = await api.get_active_plan();
    applyPlanData(planData);
    reportPlanDiagnostics(planData);

    if (!planData.exists && (!planData.plans || planData.plans.length === 0)) {
      openCreatePlanModal();
    }

    // The UI is loaded and its inbound sink is bound, so the backend may start the swarm. The
    // cold-start tick is gated on this: a tick before these listeners exist would broadcast
    // task_state_updated into nothing and the DAG would render stale until the next plan load.
    await api.ui_ready();

    // The durable ledger, not the in-memory queue: a run that failed while this window was closed
    // is still a run that failed, and the user has not been told. Reading it here is what makes
    // the gate survive a reload.
    const intents = await api.get_intent_status();
    if (intents && intents.pending_failure) {
      blockOnEngineFailure(intents.pending_failure);
    }

    // A reload mid-run leaves the backend thread alive while `state.isExecuting` resets, so
    // Stop stays hidden and a second run is launchable. Ask the backend and re-arm the lock
    // (audit H6).
    const runState = await api.get_run_state();
    if (runState && runState.running && typeof beginRunUi === "function") {
      beginRunUi();
    }
  } catch (err) {
    // The client throws on every failure, so this is the one place a failed boot is caught -- and
    // it must not be swallowed: the window would stay fully rendered but empty, which looks
    // identical to a working app with no project. A browser opened with `?demo=1` still gets its
    // layout preview; everywhere else this is the blocking screen, because there is nothing real
    // to show and no way for the user to tell.
    const detail = (err && err.message) || String(err);
    console.error("[Aleth UI] the gateway handshake failed:", detail);
    if (isDemoModeRequested()) {
      renderFallbackDemoData();
      return;
    }
    showFatalError({
      title: "The engine is unreachable",
      detail,
      onRetry: () => initFromGateway(),
    });
  }
}

// Starts the engine event stream, and reflects its health in the UI. The gateway is a plain HTTP
// server, so this works in the desktop window and in a browser alike -- there is no desktop-only
// path left.
//
// The sink is installed first. A frame that arrives before the sink exists is dropped, and the
// sink is the same strict parser the pywebview push used -- so an event from the stream is
// validated exactly as an injected one was, and a malformed payload is rejected either way.
export function startEventStream() {
  installBus();
  initConnectionIndicator();
  return connectEventStream({
    onMessage: (raw) => {
      const sink = window.__deepAgentsBus;
      if (sink && typeof sink.receive === "function") sink.receive(raw);
    },
    onStatus: (status) => {
      if (!status) return;
      if (status.unsupported) {
        // No EventSource at all: the UI has no way to be told anything, so it must not pretend
        // it can act.
        setConnectionState(LOST);
        return;
      }
      setConnectionState(status.connected ? CONNECTED : RECONNECTING);
    },
  });
}

function reportPlanDiagnostics(planData) {
  if (!planData || typeof showToast !== "function") return;

  // A plan.json that could not be read is rebuilt from the markdown when it can be, and
  // degrades to a blank plan when it cannot. Either way, say so: a blank tree with no
  // explanation is the signature of a corrupt file, not an empty roadmap (audit F3).
  if (planData.load_error) {
    showToast(`${planData.load_error}. The plan was rebuilt from ${planData.filename || "the markdown plan"}.`, "error");
  }

  const tasks = planData.plan_json && planData.plan_json.steps
    ? planData.plan_json.steps.length
    : (planData.tree || []).length;
  if (tasks > 0) return;

  const dir = state.workspaceDir || "the workspace";
  const plans = planData.plans || [];
  if (!planData.exists && plans.length === 0) {
    showToast(`No plan file found in "${dir}". Actions stay locked until a plan exists.`, "error");
  } else {
    showToast(`No tasks could be read from "${planData.filename || "the plan"}" in "${dir}".`, "error");
  }
}

// Demo data is opt-in (open this page with ?demo=1). It exists to preview the layout
// in a plain browser with no gateway behind it. In the packaged app a gateway that cannot be
// reached means the backend failed to start, not that the user has no project -- and seeding
// demo data there disguised exactly that failure as a working app showing a project the user
// does not have.
function isDemoModeRequested() {
  try {
    // A proper `demo=1` match, not a substring test: `?x=demo` enabled the preview path,
    // and the flag is a deliberate opt-in (audit M9). A regex rather than URLSearchParams
    // so the check has no host dependency.
    const search = window.location && window.location.search;
    return typeof search === "string" && /(?:^|[?&])demo=1(?:&|$)/.test(search);
  } catch (_err) {
    return false;
  }
}

function renderFallbackDemoData() {
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
            { id: "task-1", title: "Formulate system roadmap and data models", status: "completed", details: ["Lead Architect: Defined requirements", "Dynamic plan: PLAN.md"] },
            { id: "task-2", title: "Scaffold project environment", status: "completed", details: ["Assigned: coder-deep", "Created: main.py"] }
          ]
        },
        {
          id: "sec-2",
          title: "2. Core Implementation",
          tasks: [
            { id: "task-3", title: "Build weather service REST endpoints", status: "pending", details: ["Assigned: coder-deep", "File: weather_api.py"] },
            { id: "task-4", title: "Build weather UI dashboard", status: "pending", tag: "FE", details: ["Frontend visualization component"] },
            { id: "task-5", title: "Implement query caching and validation", status: "pending", details: [] }
          ]
        },
        {
          id: "sec-3",
          title: "3. Testing & Verification",
          tasks: [
            { id: "task-6", title: "Construct automated pytest suite", status: "pending", details: ["Assigned: coder-standard", "File: test_weather_api.py"] },
            { id: "task-7", title: "Audit deliverables and deployment readiness", status: "pending", details: [] }
          ]
        }
      ],
      steps: [
        { id: "task-1", section: "1. Architecture & Setup", title: "Formulate system roadmap and data models", status: "completed", details: [] },
        { id: "task-2", section: "1. Architecture & Setup", title: "Scaffold project environment", status: "completed", details: [] },
        { id: "task-3", section: "2. Core Implementation", title: "Build weather service REST endpoints", status: "pending", details: [] },
        { id: "task-4", section: "2. Core Implementation", title: "Build weather UI dashboard", status: "pending", tag: "FE", details: [] },
        { id: "task-5", section: "2. Core Implementation", title: "Implement query caching and validation", status: "pending", details: [] },
        { id: "task-6", section: "3. Testing & Verification", title: "Construct automated pytest suite", status: "pending", details: [] },
        { id: "task-7", section: "3. Testing & Verification", title: "Audit deliverables and deployment readiness", status: "pending", details: [] }
      ]
    }
  });
}

export function applyAgentsData(data) {
  if (!data) return;
  state.mainAgents = data.main_agents || [];
  state.coderAgents = data.coder_agents || [];
  if (data.workspace_dir) {
    updateWorkspaceUI(data.workspace_dir);
  }
  renderSidebarAgents();
}

export function updateWorkspaceUI(dirPath) {
  state.workspaceDir = dirPath;
  const parts = dirPath.split(/[/\\]/);
  const folderName = parts[parts.length - 1] || dirPath;
  DOM.txtWorkspacePath.textContent = folderName;
  DOM.txtWorkspacePath.title = dirPath;
  // The workbench breadcrumb names the same folder, and a workspace_changed event
  // arrives without a plan reload to refresh it.
  if (typeof refreshWorkbenchChrome === "function") refreshWorkbenchChrome();
}
