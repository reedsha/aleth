// ui/js/result-view.js — Polymorphic result views: the centre stage's output router.
//
// Every action used to end the same way: the two agent cards stayed on screen with a
// prose summary of what had happened. That is the wrong shape for most of the answers
// this app produces. An implementation is a diff plus a test verdict; an analysis is a
// dashboard of findings; a plan edit is a roadmap diff; a recommendation is a set of
// things to accept or reject. So the centre stage mounts a component chosen by the
// action that just ran instead of one renderer for all six.
//
// The router key is `state.selectedAction`. It is already set when the drawer opens and
// is not cleared by finalizeWorkflow(), so no event payload had to change. `custom` is
// the one genuinely free-form case -- the instruction is unpredictable and the raw
// stream in the cards is the right shape for it -- so it renders nothing new.
//
// The mount is a full-stage overlay, exactly like the live preview in ui/js/preview.js:
// it covers the cards rather than replacing them. That keeps the rule
// checkAllCardsClosed() enforces -- both cards closed returns to the plan workbench --
// completely untouched, so this is an additional surface the user dismisses back to the
// stage rather than a third state the two-state swap has to know about.
//
// What each action's payload actually is, and where it comes from:
//   next_step   -> get_task_diff(state.targetTaskId)      (backup_file_for_task key = task id)
//   fix_bug     -> get_task_diff("bugfix") + summary.root_cause (structured on the backend)
// The two code actions also ask the backend to run the test file the task wrote
// (run_task_tests), so their pass/fail strip is a real verdict instead of a guess.
//   update_plan -> state.planTreeBeforeUpdate vs state.planTree (captured in plan_updated)
//         plus a one-click revert of the revision the run already saved
//   analyze     -> summary.files + summary.deliverables   (no complexity/security metrics exist)
//   recommend   -> summary.proposals                      (one card per proposal)
//   custom      -> nothing; the plain stream is the answer

import { api } from "./api-client.js";
import { escapeHtml } from "./dom.js";
import { showToast } from "./notify.js";
import { openFilesTab } from "./sidebar.js";
import { setHtml, setText } from "./safe-dom.js";
import { DOM, clearPlanTreeBeforeUpdate, clearRunSummary, state } from "./store.js";
import { setWorkbenchView } from "./workbench.js";

const RESULT_LABELS = {
  next_step: { icon: "\u26A1", label: "Execute Next Step" },
  fix_bug: { icon: "\uD83D\uDC1B", label: "Fix Bug" },
  update_plan: { icon: "\u270F\uFE0F", label: "Update Plan" },
  analyze: { icon: "\uD83D\uDD0D", label: "Analyze Codebase" },
  recommend: { icon: "\uD83D\uDCA1", label: "Get Recommendation" }
};

// Bumped on every render and on every reset, so a diff that resolves after the user has
// dismissed the view or started another run cannot paint into the new one.
let resultRenderToken = 0;

// The backup key each action's files were recorded under. `task_diff` reads
// .aleth_backups/<key>/_meta.json, so this has to match what the backend passed to
// backup_file_for_task: the task id for a roadmap step (a targeted fix uses the task it
// answers for), and the two literal action names for untargeted fixes and custom directives.
function resultBackupKey(kind) {
  if (kind === "custom") return "custom";
  if (kind === "fix_bug") return state.targetTaskId ? String(state.targetTaskId) : "bugfix";
  return state.targetTaskId ? String(state.targetTaskId) : "";
}

// --- Mount / dismiss --------------------------------------------------------

// Called on the terminal event. A refusal is not a result -- it is a request for input,
// and the transcript the console is already holding is the answer to it.
export function mountResultView() {
  const kind = state.selectedAction || "custom";
  if (kind === "custom") return;
  if (state.lastRunRefused) return;
  if (!RESULT_LABELS[kind]) return;
  renderResultView(kind);
}

// A new run supersedes whatever the last one left on screen, including the payloads the
// terminal event does not carry.
export function resetResultView() {
  resultRenderToken++;
  state.resultViewOpen = false;
  state.resultViewKind = null;
  clearRunSummary();
  clearPlanTreeBeforeUpdate();
  if (DOM.resultView) DOM.resultView.classList.remove("active");
  refreshResultAffordance();
}

