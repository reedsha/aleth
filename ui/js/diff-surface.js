// ui/js/diff-surface.js — The artifact diff surface (Phase 8).
//
// An approved plan is applied by splicing one AST node at a time, so the thing a human
// reviews is not a file diff: it is one :class:`ASTTarget` per node, each carrying the
// exact replacement `content` and the `byte_range` it would replace. This module renders
// exactly that -- the proposed content against the current bytes at that span, with the
// operation (insert / replace / delete) stated -- and nothing else.
//
// There is deliberately no whole-file view. Reading the whole file would make a whole-file
// diff possible, which is the thing the artifact surface exists to prevent: a human
// approving a plan is approving the listed splices, not a rewritten document.
//
// The span is fetched through the bridge (`get_source_span`), which returns only the
// recorded bytes, so the "no whole file" rule holds at the IPC boundary too.
//
// The proposed content is editable: a plan is a proposal, so a hallucinated character should
// cost one edit rather than a rejected run and a re-plan. An edit is written back with
// `update_artifact_target`, which re-validates and re-stores the artifact and republishes it.
import { api } from "./api-client.js";
import { showToast } from "./notify.js";

// A diff larger than this is shown as a plain before/after rather than aligned: the LCS
// table is O(n*m) and a pathological pair of spans must not freeze the webview.
const MAX_DP_CELLS = 40000;
const MAX_DIFF_LINES = 400;

function splitLines(text) {
  const normalised = String(text || "").replace(/\r\n/g, "\n");
  return normalised.length ? normalised.split("\n") : [];
}

/** A line diff of two spans, as `{type: "same" | "add" | "del", text}` entries.
 *
 * The spans are small by construction -- one AST node each -- so an exact LCS is cheap and
 * gives the honest alignment a reviewer needs. A pair too large for the table degrades to
 * "all removed, then all added", which is still correct, only unaligned.
 */
export function lineDiff(oldText, newText) {
  const a = splitLines(oldText);
  const b = splitLines(newText);
  if (
    a.length > MAX_DIFF_LINES ||
    b.length > MAX_DIFF_LINES ||
    a.length * b.length > MAX_DP_CELLS
  ) {
    return [
      ...a.map((text) => ({ type: "del", text })),
      ...b.map((text) => ({ type: "add", text })),
    ];
  }

  const n = a.length;
  const m = b.length;
  const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }

  const out = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ type: "same", text: a[i] });
      i += 1;
      j += 1;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ type: "del", text: a[i] });
      i += 1;
    } else {
      out.push({ type: "add", text: b[j] });
      j += 1;
    }
  }
  while (i < n) out.push({ type: "del", text: a[i++] });
  while (j < m) out.push({ type: "add", text: b[j++] });
  return out;
}

function operationOf(target) {
  const op = String(target.operation || "replace");
  return op === "insert" || op === "delete" ? op : "replace";
}

