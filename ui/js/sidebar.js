// ui/js/sidebar.js — Collapsible side panels and the workspace file tree.
//
// Both panes collapse to a narrow icon rail rather than disappearing. The dock beside the
// centre stage is a width query container, so it takes a real width change -- not
// display:none -- to let the action toolbar reflow into the space a pane gives back. The
// rail also keeps each pane's own controls reachable while the pane is out of the way.
//
// The tree is derived client-side from the same flat listing the file explorer modal uses.
// The backend already returns every path relative to the workspace root, so grouping them
// into folders needs no second bridge method and no guessing about which names are
// directories -- a name only ever appears as a folder if some path has a segment below it.

import { api } from "./api-client.js";
import { escapeHtml } from "./dom.js";
import { showToast } from "./notify.js";
import { checkPlanStructureGate } from "./plan-modals.js";
import { applyPlanData } from "./plan-tree.js";
import { setHtml } from "./safe-dom.js";
import { DOM, setAvailablePlans, state } from "./store.js";

const SIDEBAR_LEFT_KEY = "aleth.sidebarCollapsed";
const SIDEBAR_RIGHT_KEY = "aleth.planSidebarCollapsed";

// A whole workspace listing is re-rendered on every change. A ceiling keeps a very large
// workspace from turning the left pane into a multi-second render; the note says so.
const TREE_MAX_ROWS = 300;

const TREE_FOLDER_ICON =
  '<svg class="tree-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
  '<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/>' +
  "</svg>";

const TREE_FILE_ICON =
  '<svg class="tree-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" ' +
  'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
  '<path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/>' +
  '<polyline points="14 2 14 8 20 8"/>' +
  "</svg>";

// ---------------------------------------------------------------------------
// Collapse state
// ---------------------------------------------------------------------------

// A webview can refuse the store outright (private mode, a locked profile), so both the
// read and the write are guarded: a lost preference costs one relaunch, never the panel.
function readSidebarFlag(key) {
  try {
    if (typeof localStorage === "undefined") return false;
    return localStorage.getItem(key) === "1";
  } catch (_err) {
    return false;
  }
}

function writeSidebarFlag(key, collapsed) {
  try {
    if (typeof localStorage === "undefined") return;
    localStorage.setItem(key, collapsed ? "1" : "0");
  } catch (_err) {
    /* the preference simply does not survive this session */
  }
}

function applyLeftSidebarState() {
  const collapsed = !!state.sidebarCollapsed;
  if (DOM.leftSidebar) DOM.leftSidebar.classList.toggle("collapsed", collapsed);
  const button = DOM.btnToggleLeftSidebar;
  if (!button) return;
  button.setAttribute("aria-expanded", collapsed ? "false" : "true");
  button.title = collapsed ? "Expand the agents panel" : "Collapse the agents panel";
}

function applyRightSidebarState() {
  const collapsed = !!state.planSidebarCollapsed;
  if (DOM.rightPlanSidebar) DOM.rightPlanSidebar.classList.toggle("collapsed", collapsed);
  const button = DOM.btnToggleRightSidebar;
  if (!button) return;
  button.setAttribute("aria-expanded", collapsed ? "false" : "true");
  button.title = collapsed ? "Expand the plan tracker" : "Collapse the plan tracker";
}

function setLeftSidebarCollapsed(collapsed) {
  state.sidebarCollapsed = !!collapsed;
  writeSidebarFlag(SIDEBAR_LEFT_KEY, state.sidebarCollapsed);
  applyLeftSidebarState();
}

function setRightSidebarCollapsed(collapsed) {
  state.planSidebarCollapsed = !!collapsed;
  writeSidebarFlag(SIDEBAR_RIGHT_KEY, state.planSidebarCollapsed);
  applyRightSidebarState();
}

function toggleLeftSidebar() {
  setLeftSidebarCollapsed(!state.sidebarCollapsed);
}

function toggleRightSidebar() {
  setRightSidebarCollapsed(!state.planSidebarCollapsed);
}

// ---------------------------------------------------------------------------
// Left-pane tabs
// ---------------------------------------------------------------------------