// Dismissing keeps the payload, so the workbench's Result button can bring the view back
// without re-running anything. Without that, dismissing would be a one-way door.
export function closeResultView() {
  state.resultViewOpen = false;
  if (DOM.resultView) DOM.resultView.classList.remove("active");
  refreshResultAffordance();
}

export function openResultView() {
  if (!state.resultViewKind) return;
  renderResultView(state.resultViewKind);
}

// The workbench bar's Result button appears only while a result is available to reopen,
// and never mid-run: during a run the cards own the stage.
function refreshResultAffordance() {
  if (DOM.btnWorkbenchResult) {
    DOM.btnWorkbenchResult.hidden = !state.resultViewKind || !!state.isExecuting;
  }
}

function renderResultView(kind) {
  if (!DOM.resultView || !RESULT_LABELS[kind]) return;
  const token = ++resultRenderToken;
  state.resultViewKind = kind;
  state.resultViewOpen = true;

  const meta = RESULT_LABELS[kind];
  if (DOM.resultIcon) DOM.resultIcon.textContent = meta.icon;
  if (DOM.resultKind) DOM.resultKind.textContent = meta.label;
  if (DOM.resultTitle) DOM.resultTitle.textContent = "";
  if (DOM.resultMetrics) setText(DOM.resultMetrics, "");
  if (DOM.resultBody) setHtml(DOM.resultBody, '<div class="result-note">Reading the result\u2026</div>');
  DOM.resultView.classList.add("active");
  refreshResultAffordance();

  RESULT_RENDERERS[kind](kind, token);
}

function resultStillCurrent(kind, token) {
  return state.resultViewOpen && state.resultViewKind === kind && token === resultRenderToken;
}

// --- Renderers --------------------------------------------------------------

const RESULT_RENDERERS = {
  next_step: renderExecuteResult,
  fix_bug: renderBugfixResult,
  update_plan: renderPlanUpdateResult,
  analyze: renderAnalyzeResult,
  recommend: renderRecommendResult
};

async function loadTaskDiff(key) {
  const empty = { files: [], totals: { files: 0, added: 0, removed: 0 } };
  if (!key) return Object.assign({ found: false }, empty);
  try {
    const res = await api.get_task_diff(key);
    return res && typeof res === "object" ? res : Object.assign({ found: false }, empty);
  } catch (err) {
    return Object.assign({ error: (err && err.message) || String(err) }, empty);
  }
}

function resultFiles(res) {
  return (res && Array.isArray(res.files)) ? res.files : [];
}

// The test verdict is fetched the same way the diff is: on open, from the bridge, with
// the same staleness guard. Opening the result view is the only thing that runs a test.
async function loadTaskTests(key) {
  const none = { ran: false, found: false, verdict: "none" };
  if (!key) return none;
  try {
    const res = await api.run_task_tests(key);
    return res && typeof res === "object" ? res : none;
  } catch (err) {
    return Object.assign({ error: (err && err.message) || String(err) }, none);
  }
}

function resultTotals(res) {
  const totals = (res && res.totals) || {};
  return {
    files: Number(totals.files) || resultFiles(res).length,
    added: Number(totals.added) || 0,
    removed: Number(totals.removed) || 0
  };
}

function resultPill(text, tone) {
  return `<span class="result-pill result-pill-${tone || "neutral"}">${escapeHtml(text)}</span>`;
}

// The diff arrives as `difflib.unified_diff` output: complete lines without terminators,
// headers included. The block header above already names the file and whether it was
// created or modified, so the two file headers are dropped; the hunk offsets are kept,
// because they are what makes a truncated hunk readable as a location.
function resultDiffLinesHtml(lines) {
  const list = (lines || []).map((line) => String(line));
  let start = 0;
  if (list[0] && list[0].startsWith("--- ")) start = 1;
  if (list[start] && list[start].startsWith("+++ ")) start += 1;

  const rows = [];
  list.slice(start).forEach((line) => {
    let cls = "diff-ctx";
    if (line.startsWith("@@")) cls = "diff-hunk";
    else if (line.startsWith("+")) cls = "diff-add";
    else if (line.startsWith("-")) cls = "diff-del";
    rows.push(`<span class="diff-line ${cls}">${escapeHtml(line) || "&nbsp;"}</span>`);
  });
  if (rows.length === 0) {
    rows.push('<span class="diff-line diff-hunk">No textual changes recorded.</span>');
  }
  return rows.join("");
}

