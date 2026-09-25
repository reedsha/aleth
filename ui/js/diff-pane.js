// ui/js/diff-pane.js — The tracked-edits pane: the file changes a task actually made.
//
// The diffs are not reconstructed here. A task snapshots each deliverable before touching
// it (tools/recovery.py), so the bridge can hand back the real before/after text and a
// unified diff of the two; this module only lays those lines out. What is shown is
// therefore the change on disk, not a guess at it.
//
// The hunk headers carry the numbers, so the gutter is derived from the diff itself: one
// counter per side, advanced only by the lines that side actually saw.

const DIFF_HUNK_RE = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

// Turns unified-diff lines into rows with a display number. A pure function of the diff,
// so the gutter can never drift from the text beside it.
function parseUnifiedDiff(lines) {
  const rows = [];
  let oldNo = 0;
  let newNo = 0;

  (lines || []).forEach((raw) => {
    const text = String(raw);
    if (text.startsWith("--- ") || text.startsWith("+++ ")) {
      rows.push({ kind: "meta", text, number: "" });
      return;
    }

    const hunk = text.match(DIFF_HUNK_RE);
    if (hunk) {
      oldNo = parseInt(hunk[1], 10);
      newNo = parseInt(hunk[2], 10);
      rows.push({ kind: "hunk", text, number: "" });
      return;
    }

    const sign = text.charAt(0);
    const body = text.slice(1);
    if (sign === "+") {
      rows.push({ kind: "add", text: body, number: String(newNo) });
      newNo += 1;
    } else if (sign === "-") {
      rows.push({ kind: "remove", text: body, number: String(oldNo) });
      oldNo += 1;
    } else if (sign === " ") {
      rows.push({ kind: "context", text: body, number: String(newNo) });
      oldNo += 1;
      newNo += 1;
    } else {
      // "\ No newline at end of file" and any other marker line.
      rows.push({ kind: "meta", text, number: "" });
    }
  });

  return rows;
}

const DIFF_SIGN = { add: "+", remove: "-", context: " " };

function diffRowMarkup(row) {
  if (row.kind === "meta" || row.kind === "hunk") {
    return `<div class="diff-line diff-${row.kind}">` +
      `<span class="diff-ln"></span><span class="diff-sign"></span>` +
      `<span class="diff-text">${escapeHtml(row.text)}</span></div>`;
  }
  return `<div class="diff-line diff-${row.kind}">` +
    `<span class="diff-ln">${escapeHtml(row.number)}</span>` +
    `<span class="diff-sign">${DIFF_SIGN[row.kind] || " "}</span>` +
    `<span class="diff-text">${escapeHtml(row.text)}</span></div>`;
}

const DIFF_ACTION_LABEL = { created: "new file", modified: "modified" };

function diffFileMarkup(file) {
  const action = file.action || "modified";
  const label = DIFF_ACTION_LABEL[action] || action;
  const head =
    `<div class="diff-file-head">` +
      `<span class="diff-file-name" title="${escapeHtml(file.filename)}">${escapeHtml(file.filename)}</span>` +
      `<span class="diff-file-action ${escapeHtml(action)}">${escapeHtml(label)}</span>` +
      `<span class="diff-file-stat">` +
        `<span class="diff-stat-added">+${file.added || 0}</span>` +
        `<span class="diff-stat-removed">−${file.removed || 0}</span>` +
      `</span>` +
    `</div>`;

  if (!file.available) {
    return `<div class="diff-file">${head}` +
      `<div class="diff-file-note">Content unavailable (binary, oversized or missing).</div></div>`;
  }

  const rows = parseUnifiedDiff(file.diff);
  if (!rows.length) {
    return `<div class="diff-file">${head}` +
      `<div class="diff-file-note">No textual change recorded.</div></div>`;
  }

  return `<div class="diff-file">${head}` +
    `<div class="diff-lines">${rows.map(diffRowMarkup).join("")}</div></div>`;
}

function renderDiffPane(data, taskId) {
  if (!DOM.diffPaneBody) return;
  const files = (data && data.files) || [];

  if (DOM.diffPaneTitle) {
    DOM.diffPaneTitle.textContent = taskId ? `Edits · ${taskId}` : "File edits";
  }
  if (DOM.diffPaneStat) {
    const totals = (data && data.totals) || { added: 0, removed: 0 };
    DOM.diffPaneStat.textContent = files.length
      ? `${files.length} file${files.length === 1 ? "" : "s"} · +${totals.added} −${totals.removed}`
      : "";
  }

  if (!data || data.success === false) {
    DOM.diffPaneBody.innerHTML =
      `<div class="diff-empty">${escapeHtml((data && data.error) || "Could not read the tracked edits.")}</div>`;
    return;
  }
  if (!files.length) {
    DOM.diffPaneBody.innerHTML = `<div class="diff-empty">` +
      escapeHtml(data.found === false
        ? "This task has recorded no file edits."
        : "No file changes recorded for this task.") + `</div>`;
    return;
  }

  DOM.diffPaneBody.innerHTML = files.map(diffFileMarkup).join("");
  DOM.diffPaneBody.scrollTop = 0;
}

// `pending` shows the pane before the run has written anything, so the stage is already
// split while the agents work; the same call without it reads the snapshots afterwards.
// The bridge is checked first because a pane that silently stays on "Watching" would be
// indistinguishable from a run that simply wrote nothing yet.
async function openDiffPane(taskId, pending) {
  if (!DOM.diffPane || !taskId) return;
  DOM.diffPane.classList.add("active");

  const hasBridge = !!(window.pywebview && window.pywebview.api &&
    typeof window.pywebview.api.get_task_diff === "function");
  if (!hasBridge) {
    renderDiffPane({ success: false, error: "The desktop bridge is unavailable, so tracked edits cannot be read." }, taskId);
    return;
  }

  if (pending) {
    if (DOM.diffPaneTitle) DOM.diffPaneTitle.textContent = `Edits · ${taskId}`;
    if (DOM.diffPaneStat) DOM.diffPaneStat.textContent = "";
    if (DOM.diffPaneBody) {
      DOM.diffPaneBody.innerHTML = '<div class="diff-empty">Watching for file edits…</div>';
    }
    return;
  }

  if (DOM.diffPaneBody) {
    DOM.diffPaneBody.innerHTML = '<div class="diff-empty">Loading tracked edits…</div>';
  }
  try {
    renderDiffPane(await window.pywebview.api.get_task_diff(taskId), taskId);
  } catch (err) {
    renderDiffPane({ success: false, error: `Could not read tracked edits: ${(err && err.message) || err}` }, taskId);
  }
}

function closeDiffPane() {
  if (DOM.diffPane) DOM.diffPane.classList.remove("active");
}

function initDiffPane() {
  // The pane starts closed; it is opened by a run or by a task's own Diff button.
  closeDiffPane();
}
