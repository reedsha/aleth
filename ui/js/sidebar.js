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

const SIDEBAR_LEFT_KEY = "deepagents.sidebarCollapsed";
const SIDEBAR_RIGHT_KEY = "deepagents.planSidebarCollapsed";

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

    node.files.push({ name: fileName, path: rel, size: (entry && entry.size) || 0 });
  });

  return root;
}

function formatTreeFileSize(bytes) {
  const size = Number(bytes) || 0;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
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
        `<span class="tree-size">${formatTreeFileSize(file.size)}</span>` +
        "</div>";
    });

  return html;
}

function renderSidebarWorkspaceTree(files) {
  if (!DOM.sidebarTree) return;

  const list = files || [];
  if (DOM.countTreeFiles) DOM.countTreeFiles.textContent = String(list.length);

  if (list.length === 0) {
    DOM.sidebarTree.innerHTML =
      '<div class="tree-empty">No files in this workspace yet.</div>';
    return;
  }

  const budget = { rows: 0, truncated: false };
  const html = renderTreeContents(buildWorkspaceTree(list), budget);
  DOM.sidebarTree.innerHTML = budget.truncated
    ? html + `<div class="tree-note">Showing the first ${TREE_MAX_ROWS} entries.</div>`
    : html;
}

// Re-reads the listing after the workspace itself changed. bootstrap seeds the tree from
// the workspace info it already fetched; only a later change needs a fresh call.
async function refreshSidebarWorkspaceTree() {
  if (!DOM.sidebarTree) return;
  if (!(window.pywebview && window.pywebview.api && window.pywebview.api.get_workspace_info)) return;

  try {
    const info = await window.pywebview.api.get_workspace_info();
    renderSidebarWorkspaceTree((info && info.files) || []);
  } catch (_err) {
    // The tree keeps its last listing; the workspace card still reports the new path.
  }
}

function initSidebars() {
  state.sidebarCollapsed = readSidebarFlag(SIDEBAR_LEFT_KEY);
  // The right pane is a fixed rail now: the plan tracker it used to hold is rendered in
  // the centre workbench, so there is nothing to expand into and the stored preference is
  // deliberately ignored.
  state.planSidebarCollapsed = true;
  applyLeftSidebarState();
  applyRightSidebarState();

  if (DOM.btnToggleLeftSidebar) {
    DOM.btnToggleLeftSidebar.addEventListener("click", toggleLeftSidebar);
  }
  if (DOM.btnToggleRightSidebar) {
    DOM.btnToggleRightSidebar.addEventListener("click", toggleRightSidebar);
  }
}
