// ui/js/workbench.js — The Plan Workbench: the active plan rendered in the centre stage.
//
// The document shown is a **read-only projection** rendered from the SQLite plan state;
// it is never parsed back to determine state. The centre stage renders the plan's own
// markdown rather than a rendered summary, with the syntax marks dimmed instead of
// hidden, so the roadmap stays readable.
//
// Read mode renders one row per source line, so the line numbers are produced by the
// same loop that produces the text and cannot drift apart from it.
//
// Wiring deliberately lives in initEventListeners (ui/js/wire.js) with every other
// control, so a throw in this module cannot cost any interactivity.

import { emit } from "./bus.js";
import { escapeHtml } from "./dom.js";
import { showToast } from "./notify.js";
import { setHtml } from "./safe-dom.js";
import { DOM, state } from "./store.js";

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
export function renderPlanDocument(content) {
  if (!DOM.planDoc) return;
  const text = typeof content === "string" ? content : "";
  if (text === workbenchRendered) return; // identical content: no DOM churn, keeps the scroll position
  workbenchRendered = text;

  if (!text.trim()) {
    setHtml(DOM.planDoc, `<div class="workbench-empty">${escapeHtml(workbenchEmptyMessage())}</div>`);
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

  setHtml(DOM.planDoc, rows.join(""));
  DOM.planDoc.scrollTop = 0;
  updateWorkbenchMeta();
}

// --- Chrome: breadcrumb, task counter, status bar --------------------------
export function refreshWorkbenchChrome() {
  const dir = state.workspaceDir || "";
  const segments = dir.split(/[/\\]/).filter(Boolean);
  if (DOM.crumbWorkspace) {
    DOM.crumbWorkspace.textContent = segments.length ? segments[segments.length - 1] : "workspace";
    DOM.crumbWorkspace.title = dir;
  }

  const planFile = state.activePlan || "PLAN.md";
  if (DOM.crumbPlanFile) DOM.crumbPlanFile.textContent = planFile;

  // The plan is a read-only projection of the SQLite state, so the raw editor is disabled
  // outright -- there is no longer any path that parses edited markdown back into state.
  if (DOM.btnWorkbenchEdit) {
    DOM.btnWorkbenchEdit.disabled = true;
    DOM.btnWorkbenchEdit.title =
      "Read-only: change the plan with Execute Next Step, Update Plan or Add to Plan";
  }
  updateWorkbenchMeta();
}

function workbenchIsEditing() {
  return !!(DOM.emptyStateContainer && DOM.emptyStateContainer.classList.contains("editing"));
}

// --- View mode: the document tree, the DAG canvas, or the raw markdown ----
// The document tree is the primary view. Editing always means the source, so entering edit
// mode switches to raw rather than leaving a textarea hidden behind the cards -- and the
// toggle is disabled while editing, so the two states can never fight.
//
// The DAG canvas is a peer of the document, not a drawer: it is the same roadmap laid out as
// a dependency graph and it gets the whole stage, which is why the squeezed right-rail copy
// no longer exists. Which of the three is on screen is one class on the workbench; the CSS
// does the showing and hiding so a view cannot be half-applied by a missed JS branch.
export function setWorkbenchView(view) {
  if (!DOM.emptyStateContainer) return;
  const raw = view === "raw";
  const dag = view === "dag";
  DOM.emptyStateContainer.classList.toggle("raw-view", raw);
  DOM.emptyStateContainer.classList.toggle("dag-view", dag);

  const setToggle = (button, active) => {
    if (!button) return;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  };
  setToggle(DOM.btnWorkbenchTreeView, !raw && !dag);
  setToggle(DOM.btnWorkbenchDagView, dag);
  setToggle(DOM.btnWorkbenchRawMd, raw);

  // `hidden` as well as the class: the canvas must be measurable the moment it is shown, and
  // a display rule is not something the DAG renderer can wait on.
  if (DOM.workbenchDagView) DOM.workbenchDagView.hidden = !dag;

  refreshWorkbenchChrome();
  updateWorkbenchMeta();
  if (dag) emit("dag:view-shown");
}

export function handleWorkbenchShowTree() {
  setWorkbenchView("tree");
}

export function handleWorkbenchShowDag() {
  setWorkbenchView("dag");
}

export function handleWorkbenchShowRaw() {
  setWorkbenchView("raw");
}

function updateWorkbenchMeta() {
  if (!DOM.txtWorkbenchMeta) return;
  const source = workbenchIsEditing() && DOM.planEditorInput
    ? DOM.planEditorInput.value
    : (workbenchRendered || "");
  const lines = source ? source.split("\n").length : 0;
  const mode = "editable";
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

export function syncWorkbenchGutterScroll() {
  if (DOM.planEditorGutter && DOM.planEditorInput) {
    DOM.planEditorGutter.scrollTop = DOM.planEditorInput.scrollTop;
  }
}

export function handleWorkbenchInput() {
  renderWorkbenchGutter();
  updateWorkbenchDirty();
  updateWorkbenchMeta();
}

// The raw-markdown editor is gone. The plan is a read-only projection of the SQLite
// state, so a free-text edit cannot be parsed back into it -- a malformed document would
// corrupt the DAG or silently drop dependency edges. The plan is changed through the
// structured actions that mutate the store directly (Execute Next Step, Update Plan,
// Add to Plan), never by hand-writing the document.
export function handleWorkbenchEdit() {
  showToast(
    "The plan is read-only here. Change it with Execute Next Step, Update Plan or Add to Plan.",
    "info"
  );
}

// "Edit this task" is a jump into the source, not a second editor: the plan markdown is the
// only copy the agents read, so editing means the raw view. The title is the anchor because
// that is what the markdown carries; a miss simply opens the top of the file, which is still
// an edit session rather than a dead click.
export function editTaskInWorkbench(step) {
  if (!step || !DOM.planEditorInput) return;
  // Re-anchoring while already editing must not discard the draft: setWorkbenchEditing(true)
  // rewrites the textarea from the last rendered content, so only enter edit mode if we are
  // not in it. Then a second "Edit" just moves the caret.
  if (!workbenchIsEditing()) {
    handleWorkbenchEdit();
    if (!workbenchIsEditing()) return;   // the bridge refused; stay where we were
  }
  const needle = String(step.title || "").trim();
  if (!needle) return;
  const at = DOM.planEditorInput.value.indexOf(needle);
  if (at < 0) return;
  const line = DOM.planEditorInput.value.slice(0, at).split("\n").length;
  DOM.planEditorInput.scrollTop = Math.max(0, (line - 4) * workbenchLineHeight());
  DOM.planEditorInput.setSelectionRange(at, at + needle.length);
}

// The line stride is measured, never assumed: the reading surface's size is a theme token, so
// a hardcoded number would drift the moment the theme changes.
function workbenchLineHeight() {
  if (DOM.planEditorInput && typeof getComputedStyle === "function") {
    const cs = getComputedStyle(DOM.planEditorInput);
    const px = parseFloat(cs && cs.lineHeight);
    if (px > 0) return px;
    const size = parseFloat(cs && cs.fontSize);
    if (size > 0) return size * 1.65;
  }
  return 23;   // 14px x 1.65: the metrics declared in ui/css/workbench.css
}

export function handleWorkbenchDiscard() {
  if (workbenchBusy || !DOM.planEditorInput) return;
  DOM.planEditorInput.value = workbenchSaved;
  setWorkbenchEditing(false);
  showToast("Discarded unsaved plan edits", "info");
}

export function initWorkbench() {
  // The first plan load renders the document itself; this settles only the chrome that
  // does not depend on plan content.
  setWorkbenchView("tree");
  refreshWorkbenchChrome();
  updateWorkbenchDirty();
}