// One tab at a time. The panels keep their ids and their renderers, so this is presentation
// only: switching never re-renders and never loses a panel's scroll position. The block's
// off-state is a class rather than the `hidden` attribute -- an author rule already sets
// `display` on these blocks, and author rules beat the UA's [hidden] rule.
export function setSidebarTab(tab) {
  const names = ["agents", "plans", "files", "env"];
  const active = names.indexOf(tab) >= 0 ? tab : "agents";
  state.sidebarTab = active;

  const blocks = document.querySelectorAll(".agent-section-block[data-tab]");
  for (let i = 0; i < blocks.length; i++) {
    blocks[i].classList.toggle("tab-hidden", blocks[i].getAttribute("data-tab") !== active);
  }

  const buttons = [DOM.tabSidebarAgents, DOM.tabSidebarPlans, DOM.tabSidebarFiles, DOM.tabSidebarEnv];
  names.forEach((name, i) => {
    const button = buttons[i];
    if (!button) return;
    const on = name === active;
    button.classList.toggle("active", on);
    button.setAttribute("aria-selected", String(on));
  });

  // The plan list is a backend fact, so it is refreshed when its tab is revealed rather than
  // trusted across runs.
  if (active === "plans") refreshSidebarPlans();
}

// The rail's ⇆ reveals the Plans tab rather than opening the switcher modal. The modal stays
// reachable from the tab's own expand button.
export function openPlansTab() {
  if (state.sidebarCollapsed) setLeftSidebarCollapsed(false);
  setSidebarTab("plans");
}

// ---------------------------------------------------------------------------
// Plan list (left pane)
// ---------------------------------------------------------------------------

export function renderSidebarPlans() {
  if (!DOM.sidebarPlansList) return;
  const plans = Array.isArray(state.availablePlans) ? state.availablePlans : [];
  if (DOM.countSidebarPlans) DOM.countSidebarPlans.textContent = String(plans.length);

  if (plans.length === 0) {
    setHtml(DOM.sidebarPlansList, '<div class="tree-empty">No .md plans found in the workspace yet.</div>');
    return;
  }

  // The same chip the switcher draws, so the two surfaces cannot drift apart.
  setHtml(DOM.sidebarPlansList, plans.map((name) => {
    const active = name === state.activePlan ? " active" : "";
    return `<button type="button" class="plan-chip${active}" data-plan="${escapeHtml(name)}">${escapeHtml(name)}</button>`;
  }).join(""));
}

// The set of plan files is a backend fact, not something to remember across events: a
// `plan_updated` carries the plan's content but no file list. Ask fresh when the tab opens.
async function refreshSidebarPlans() {
  try {
    const plans = await api.get_plan_files();
    setAvailablePlans(plans);
  } catch (_err) {
    /* offline / no bridge: fall back to the list already in state */
  }
  renderSidebarPlans();
}

// Switching from the tab runs the same path the switcher's chips do, including the advisory
// structural gate -- one behaviour, two entry points.
export async function switchActivePlan(planName) {
  if (!planName) return;
  if (planName === state.activePlan) {
    // A silent no-op reads as a broken click; say the plan is already active (audit L9).
    showToast("Already the active plan.", "info");
    return;
  }
  if (state.isExecuting) {
    showToast("A run is in progress; switch plans after it finishes.", "info");
    return;
  }
  try {
    const res = await api.set_active_plan(planName);
    if (res && res.error) {
      showToast(res.error, "error");
      return;
    }
    applyPlanData(res);
    showToast(`Switched active plan to: ${planName}`, "success");
    checkPlanStructureGate(planName);
  } catch (err) {
    showToast(`Could not switch plan: ${(err && err.message) || err}`, "error");
  }
}

// The top bar's Files button reveals the sidebar's Files tab instead of a centred modal: the
// tree already lives there, and mirroring it in a modal was the redundancy this pass removed.
// The full explorer (search, counts) stays reachable from the tab's own expand button.
export function openFilesTab() {
  if (state.sidebarCollapsed) setLeftSidebarCollapsed(false);
  setSidebarTab("files");
}

// ---------------------------------------------------------------------------
// Workspace tree
// ---------------------------------------------------------------------------

// Turns the flat {name, path, size} listing into a folder hierarchy. Every node keeps the
// order the caller supplies; rendering sorts, so the tree is stable regardless of the
// bridge's ordering.
function buildWorkspaceTree(files) {
  const root = { folders: [], files: [] };
  const seen = new Map();

  (files || []).forEach((entry) => {
    const rel = String((entry && (entry.path || entry.name)) || "").replace(/\\/g, "/");
    const parts = rel.split("/").filter(Boolean);
    if (parts.length === 0) return;

    const fileName = parts.pop();
    let node = root;
    let prefix = "";
    parts.forEach((part) => {
      prefix = prefix ? `${prefix}/${part}` : part;
      let child = seen.get(prefix);
      if (!child) {
        child = { name: part, folders: [], files: [] };
        seen.set(prefix, child);
        node.folders.push(child);
      }
      node = child;
    });

    node.files.push({
      name: fileName,
      path: rel,
      vcs: (entry && entry.vcs) || "",
    });
  });

  return root;
}

