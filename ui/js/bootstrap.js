// ui/js/bootstrap.js — PyWebView handshake, opt-in demo data, agent/workspace hydration.
// ============================================================================
// PyWebView Integration & Plan Initialization
// ============================================================================
import { beginRunUi } from "./actions.js";
import { installBus } from "./bridge-bus.js";
import { renderSidebarAgents } from "./agents.js";
import { reportStartupFailure, showToast } from "./notify.js";
import { openCreatePlanModal } from "./plan-modals.js";
import { applyPlanData } from "./plan-tree.js";
import { renderSidebarWorkspaceTree } from "./sidebar.js";
import { DOM, state } from "./store.js";
import { refreshWorkbenchChrome } from "./workbench.js";

export async function onPyWebViewReady() {
  console.log("[PyWebView] API connected");
  // The single inbound sink: the backend delivers every event to this one function.
  installBus();

  try {
    const agentsData = await window.pywebview.api.get_agents();
    applyAgentsData(agentsData);

    const wsInfo = await window.pywebview.api.get_workspace_info();
    if (wsInfo && wsInfo.workspace_dir) {
      updateWorkspaceUI(wsInfo.workspace_dir);
    }
    // The same payload the file explorer modal reads, grouped into folders here rather
    // than in Python: the listing is already a flat set of workspace-relative paths.
    renderSidebarWorkspaceTree((wsInfo && wsInfo.files) || []);

    // Load active plan with plan.json machine state
    const planData = await window.pywebview.api.get_active_plan();
    applyPlanData(planData);
    reportPlanDiagnostics(planData);

    if (!planData.exists && (!planData.plans || planData.plans.length === 0)) {
      openCreatePlanModal();
    }

    // A reload mid-run leaves the backend thread alive while `state.isExecuting` resets, so
    // The UI is loaded and its inbound bus is bound, so the backend may start the swarm. The
    // cold-start tick is gated on this: a tick before these listeners exist would broadcast
    // task_state_updated into nothing and the DAG would render stale until the next plan load.
    if (typeof window.pywebview.api.ui_ready === "function") {
      await window.pywebview.api.ui_ready();
    }

    // Stop stays hidden and a second run is launchable. Ask the backend and re-arm the lock
    // (audit H6). Guarded: an older bridge without the method keeps the old behaviour.
    if (typeof window.pywebview.api.get_run_state === "function") {
      const runState = await window.pywebview.api.get_run_state();
      if (runState && runState.running && typeof beginRunUi === "function") {
        beginRunUi();
      }
    }
  } catch (err) {
    console.warn("[PyWebView] Init notice:", err);
    // A failure in the handshake above used to be silent: the window stayed fully
    // rendered but with no agents and no plan, which looks identical to a working
    // app. Surface it, because the packaged app runs with debug=False where console
    // output is invisible.
    notifyInitFailure(err);
  }
}

function notifyInitFailure(err) {
  if (typeof showToast !== "function") return;
  const detail = (err && err.message) ? err.message : String(err);
  showToast(`Could not load workspace data: ${detail}`, "error");
}

// Explains an empty plan tree instead of leaving the action panel locked with no
// visible reason. A locked panel plus a blank tree is the exact signature of the
// app resolving to the wrong (empty) workspace directory.
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
// in a plain browser, where there is no pywebview bridge. In the packaged app a missing
// bridge means the desktop API failed to load, not that the user has no project -- and
// seeding demo data there disguised exactly that failure as a working app showing a
// project the user does not have.
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

export function initFallbackMode() {
  if (!isDemoModeRequested()) {
    const message = "Desktop bridge unavailable: the workspace could not be loaded. " +
      "Restart the app. (For a layout-only preview in a browser, open this page with ?demo=1.)";
    if (typeof reportStartupFailure === "function") reportStartupFailure(message);
    else console.error("[Aleth UI]", message);
    return;
  }
  renderFallbackDemoData();
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
