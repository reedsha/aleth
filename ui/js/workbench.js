// ui/js/workbench.js — The Plan Workbench: the active plan rendered in the centre stage.
//
// The plan file is not a description of the work, it *is* the work: it is what the
// agents read and rewrite, and what plan.json is compiled from. So the centre stage
// shows the plan's own markdown rather than a rendered summary, with the syntax marks
// dimmed instead of hidden -- the document stays readable while remaining
// character-for-character what a save would write.
//
// Read mode renders one row per source line, so the line numbers are produced by the
// same loop that produces the text and cannot drift apart from it. Edit mode swaps in a
// textarea with a virtual gutter (one number per newline) synced to its scroll offset.
//
// Wiring deliberately lives in initEventListeners (ui/js/wire.js) with every other
// control, so a throw in this module cannot cost any interactivity.

let workbenchRendered = null; // the content currently on screen; null before the first load
let workbenchSaved = "";      // the content the editor was opened with, i.e. last known disk state
let workbenchBusy = false;    // re-entrancy guard around the async save
let workbenchGutterLines = 0; // line count currently painted in the gutter

// --- Markdown source rendering ---------------------------------------------
// Escaping happens first and every pattern is single-line, so no markdown run can span
// a newline. That keeps the whole document escaped by construction: the only angle
// brackets in the output are the spans built below.
const WORKBENCH_INLINE_RE = /(`[^`\n]*`)|(\*\*[^*\n]*\*\*)|(__[^_\n]*__)/g;
const WORKBENCH_FENCE_RE = /^\s*(```|~~~)/;
const WORKBENCH_HEADING_RE = /^(\s*)(#{1,6})(\s+)(.*)$/;
const WORKBENCH_CHECK_RE = /^(\s*)([-*+])(\s+)\[([ xX])\]\s*(.*)$/;
const WORKBENCH_BULLET_RE = /^(\s*)([-*+])(\s+)(.*)$/;
const WORKBENCH_NUMBER_RE = /^(\s*)(\d+[.)])(\s+)(.*)$/;

// Dims the delimiters of inline code, bold and underline runs. Done in one pass so an
// inserted span can never be rescanned and re-wrapped by the next pattern.
function workbenchInline(text) {
  return text.replace(WORKBENCH_INLINE_RE, (match, code, strong) => {
    const marker = code ? "`" : (strong ? "**" : "__");
    const width = marker.length;
    const inner = match.slice(width, -width);
    return `<span class="wb-md">${marker}</span>${inner}<span class="wb-md">${marker}</span>`;
  });
}

function workbenchRenderLine(line) {
  const escaped = escapeHtml(line);

  const check = escaped.match(WORKBENCH_CHECK_RE);
  if (check) {
    const done = check[4].toLowerCase() === "x";
    return check[1] +
      `<span class="wb-md">${check[2]}</span>${check[3]}` +
      `<span class="wb-md-check task${done ? " task-done" : ""}">${done ? "✓" : "○"}</span> ` +
      `<span class="wb-src-text${done ? " done" : ""}">${workbenchInline(check[5])}</span>`;
  }

  const heading = escaped.match(WORKBENCH_HEADING_RE);
  if (heading) {
    return heading[1] +
      `<span class="wb-md wb-md-heading">${heading[2]}</span>${heading[3]}` +
      workbenchInline(heading[4]);
  }

  const bullet = escaped.match(WORKBENCH_BULLET_RE);
  if (bullet) {
    return bullet[1] +
      `<span class="wb-md">${bullet[2]}</span>${bullet[3]}` +
      workbenchInline(bullet[4]);
  }

  const numbered = escaped.match(WORKBENCH_NUMBER_RE);
  if (numbered) {
    return numbered[1] +
      `<span class="wb-md">${numbered[2]}</span>${numbered[3]}` +
      workbenchInline(numbered[4]);
  }

  return workbenchInline(escaped);
}

function workbenchEmptyMessage() {
  if (!state.activePlan) {
    return "No active plan selected. Create or select a plan to render it here.";
  }
  return `${state.activePlan} has no content yet.`;
}