// One letter, colour-coded, in the space the file size used to take: M = changed, U = new.
// Nothing renders for an unchanged file, so the pane reads as a change list rather than a
// stat dump. The letters come from the backend (tools/git_status.py), which answers from git
// when the workspace is a work tree and from the task snapshots when it is not -- the
// validation below is defensive, because the badge is the only thing shown here.
function treeVcsBadge(status) {
  const letter = String(status || "").toUpperCase();
  if (letter !== "M" && letter !== "U") return "";
  const label = letter === "M" ? "Modified" : "Untracked";
  return `<span class="tree-vcs vcs-${letter}" title="${label}">${letter}</span>`;
}

// One row budget shared by the whole walk, so the cap counts what is actually rendered
// rather than what each level would render on its own.
function nextTreeRow(budget) {
  if (budget.rows >= TREE_MAX_ROWS) {
    budget.truncated = true;
    return false;
  }
  budget.rows += 1;
  return true;
}

function renderTreeContents(node, budget) {
  let html = "";

  node.folders
    .slice()
    .sort((a, b) => a.name.localeCompare(b.name))
    .forEach((folder) => {
      if (!nextTreeRow(budget)) return;
      html +=
        "<details class=\"tree-folder\" open>" +
        "<summary class=\"tree-folder-row\">" +
        "<span class=\"tree-caret\">\u203a</span>" +
        TREE_FOLDER_ICON +
        `<span class="tree-name">${escapeHtml(folder.name)}</span>` +
        "</summary>" +
        `<div class="tree-children">${renderTreeContents(folder, budget)}</div>` +
        "</details>";
    });

  node.files
    .slice()
    .sort((a, b) => a.name.localeCompare(b.name))
    .forEach((file) => {
      if (!nextTreeRow(budget)) return;
      html +=
        `<div class="tree-file-row" title="${escapeHtml(file.path)}">` +
        TREE_FILE_ICON +
        `<span class="tree-name">${escapeHtml(file.name)}</span>` +
        treeVcsBadge(file.vcs) +
        "</div>";
    });

  return html;
}

export function renderSidebarWorkspaceTree(files) {
  if (!DOM.sidebarTree) return;

  const list = files || [];
  if (DOM.countTreeFiles) DOM.countTreeFiles.textContent = String(list.length);

  if (list.length === 0) {
    setHtml(DOM.sidebarTree,
      '<div class="tree-empty">No files in this workspace yet.</div>');
    return;
  }

  const budget = { rows: 0, truncated: false };
  const html = renderTreeContents(buildWorkspaceTree(list), budget);
  setHtml(DOM.sidebarTree, budget.truncated
    ? html + `<div class="tree-note">Showing the first ${TREE_MAX_ROWS} entries.</div>`
    : html);
}

// Re-reads the listing after the workspace itself changed. bootstrap seeds the tree from
// the workspace info it already fetched; only a later change needs a fresh call.
export async function refreshSidebarWorkspaceTree() {
  if (!DOM.sidebarTree) return;

  try {
    const info = await api.get_workspace_info();
    renderSidebarWorkspaceTree((info && info.files) || []);
  } catch (_err) {
    // The tree keeps its last listing; the workspace card still reports the new path.
  }
}

export function initSidebars() {
  state.sidebarCollapsed = readSidebarFlag(SIDEBAR_LEFT_KEY);
  // The right pane is a fixed rail now: the plan tracker it used to hold is rendered in
  // the centre workbench, so there is nothing to expand into and the stored preference is
  // deliberately ignored.
  state.planSidebarCollapsed = true;
  applyLeftSidebarState();
  applyRightSidebarState();
  // Settle the initial tab. The markup already marks the off tabs, so this only confirms the
  // state; it is here rather than inline so the tab and the flag can never disagree.
  setSidebarTab(state.sidebarTab || "agents");

  if (DOM.btnToggleLeftSidebar) {
    DOM.btnToggleLeftSidebar.addEventListener("click", toggleLeftSidebar);
  }
  if (DOM.btnToggleRightSidebar) {
    DOM.btnToggleRightSidebar.addEventListener("click", toggleRightSidebar);
  }
}