function resultDiffBlockHtml(file) {
  const action = file.action === "created" ? "created" : "modified";
  const added = Number(file.added) || 0;
  const removed = Number(file.removed) || 0;
  const body = file.available === false
    // Both sides were binary, oversized or already gone, so there is no text to show.
    // Saying so beats an empty box the reader would take for a change-free file.
    ? '<div class="result-note">Content unavailable (binary, oversized, or removed).</div>'
    : `<div class="result-diff-code">${resultDiffLinesHtml(file.diff)}</div>`;
  return `
    <div class="result-diff-block">
      <div class="result-diff-head">
        <span class="result-diff-file">${escapeHtml(file.filename || "unknown")}</span>
        <span class="result-diff-tag tag-${action}">${action}</span>
        <span class="result-diff-count"><span class="count-add">+${added}</span> <span class="count-del">-${removed}</span></span>
      </div>
      ${body}
    </div>
  `;
}

function resultNoDiffHtml(res) {
  if (res && res.error) {
    return `<div class="result-note">Could not read the diff: ${escapeHtml(res.error)}</div>`;
  }
  return '<div class="result-note">This action recorded no file edits, so there is nothing to diff.</div>';
}

// The pass/fail verdict comes from a real run now (tools/test_runner.py), so the strip
// names exactly what happened instead of implying a run that never occurred. A pass is
// green, an assertion failure is red, and everything that is not a clean result -- a
// collection error, a test file that is gone, no test recorded, or a runner the browser
// preview cannot reach -- is amber and says so.
function resultTestPill(tests, files) {
  const written = (files || []).map((f) => f.filename || "").filter((name) => /test/i.test(name));
  const named = written.length ? written.join(", ") : "the test file";
  const notRun = () => (written.length ? resultPill(`Test written: ${named} \u00B7 not run`, "warn") : "");

  if (!tests) return notRun();
  if (!tests.ran) {
    if (tests.found) return resultPill("Tests: could not run", "warn");
    return notRun();
  }

  const totals = tests.totals || {};
  const passed = Number(totals.passed) || 0;
  const failed = Number(totals.failed) || 0;
  const errors = Number(totals.errors) || 0;

  if (tests.verdict === "passed") return resultPill(`Tests: ${passed} passed`, "add");
  if (failed) {
    const parts = [];
    if (passed) parts.push(`${passed} passed`);
    parts.push(`${failed} failed`);
    if (errors) parts.push(`${errors} error${errors === 1 ? "" : "s"}`);
    return resultPill(`Tests: ${parts.join(", ")}`, "del");
  }
  if (errors) return resultPill(`Tests: ${errors} error${errors === 1 ? "" : "s"}`, "warn");
  return resultPill("Tests: no assertions ran", "warn");
}

async function renderExecuteResult(kind, token) {
  const title = state.targetTaskTitle || "The top pending task";
  if (DOM.resultTitle) DOM.resultTitle.textContent = title;

  const res = await loadTaskDiff(resultBackupKey(kind));
  if (!resultStillCurrent(kind, token)) return;
  const tests = await loadTaskTests(resultBackupKey(kind));
  if (!resultStillCurrent(kind, token)) return;

  const files = resultFiles(res);
  const totals = resultTotals(res);
  if (DOM.resultMetrics) {
    setHtml(DOM.resultMetrics, [
      resultPill(`${totals.files} file${totals.files === 1 ? "" : "s"} changed`, "neutral"),
      resultPill(`+${totals.added}`, "add"),
      resultPill(`-${totals.removed}`, "del"),
      resultTestPill(tests, files)
    ].join(""));
  }

  if (files.length === 0) {
    setHtml(DOM.resultBody, resultNoDiffHtml(res));
    return;
  }
  setHtml(DOM.resultBody, `<div class="result-diff-list">${files.map(resultDiffBlockHtml).join("")}</div>`);
}