// The single entry point for plan text. Fed from applyPlanData, the funnel every plan
// load, switch, save and sync already flows through.
function renderPlanDocument(content) {
  if (!DOM.planDoc) return;
  const text = typeof content === "string" ? content : "";
  if (text === workbenchRendered) return; // identical content: no DOM churn, keeps the scroll position
  workbenchRendered = text;

  if (!text.trim()) {
    DOM.planDoc.innerHTML = `<div class="workbench-empty">${escapeHtml(workbenchEmptyMessage())}</div>`;
    updateWorkbenchMeta();
    return;
  }

  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const rows = [];
  let inFence = false;

  lines.forEach((line, index) => {
    const isFenceDelimiter = WORKBENCH_FENCE_RE.test(line);
    let lineClass = "";
    let source;

    if (isFenceDelimiter || inFence) {
      // Inside a fence the text is code, so markdown marks in it stay literal.
      source = escapeHtml(line);
      lineClass = isFenceDelimiter ? "wb-fence" : "";
      if (isFenceDelimiter) inFence = !inFence;
    } else if (!line.trim()) {
      lineClass = "wb-blank";
      source = "";
    } else {
      if (WORKBENCH_HEADING_RE.test(line)) lineClass = "wb-heading";
      source = workbenchRenderLine(line);
    }

    rows.push(
      `<div class="wb-line${lineClass ? " " + lineClass : ""}">` +
        `<span class="wb-ln">${index + 1}</span>` +
        `<span class="wb-src">${source}</span>` +
      `</div>`
    );
  });

  DOM.planDoc.innerHTML = rows.join("");
  DOM.planDoc.scrollTop = 0;
  updateWorkbenchMeta();
}

// --- Chrome: breadcrumb, task counter, status bar --------------------------
function refreshWorkbenchChrome() {
  const dir = state.workspaceDir || "";
  const segments = dir.split(/[/\\]/).filter(Boolean);
  if (DOM.crumbWorkspace) {
    DOM.crumbWorkspace.textContent = segments.length ? segments[segments.length - 1] : "workspace";
    DOM.crumbWorkspace.title = dir;
  }

  const planFile = state.activePlan || "PLAN.md";
  if (DOM.crumbPlanFile) DOM.crumbPlanFile.textContent = planFile;
  if (DOM.txtWorkbenchPath) DOM.txtWorkbenchPath.textContent = planFile;

  const tasks = state.planTree || [];
  const completed = tasks.filter((task) => task.status === "completed").length;
  if (DOM.txtWorkbenchStat) DOM.txtWorkbenchStat.textContent = `${completed}/${tasks.length} tasks`;

  // Editing needs the bridge: without one a save has nowhere to go, and pretending
  // otherwise would be the same disguise initFallbackMode refuses to make. The bridge
  // is injected after these modules run, so the flag is read here rather than at init.
  const hasBridge = !!(window.pywebview && window.pywebview.api);
  if (DOM.btnWorkbenchEdit) {
    DOM.btnWorkbenchEdit.disabled = !hasBridge;
    DOM.btnWorkbenchEdit.title = hasBridge
      ? "Edit the plan source"
      : "Read-only: the desktop bridge is unavailable";
  }
  updateWorkbenchMeta();
}

function workbenchIsEditing() {
  return !!(DOM.emptyStateContainer && DOM.emptyStateContainer.classList.contains("editing"));
}

// --- View mode: the roadmap as a tree, or the raw markdown ----------------
// The tree is the primary view. Editing always means the source, so entering edit mode
// switches to raw rather than leaving a textarea hidden behind the cards -- and the
// toggle is disabled while editing, so the two states can never fight.
function workbenchView() {
  if (!DOM.emptyStateContainer) return "tree";
  return DOM.emptyStateContainer.classList.contains("raw-view") ? "raw" : "tree";
}

function setWorkbenchView(view) {
  if (!DOM.emptyStateContainer) return;
  const raw = view === "raw";
  DOM.emptyStateContainer.classList.toggle("raw-view", raw);
  if (DOM.btnWorkbenchTreeView) {
    DOM.btnWorkbenchTreeView.classList.toggle("active", !raw);
    DOM.btnWorkbenchTreeView.setAttribute("aria-pressed", String(!raw));
  }
  if (DOM.btnWorkbenchRawMd) {
    DOM.btnWorkbenchRawMd.classList.toggle("active", raw);
    DOM.btnWorkbenchRawMd.setAttribute("aria-pressed", String(raw));
  }
  refreshWorkbenchChrome();
  updateWorkbenchMeta();
}

function handleWorkbenchShowTree() {
  setWorkbenchView("tree");
}

function handleWorkbenchShowRaw() {
  setWorkbenchView("raw");
}

function updateWorkbenchMeta() {
  if (!DOM.txtWorkbenchMeta) return;
  const source = workbenchIsEditing() && DOM.planEditorInput
    ? DOM.planEditorInput.value
    : (workbenchRendered || "");
  const lines = source ? source.split("\n").length : 0;
  const mode = (window.pywebview && window.pywebview.api) ? "editable" : "read-only preview";
  DOM.txtWorkbenchMeta.textContent = `${lines} line${lines === 1 ? "" : "s"} · ${mode}`;
}