function spanOf(target) {
  const range = Array.isArray(target.byte_range) ? target.byte_range : [];
  const start = Number.isFinite(range[0]) ? range[0] : 0;
  const end = Number.isFinite(range[1]) ? range[1] : start;
  return [start, end];
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function diffLine(entry) {
  const row = el("div", `diff-line diff-line--${entry.type}`);
  const mark = entry.type === "add" ? "+" : entry.type === "del" ? "-" : " ";
  row.appendChild(el("span", "diff-line-mark", mark));
  row.appendChild(el("span", "diff-line-text", entry.text.length ? entry.text : " "));
  return row;
}

/** The current bytes at one target's span, through the bridge.
 *
 * Returns `{text, missing}`: a read that failed or was refused is reported as `missing`
 * rather than rendered as an empty diff -- an empty diff would read as "this replaces
 * nothing".
 */
async function fetchSpan(filePath, start, end) {
  try {
    const res = await api.get_source_span(filePath, start, end);
    if (res && res.success === false) {
      return { text: "", missing: true, error: res.error || "" };
    }
    return {
      text: String((res && res.text) || ""),
      missing: !(res && res.found),
    };
  } catch (err) {
    return { text: "", missing: true, error: String((err && err.message) || err) };
  }
}

function renderBody(body, target, current) {
  body.textContent = "";
  const op = operationOf(target);

  if (current.missing) {
    body.appendChild(el(
      "div",
      "diff-loading",
      current.error
        ? `Could not read the current bytes: ${current.error}`
        : "The file does not exist yet, so there are no current bytes to replace.",
    ));
    return;
  }

  // A delete ignores its content (the executor removes the span), so saying so is more
  // honest than diffing against bytes that will not be used.
  const proposed = op === "delete" ? "" : String(target.content || "");
  const entries = lineDiff(current.text, proposed);
  if (!entries.length) {
    body.appendChild(el("div", "diff-loading", "No change: the span and the proposed content are both empty."));
    return;
  }
  for (const entry of entries) body.appendChild(diffLine(entry));
}

/** Wires the per-target editor: reveal a textarea, save through the amendment IPC.
 *
 * The saved payload comes back as a fresh `artifact_planned` event, so the surface repaints
 * from what the store now holds rather than from this call's return value -- the same rule
 * the rest of the review surface follows. Absent a bridge (the browser preview), no editor
 * is offered at all rather than an editor that cannot save.
 */
function attachEditor(head, card, target, artifact, index) {
  const editBtn = el("button", "diff-edit-btn", "Edit");
  editBtn.type = "button";
  head.appendChild(editBtn);

  editBtn.addEventListener("click", () => {
    if (card.querySelector(".diff-editor-wrap")) return;

    const editor = el("textarea", "diff-editor");
    editor.value = String(target.content || "");
    editor.spellcheck = false;
    editor.setAttribute("aria-label", "Proposed content for this AST target");

    const actions = el("div", "diff-editor-actions");
    const cancel = el("button", "diff-cancel-btn", "Cancel");
    cancel.type = "button";
    const save = el("button", "diff-save-btn", "Save edit");
    save.type = "button";
    actions.appendChild(cancel);
    actions.appendChild(save);

    const wrap = el("div", "diff-editor-wrap");
    wrap.appendChild(editor);
    wrap.appendChild(actions);
    card.appendChild(wrap);

    cancel.addEventListener("click", () => wrap.remove());
    save.addEventListener("click", async () => {
      save.disabled = true;
      save.textContent = "Saving\u2026";
      try {
        const res = await api.update_artifact_target(
          String(artifact.task_id || ""),
          index,
          editor.value,
          String(artifact.plan_id || ""),
        );
        if (res && res.success) {
          showToast("Artifact target updated.", "success");
          wrap.remove();
        } else {
          showToast((res && res.error) || "The edit was refused.", "error");
          save.disabled = false;
          save.textContent = "Save edit";
        }
      } catch (err) {
        showToast(`Could not save the edit: ${(err && err.message) || err}`, "error");
        save.disabled = false;
        save.textContent = "Save edit";
      }
    });
  });
}

function renderTargetCard(target, artifact, index) {
  const op = operationOf(target);
  const [start, end] = spanOf(target);
  const card = el("article", `diff-target diff-target--${op}`);

  const head = el("div", "diff-target-head");
  head.appendChild(el("span", `diff-op diff-op--${op}`, op.toUpperCase()));
  head.appendChild(el("span", "diff-file", String(target.file_path || "(no file)")));
  if (target.symbol_name) {
    head.appendChild(el("span", "diff-symbol", String(target.symbol_name)));
  }
  attachEditor(head, card, target, artifact, index);
  head.appendChild(el("span", "diff-span", `bytes ${start}..${end}`));
  card.appendChild(head);

  if (op === "delete") {
    card.appendChild(el("div", "diff-note", "The recorded span is removed; the proposed content is ignored for a delete."));
  } else if (op === "insert") {
    card.appendChild(el("div", "diff-note", "This span is replaced with the proposed content; an unnamed target writes the whole file."));
  }

  const body = el("div", "diff-body");
  body.appendChild(el("div", "diff-loading", "Reading the current bytes\u2026"));
  card.appendChild(body);

  fetchSpan(String(target.file_path || ""), start, end).then((current) => {
    renderBody(body, target, current);
  });
  return card;
}

/** Renders the placeholder the surface shows when there is nothing to review. */
export function renderDiffPlaceholder(message) {
  const container = document.getElementById("dagDiffSurface");
  if (!container) return;
  container.textContent = "";
  container.appendChild(el("div", "dag-diff-empty", message));
}

/** Renders one artifact's AST targets against the bytes they would replace. */
export function renderDiffSurface(artifact) {
  const container = document.getElementById("dagDiffSurface");
  if (!container) return;
  container.textContent = "";

  const summary = el("div", "diff-summary");
  const title = el("strong", "", String(artifact.summary || "Implementation plan"));
  summary.appendChild(title);
  if (artifact.estimated_impact) {
    summary.appendChild(document.createTextNode(` \u2014 ${String(artifact.estimated_impact)}`));
  }
  container.appendChild(summary);

  const targets = Array.isArray(artifact.ast_targets) ? artifact.ast_targets : [];
  if (!targets.length) {
    container.appendChild(el("div", "dag-diff-empty", "This artifact proposes no AST targets."));
    return;
  }
  for (let index = 0; index < targets.length; index += 1) {
    container.appendChild(renderTargetCard(targets[index], artifact, index));
  }
}