async function renderBugfixResult(kind, token) {
  const summary = state.lastSummary || {};
  if (DOM.resultTitle) DOM.resultTitle.textContent = summary.title || "Bug diagnosis and patch";

  const res = await loadTaskDiff(resultBackupKey(kind));
  if (!resultStillCurrent(kind, token)) return;
  const tests = await loadTaskTests(resultBackupKey(kind));
  if (!resultStillCurrent(kind, token)) return;

  const files = resultFiles(res);
  const totals = resultTotals(res);
  if (DOM.resultMetrics) {
    setHtml(DOM.resultMetrics, [
      resultPill(`${totals.files} file${totals.files === 1 ? "" : "s"} patched`, "neutral"),
      resultPill(`+${totals.added}`, "add"),
      resultPill(`-${totals.removed}`, "del"),
      resultTestPill(tests, files)
    ].join(""));
  }

  const rootCause = summary.root_cause
    ? escapeHtml(summary.root_cause)
    : "The diagnosis was streamed to the agent cards rather than recorded as a field.";
  const fixSpec = summary.fix_spec ? `<div class="result-cause-spec">${escapeHtml(summary.fix_spec)}</div>` : "";
  const targets = (summary.files || []).filter(Boolean)
    .map((f) => `<span class="result-file-chip">${escapeHtml(f)}</span>`).join("");

  const patch = files.length === 0
    ? resultNoDiffHtml(res)
    : `<div class="result-diff-list">${files.map(resultDiffBlockHtml).join("")}</div>`;

  setHtml(DOM.resultBody, `
    <div class="result-split">
      <section class="result-pane result-pane-cause">
        <h3 class="result-pane-title">Root cause</h3>
        <p class="result-cause-text">${rootCause}</p>
        ${fixSpec}
        ${targets ? `<div class="result-cause-targets">${targets}</div>` : ""}
      </section>
      <section class="result-pane result-pane-patch">
        <h3 class="result-pane-title">Resolution</h3>
        ${patch}
      </section>
    </div>
  `);
}

// --- Update Plan: the roadmap as a diff -------------------------------------
// The plan is already written to disk by the time this event arrives -- there is no
// staging step for an "Approve Changes" button to confirm. So the view shows the diff and
// says plainly that it is applied, rather than offering a button that only looks like it
// approves something.
function planTaskIndex(tree) {
  const index = {};
  (tree || []).forEach((task) => {
    if (task && task.id) index[String(task.id)] = task;
  });
  return index;
}

function planTreeDiff(before, after) {
  const oldIndex = planTaskIndex(before);
  const newIndex = planTaskIndex(after);
  const added = [];
  const removed = [];
  const changed = [];

  Object.keys(newIndex).forEach((id) => {
    const now = newIndex[id];
    const then = oldIndex[id];
    if (!then) {
      added.push(now);
      return;
    }
    const wasStatus = then.status || "pending";
    const isStatus = now.status || "pending";
    if (wasStatus !== isStatus || (then.title || "") !== (now.title || "")) {
      changed.push({ task: now, from: wasStatus, to: isStatus });
    }
  });
  Object.keys(oldIndex).forEach((id) => {
    if (!newIndex[id]) removed.push(oldIndex[id]);
  });

  return { added: added, removed: removed, changed: changed };
}

function planDiffRowHtml(task, tone) {
  const state = task.status === "completed"
    ? "completed"
    : (task.status === "in_progress" ? "in_progress" : (task.status === "failed" ? "failed" : "pending"));
  return `
    <div class="result-tree-row row-${tone}">
      <span class="result-tree-mark">${tone === "added" ? "+" : (tone === "removed" ? "\u2212" : "\u2192")}</span>
      <span class="result-tree-title">${escapeHtml(task.title || task.id || "untitled task")}</span>
      <span class="result-tree-state">${escapeHtml(state)}</span>
    </div>
  `;
}