function updateWorkbenchDirty() {
  const dirty = !!DOM.planEditorInput && DOM.planEditorInput.value !== workbenchSaved;
  if (DOM.crumbDirty) DOM.crumbDirty.hidden = !dirty;
}

// --- Read/write mode ------------------------------------------------------
function setWorkbenchEditing(editing) {
  if (!DOM.emptyStateContainer || !DOM.planEditorInput) return;
  DOM.emptyStateContainer.classList.toggle("editing", editing);
  if (DOM.btnWorkbenchEdit) DOM.btnWorkbenchEdit.hidden = editing;
  if (DOM.btnWorkbenchSave) DOM.btnWorkbenchSave.hidden = !editing;
  if (DOM.btnWorkbenchDiscard) DOM.btnWorkbenchDiscard.hidden = !editing;
  // The view toggle is meaningless mid-edit: editing always shows the source.
  [DOM.btnWorkbenchTreeView, DOM.btnWorkbenchRawMd].forEach((btn) => {
    if (btn) btn.disabled = !!editing;
  });

  if (editing) {
    setWorkbenchView("raw");
    DOM.planEditorInput.value = workbenchRendered || "";
    workbenchSaved = DOM.planEditorInput.value;
    renderWorkbenchGutter();
    if (DOM.planEditorInput.focus) DOM.planEditorInput.focus();
  }

  updateWorkbenchDirty();
  updateWorkbenchMeta();
}

// The gutter is virtual: it numbers the draft's lines, which is all a textarea can be
// counted on for. It is scrolled in lockstep with the textarea by syncWorkbenchGutterScroll.
function renderWorkbenchGutter() {
  if (!DOM.planEditorGutter || !DOM.planEditorInput) return;
  const count = DOM.planEditorInput.value.split("\n").length;
  if (count === workbenchGutterLines) return; // typing within a line leaves the numbers alone
  workbenchGutterLines = count;
  const numbers = [];
  for (let line = 1; line <= count; line++) numbers.push(String(line));
  DOM.planEditorGutter.textContent = numbers.join("\n");
}

function syncWorkbenchGutterScroll() {
  if (DOM.planEditorGutter && DOM.planEditorInput) {
    DOM.planEditorGutter.scrollTop = DOM.planEditorInput.scrollTop;
  }
}

function handleWorkbenchInput() {
  renderWorkbenchGutter();
  updateWorkbenchDirty();
  updateWorkbenchMeta();
}

function handleWorkbenchEdit() {
  if (workbenchBusy) return;
  if (!window.pywebview || !window.pywebview.api) {
    showToast("Editing the plan needs the desktop app; this view is read-only.", "info");
    return;
  }
  setWorkbenchEditing(true);
}

function handleWorkbenchDiscard() {
  if (workbenchBusy || !DOM.planEditorInput) return;
  DOM.planEditorInput.value = workbenchSaved;
  setWorkbenchEditing(false);
  showToast("Discarded unsaved plan edits", "info");
}

async function handleWorkbenchSave() {
  if (workbenchBusy || !DOM.planEditorInput) return;
  if (!window.pywebview || !window.pywebview.api) {
    showToast("Saving the plan needs the desktop app; this view is read-only.", "info");
    return;
  }

  const filename = state.activePlan || "PLAN.md";
  const content = DOM.planEditorInput.value;
  workbenchBusy = true;
  if (DOM.btnWorkbenchSave) DOM.btnWorkbenchSave.disabled = true;

  try {
    const res = await window.pywebview.api.save_plan_content(filename, content);
    if (res && res.success === false) {
      // Stay in edit mode: the draft is the only copy of the user's work.
      showToast(`Save failed: ${res.error || "unknown error"}`, "error");
      return;
    }
    workbenchSaved = content;
    setWorkbenchEditing(false);
    // The backend re-parses the markdown and re-compiles plan.json, so its return value
    // -- not the editor's text -- is the new truth for both the tree and this document.
    applyPlanData(res);
    showToast(`Saved ${filename} and re-synced plan.json`, "success");
  } catch (err) {
    showToast(`Save error: ${(err && err.message) || err}`, "error");
  } finally {
    workbenchBusy = false;
    if (DOM.btnWorkbenchSave) DOM.btnWorkbenchSave.disabled = false;
  }
}

function initWorkbench() {
  // The first plan load renders the document itself; this settles only the chrome that
  // does not depend on plan content.
  setWorkbenchView("tree");
  refreshWorkbenchChrome();
  updateWorkbenchDirty();
}