function renderPlanUpdateResult(kind, token) {
  const summary = state.lastSummary || {};
  const before = state.planTreeBeforeUpdate;
  const after = state.planTree || [];
  if (DOM.resultTitle) DOM.resultTitle.textContent = summary.title || `Roadmap revision in ${state.activePlan || "PLAN.md"}`;

  const diff = planTreeDiff(before, after);
  const counted = diff.added.length + diff.removed.length + diff.changed.length;
  if (DOM.resultMetrics) {
    setHtml(DOM.resultMetrics, [
      resultPill(`${after.length} tasks`, "neutral"),
      resultPill(`+${diff.added.length}`, "add"),
      resultPill(`\u2212${diff.removed.length}`, "del"),
      resultPill(`${diff.changed.length} updated`, "neutral")
    ].join(""));
  }

  const column = (title, rows, tone) => `
    <section class="result-pane">
      <h3 class="result-pane-title">${title} <span class="result-count">${rows.length}</span></h3>
      ${rows.length ? rows.map((task) => planDiffRowHtml(task, tone)).join("") : '<div class="result-note">None</div>'}
    </section>
  `;

  if (counted === 0) {
    setHtml(DOM.resultBody, `
      <div class="result-note">The roadmap's tasks are unchanged; only the surrounding text may differ.</div>
      ${planUpdateFooterHtml()}
    `);
    return;
  }

  setHtml(DOM.resultBody, `
    <div class="result-tree-diff">
      ${column("Added", diff.added, "added")}
      ${column("Removed", diff.removed, "removed")}
      ${diff.changed.length
        ? `<section class="result-pane">
             <h3 class="result-pane-title">Updated <span class="result-count">${diff.changed.length}</span></h3>
             ${diff.changed.map((c) => `
               <div class="result-tree-row row-changed">
                 <span class="result-tree-mark">\u2192</span>
                 <span class="result-tree-title">${escapeHtml(c.task.title || c.task.id || "untitled task")}</span>
                 <span class="result-tree-state">${escapeHtml(c.from)} \u2192 ${escapeHtml(c.to)}</span>
               </div>`).join("")}
           </section>`
        : ""}
    </div>
    ${planUpdateFooterHtml()}
  `);
}

// Update Plan writes the plan as the run finishes, so there is nothing left to "approve" -- the
// write has already happened by the time this renders. The honest control is an undo, and the
// note says so, so the button's effect matches its label. The plan is only touched on the click.
function planUpdateFooterHtml() {
  return `
    <div class="result-note">This revision is already saved. "Revert Changes" restores the roadmap to the state before this run.</div>
    <div class="result-plan-footer">
      <span class="result-pill result-pill-ok">Applied to ${escapeHtml(state.activePlan || "PLAN.md")}</span>
      <button class="result-action" data-result-action="revert-plan" type="button">Revert Changes</button>
      <button class="result-action" data-result-action="view-plan" type="button">View in workbench</button>
    </div>
  `;
}

// --- Analyze: the structured dashboard --------------------------------------
// Every number here is one the backend actually sent. The source metrics are computed on
// the backend (tools/code_metrics.py) so the complexity and security widgets are measured
// rather than drawn as zeroes; when a run carries no metrics the dashboard says so instead
// of implying a clean bill of health.
function resultStat(value, label) {
  return `<div class="result-stat">
      <span class="result-stat-value">${escapeHtml(String(value))}</span>
      <span class="result-stat-label">${escapeHtml(label)}</span>
    </div>`;
}

// One "needs attention" row: where it is, then what it is. Shared by the complexity and the
// security panes because a reader scans both the same way.
function resultAttentionList(rows) {
  if (!rows.length) return '<div class="result-note">None found.</div>';
  return `<ul class="result-list">${rows.map((row) =>
    `<li><code class="result-inline-code">${escapeHtml(row.where)}</code> ${escapeHtml(row.what)}</li>`
  ).join("")}</ul>`;
}

function renderAnalyzeResult(kind, token) {
  const summary = state.lastSummary || {};
  const files = (summary.files || []).filter(Boolean);
  const findings = (summary.deliverables || []).filter(Boolean);
  const metrics = summary.metrics || null;
  if (DOM.resultTitle) DOM.resultTitle.textContent = summary.title || "Codebase analysis";

  if (DOM.resultMetrics) {
    setHtml(DOM.resultMetrics, [
      resultPill(summary.status || "Analysis Complete", "neutral"),
      resultPill(`${files.length} file${files.length === 1 ? "" : "s"} inspected`, "neutral"),
      resultPill(`${findings.length} finding${findings.length === 1 ? "" : "s"}`, "neutral")
    ].join(""));
  }

  const statTiles = [
    resultStat(files.length, "Files inspected"),
    resultStat(findings.length, "Findings reported")
  ];
  if (metrics) {
    statTiles.push(
      resultStat(metrics.modules, "Modules parsed"),
      resultStat(metrics.functions, "Functions"),
      resultStat(metrics.average_complexity, "Avg complexity"),
      resultStat(metrics.hotspot_count, `Hotspots (\u2265${metrics.hotspot_threshold})`),
      resultStat(metrics.security_flag_count, "Security flags")
    );
  }

  const hotspots = (metrics && metrics.hotspots) || [];
  const flags = (metrics && metrics.security_flags) || [];
  const unparsed = (metrics && metrics.unparsed) || [];

  const metricsPanes = metrics ? `
      <div class="result-two-col">
        <section class="result-pane">
          <h3 class="result-pane-title">Complexity hotspots <span class="result-count">${metrics.hotspot_count}</span></h3>
          ${resultAttentionList(hotspots.map((h) => ({
            where: `${h.file}:${h.line}`,
            what: `${h.name}() \u00B7 complexity ${h.complexity}`
          })))}
        </section>
        <section class="result-pane">
          <h3 class="result-pane-title">Security flags <span class="result-count">${metrics.security_flag_count}</span></h3>
          ${resultAttentionList(flags.map((f) => ({ where: `${f.file}:${f.line}`, what: f.kind })))}
        </section>
      </div>` : "";

  const metricsNote = metrics
    ? '<div class="result-note">Complexity is cyclomatic, counted per function; security flags are a static review of known-risky calls. Nothing here executes the code.</div>'
    : '<div class="result-note">No source metrics were computed for this run; only the inspection results above are real.</div>';
  const unparsedNote = unparsed.length
    ? `<div class="result-note">${unparsed.length} module${unparsed.length === 1 ? "" : "s"} could not be parsed and ${unparsed.length === 1 ? "is" : "are"} excluded: ${unparsed.map(escapeHtml).join(", ")}.</div>`
    : "";

  setHtml(DOM.resultBody, `
    <div class="result-dashboard">
      <div class="result-stat-grid">${statTiles.join("")}</div>
      <div class="result-two-col">
        <section class="result-pane">
          <h3 class="result-pane-title">Findings</h3>
          ${findings.length
            ? `<ul class="result-list">${findings.map((f) => `<li>${escapeHtml(f)}</li>`).join("")}</ul>`
            : '<div class="result-note">No findings were reported.</div>'}
        </section>
        <section class="result-pane">
          <h3 class="result-pane-title">Files touched by the scan</h3>
          ${files.length
            ? `<div class="result-file-list">${files.map((f) => `
                 <button class="result-file-chip is-action" type="button"
                   data-result-action="reveal-file" data-result-value="${escapeHtml(f)}"
                   title="Show ${escapeHtml(f)} in the Files tab">${escapeHtml(f)}</button>`).join("")}</div>`
            : '<div class="result-note">The scan named no files.</div>'}
        </section>
      </div>
      ${metricsPanes}
      ${unparsedNote}
      ${metricsNote}
    </div>
  `);
}

// --- Recommend: one actionable card per proposal ---------------------------
// The proposals already existed as a plain <ul> in the architect's summary. As cards with
// an action they become the interactive objects they are: each one is a real plan mutation
// through the bridge's add_plan_task endpoint, which appends the proposal as a pending task
// and saves it -- one click, no confirm step, because the click is the confirmation.
function renderRecommendResult(kind, token) {
  const summary = state.lastSummary || {};
  const proposals = (summary.proposals || []).filter(Boolean);
  const findings = (summary.deliverables || []).filter(Boolean);
  if (DOM.resultTitle) DOM.resultTitle.textContent = summary.title || "Strategic recommendations";

  if (DOM.resultMetrics) {
    setHtml(DOM.resultMetrics, [
      resultPill(summary.status || "Advisory Formulated", "neutral"),
      resultPill(`${proposals.length} proposal${proposals.length === 1 ? "" : "s"}`, "neutral")
    ].join(""));
  }

  const cards = proposals.map((proposal, index) => `
    <article class="result-card">
      <span class="result-card-index">${index + 1}</span>
      <p class="result-card-text">${escapeHtml(proposal)}</p>
      <button class="result-action" type="button"
        data-result-action="add-to-plan" data-result-value="${escapeHtml(proposal)}">Add to Plan</button>
    </article>
  `).join("");

  setHtml(DOM.resultBody, `
    <div class="result-cards-wrap">
      <div class="result-cards">
        ${proposals.length ? cards : '<div class="result-note">No proposals were returned.</div>'}
      </div>
      ${findings.length
        ? `<section class="result-pane result-context">
             <h3 class="result-pane-title">Why the Architect proposed this</h3>
             <ul class="result-list">${findings.map((f) => `<li>${escapeHtml(f)}</li>`).join("")}</ul>
           </section>`
        : ""}
      <div class="result-note">"Add to Plan" appends this proposal to the active plan as a pending task and saves it \u2014 the click is the confirmation, so there is no second step.</div>
    </div>
  `);
}

// --- Interactions -----------------------------------------------------------
// Delegated from the mount, because every renderer rewrites the body wholesale.
export function handleResultViewClick(event) {
  const button = event.target && event.target.closest
    ? event.target.closest("[data-result-action]")
    : null;
  if (!button) return;
  const action = button.getAttribute("data-result-action");
  const value = button.getAttribute("data-result-value") || "";

  if (action === "reveal-file") {
    if (typeof openFilesTab === "function") openFilesTab();
    showToast(`Showing workspace files \u2014 look for ${value}`, "info");
    return;
  }
  if (action === "view-plan") {
    closeResultView();
    if (typeof setWorkbenchView === "function") setWorkbenchView("tree");
    return;
  }
  if (action === "add-to-plan") {
    const directive = String(value).trim();
    if (!directive) return;
    addRecommendationToPlan(directive, button);
    return;
  }
  if (action === "revert-plan") {
    revertPlanUpdate(button);
    return;
  }
}

// One click, one real mutation: the bridge appends the proposal as a pending task and
// persists it through the ordinary save_plan_state + plan_updated funnel, so the tree,
// the workbench and the progress meter refresh from it like any other plan write. The
// button disables while the write is in flight, then reads "Added" rather than inviting a
// second click that would add the same task twice.
async function addRecommendationToPlan(directive, button) {
  if (button) button.disabled = true;
  try {
    const res = await api.add_plan_task(directive);
    if (button) button.textContent = "Added";
    showToast(`Added to ${res.filename || "the plan"} as a pending task.`, "success");
  } catch (err) {
    if (button) button.disabled = false;
    showToast(`Could not add the task: ${err}`, "error");
  }
}

// The counterpart of "Add to Plan": a real mutation, in the opposite direction. The run has
// already written the plan, so reverting restores the snapshot the backend took immediately
// before that write. The button disables while the round-trip is in flight, then reads
// "Reverted" rather than inviting a second click that would roll the plan back twice.
async function revertPlanUpdate(button) {
  if (button) button.disabled = true;
  try {
    await api.revert_plan_update();
    if (button) button.textContent = "Reverted";
    showToast("Roadmap restored to its previous revision.", "success");
  } catch (err) {
    if (button) button.disabled = false;
    showToast(`Could not revert the revision: ${err}`, "error");
  }
}

export function initResultView() {
  closeResultView();
}
