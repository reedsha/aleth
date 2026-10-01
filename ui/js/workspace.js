// ui/js/workspace.js — Workspace file browser, codebase sync audit, and task rollback modals.
import { api } from "./api-client.js";
import { escapeHtml } from "./dom.js";
import { showToast } from "./notify.js";
import { planStateSummaryHtml } from "./plan-tree.js";
import { setHtml } from "./safe-dom.js";
import { DOM, state } from "./store.js";

let cachedWorkspaceFiles = [];

export async function openWorkspaceFilesModal() {
  if (DOM.modalFilesList) setHtml(DOM.modalFilesList, "<div style='color: #64748b; font-size: 12px; padding: 20px; text-align: center;'>Loading workspace files...</div>");
  if (DOM.filesModalOverlay) DOM.filesModalOverlay.style.display = "flex";
  if (DOM.inputSearchWorkspaceFiles) DOM.inputSearchWorkspaceFiles.value = "";

  // Assigned on both paths below (the read and the failure), so there is no initialiser to
  // carry a value nothing reads.
  let files;
  try {
    const info = await api.get_workspace_info();
    files = info.files || [];
  } catch (e) {
    // A failed read must not render as "No files in workspace yet." (audit H11).
    showToast(`Could not read the workspace: ${(e && e.message) || e}`, "error");
    files = [];
  }

  cachedWorkspaceFiles = files;
  renderFilteredWorkspaceFiles("");
  // The modal overlay was just shown in this same task, so it has no box to receive focus
  // yet; the next frame focuses after layout instead of after a fixed 60 ms guess.
  const search = DOM.inputSearchWorkspaceFiles;
  if (search) {
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(() => search.focus && search.focus());
    else if (search.focus) search.focus();
  }
}

export function renderFilteredWorkspaceFiles(query = "") {
  if (!DOM.modalFilesList) return;
  const q = (query || "").toLowerCase().trim();
  const filtered = q
    ? cachedWorkspaceFiles.filter(f => (f.path || f.name || "").toLowerCase().includes(q))
    : cachedWorkspaceFiles;

  if (DOM.txtFilesCountBadge) {
    DOM.txtFilesCountBadge.textContent = `${filtered.length} of ${cachedWorkspaceFiles.length} file(s)`;
  }

  if (filtered.length === 0) {
    setHtml(DOM.modalFilesList, `<div style="color: #64748b; font-size: 13px; text-align: center; padding: 30px;">${q ? "No files match filter." : "No files in workspace yet."}</div>`);
    return;
  }

  // Cap initial render to 250 items to keep DOM instant and responsive
  const displayLimit = 250;
  const itemsToRender = filtered.slice(0, displayLimit);
  let html = itemsToRender.map(f => {
    const sizeStr = f.size ? `${(f.size / 1024).toFixed(1)} KB` : "0 KB";
    const filePath = escapeHtml(f.path || f.name);
    return `
      <div class="file-row">
        <span style="color: #38bdf8; font-family: var(--font-mono); font-size: 11.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${filePath}">📄 ${filePath}</span>
        <span style="color: #64748b; font-size: 10.5px; font-family: var(--font-mono); flex-shrink: 0;">${sizeStr}</span>
      </div>
    `;
  }).join("");

  if (filtered.length > displayLimit) {
    html += `<div style="color: #64748b; font-size: 11px; text-align: center; padding: 8px;">Showing first ${displayLimit} of ${filtered.length} files. Use filter search above for specific files.</div>`;
  }

  setHtml(DOM.modalFilesList, html);
}

export function closeWorkspaceFilesModal() {
  if (DOM.filesModalOverlay) DOM.filesModalOverlay.style.display = "none";
}

// ============================================================================
// Module 5: Codebase & Plan Sync Audit Modal
// ============================================================================
export async function openAuditModal() {
  // Show modal immediately with scanning state
  setAuditRailMode("audit");
  DOM.auditModalOverlay.style.display = "flex";
  if (DOM.auditStatusPill) {
    DOM.auditStatusPill.textContent = "Scanning...";
    DOM.auditStatusPill.className = "audit-status-pill scanning";
  }
  if (DOM.auditStatusSummary) DOM.auditStatusSummary.textContent = "Analyzing workspace files vs plan.json...";
  if (DOM.listAuditMissing) setHtml(DOM.listAuditMissing, "<em style='color:#64748b;font-size:11px;'>Loading...</em>");
  if (DOM.listAuditExisting) setHtml(DOM.listAuditExisting, "<em style='color:#64748b;font-size:11px;'>Loading...</em>");
  if (DOM.badgeMissingFilesCount) DOM.badgeMissingFilesCount.textContent = "…";
  if (DOM.badgeExistingFilesCount) DOM.badgeExistingFilesCount.textContent = "…";
  if (DOM.txtUntrackedFiles) DOM.txtUntrackedFiles.textContent = "Scanning...";

  // Disable resolution buttons until results are loaded
  if (DOM.btnSyncPlanToCode) DOM.btnSyncPlanToCode.disabled = true;
  if (DOM.btnForceCodeToPlan) DOM.btnForceCodeToPlan.disabled = true;

  try {
    const audit = await api.audit_codebase_sync();

    if (audit.error) {
      showToast(`Audit error: ${audit.error}`, "error");
      closeAuditModal();
      return;
    }

    renderAuditResults(audit);
  } catch (err) {
    showToast(`Audit failed: ${err.message}`, "error");
    closeAuditModal();
  }
}

// One rail, two lenses. `audit` runs the codebase/plan sync scan; `summary` is the bento
// header's "Expand Detail" -- the plan's `## Global State Summary` shown in full, the same
// text the tile clamps behind three lines. Swapping the mode is a class on the overlay, so
// the CSS folds away whichever half is not in play and the rail never doubles up.
function setAuditRailMode(mode) {
  if (!DOM.auditModalOverlay) return;
  const summary = mode === "summary";
  DOM.auditModalOverlay.classList.toggle("summary-mode", summary);
  if (DOM.auditHeaderTitle) DOM.auditHeaderTitle.textContent = summary
    ? "Global Architecture & State"
    : "Codebase & Plan Sync Audit";
  if (DOM.auditHeaderSubtitle) DOM.auditHeaderSubtitle.textContent = summary
    ? "Standing context for the active plan"
    : "Two-Phase Discrepancy Reconciliation";
}

// The bento summary tile's expand affordance: the full standing context, in the rail.
export function openPlanSummaryPanel() {
  if (!DOM.auditModalOverlay) return;
  setAuditRailMode("summary");
  if (DOM.txtAuditSummaryFull) {
    setHtml(DOM.txtAuditSummaryFull, planStateSummaryHtml(state.planJson)
      || '<div class="plan-summary-bullet">No global state summary in this plan.</div>');
  }
  DOM.auditModalOverlay.style.display = "flex";
}

function renderAuditResults(audit) {
  // Status pill
  if (DOM.auditStatusPill) {
    if (audit.in_sync) {
      DOM.auditStatusPill.textContent = "In Sync ✓";
      DOM.auditStatusPill.className = "audit-status-pill in-sync";
    } else {
      DOM.auditStatusPill.textContent = `${audit.discrepancy_count} Discrepancies`;
      DOM.auditStatusPill.className = "audit-status-pill discrepancy";
    }
  }
  if (DOM.auditStatusSummary) DOM.auditStatusSummary.textContent = audit.summary || "";

  // Column 1: Completed tasks with missing files
  const missingItems = audit.completed_missing_files || [];
  if (DOM.badgeMissingFilesCount) DOM.badgeMissingFilesCount.textContent = missingItems.length;
  if (DOM.listAuditMissing) {
    if (missingItems.length === 0) {
      setHtml(DOM.listAuditMissing, "<span style='color:#64748b;font-size:11px;'>No completed tasks with missing deliverables.</span>");
    } else {
      setHtml(DOM.listAuditMissing, missingItems.map(item => `
        <div class="audit-item">
          <div class="audit-item-title">${escapeHtml(item.title)}</div>
          <div class="audit-item-section">${escapeHtml(item.section || "")}</div>
          <div class="audit-item-files">${(item.missing_files || []).map(f => `<code>${escapeHtml(f)}</code>`).join(", ")}</div>
        </div>
      `).join(""));
    }
  }

  // Column 2: Pending tasks with existing files
  const existingItems = audit.pending_existing_files || [];
  if (DOM.badgeExistingFilesCount) DOM.badgeExistingFilesCount.textContent = existingItems.length;
  if (DOM.listAuditExisting) {
    if (existingItems.length === 0) {
      setHtml(DOM.listAuditExisting, "<span style='color:#64748b;font-size:11px;'>No pending tasks with pre-existing code.</span>");
    } else {
      setHtml(DOM.listAuditExisting, existingItems.map(item => `
        <div class="audit-item">
          <div class="audit-item-title">${escapeHtml(item.title)}</div>
          <div class="audit-item-section">${escapeHtml(item.section || "")}</div>
          <div class="audit-item-files">${(item.existing_files || []).map(f => `<code>${escapeHtml(f)}</code>`).join(", ")}</div>
        </div>
      `).join(""));
    }
  }

  // Untracked files
  const untracked = audit.untracked_files || [];
  if (DOM.txtUntrackedFiles) {
    if (untracked.length === 0) {
      setHtml(DOM.txtUntrackedFiles, "<span style='color:#64748b;'>None</span>");
    } else {
      const maxShow = 40;
      const preview = untracked.slice(0, maxShow).map(f => `<code>${escapeHtml(f)}</code>`).join(" ");
      const extra = untracked.length > maxShow ? ` <span style='color:#64748b; font-size:10.5px;'>...and ${untracked.length - maxShow} more</span>` : "";
      setHtml(DOM.txtUntrackedFiles, preview + extra);
    }
  }

  // Enable resolution buttons only if there are discrepancies
  if (DOM.btnSyncPlanToCode) DOM.btnSyncPlanToCode.disabled = audit.in_sync;
  if (DOM.btnForceCodeToPlan) DOM.btnForceCodeToPlan.disabled = audit.in_sync;
}

export async function handleAuditResolution(resolutionType) {
  if (DOM.btnSyncPlanToCode) DOM.btnSyncPlanToCode.disabled = true;
  if (DOM.btnForceCodeToPlan) DOM.btnForceCodeToPlan.disabled = true;

  const label = resolutionType === "plan_to_code" ? "Syncing plan to codebase" : "Forcing code to match plan";
  showToast(`${label}...`, "info");

  try {
    await api.resolve_sync(resolutionType);

    showToast(`✓ ${label} completed successfully!`, "success");
    // Plan data will be refreshed via plan_updated event from backend
    closeAuditModal();
  } catch (err) {
    showToast(`Resolution error: ${err.message}`, "error");
    if (DOM.btnSyncPlanToCode) DOM.btnSyncPlanToCode.disabled = false;
    if (DOM.btnForceCodeToPlan) DOM.btnForceCodeToPlan.disabled = false;
  }
}

export function closeAuditModal() {
  DOM.auditModalOverlay.style.display = "none";
}

// ============================================================================
// Module 8: Rollback Modal (Severe Confirmation)
// ============================================================================
let rollbackTargetTask = null;

export function openRollbackModal(task) {
  rollbackTargetTask = task;

  // Populate task title
  if (DOM.rollbackTargetTitle) DOM.rollbackTargetTitle.textContent = task.title || "Unknown Task";

  // Populate affected deliverables list
  if (DOM.rollbackFilesList) {
    const files = task.files || [];
    // Also extract from details
    const detailFiles = [];
    if (task.details) {
      task.details.forEach(d => {
        const found = d.match(/`([^`]+\.[a-zA-Z0-9]+)`/g);
        if (found) found.forEach(f => detailFiles.push(f.replace(/`/g, "")));
      });
    }
    const allFiles = [...new Set([...files, ...detailFiles])];

    if (allFiles.length === 0) {
      setHtml(DOM.rollbackFilesList, "<li style='color:#64748b;'>No specific deliverables recorded for this task.</li>");
    } else {
      setHtml(DOM.rollbackFilesList, allFiles.map(f =>
        `<li><code>${escapeHtml(f)}</code></li>`
      ).join(""));
    }
  }

  // Reset confirmation state
  if (DOM.chkRollbackConfirm) DOM.chkRollbackConfirm.checked = false;
  if (DOM.btnConfirmRollbackAction) DOM.btnConfirmRollbackAction.disabled = true;

  DOM.rollbackModalOverlay.style.display = "flex";
}

export function closeRollbackModal() {
  DOM.rollbackModalOverlay.style.display = "none";
  rollbackTargetTask = null;
}

export async function handleConfirmRollback() {
  if (!rollbackTargetTask) return;

  const taskId = rollbackTargetTask.id;
  const taskTitle = rollbackTargetTask.title;

  // Disable button to prevent double-click
  if (DOM.btnConfirmRollbackAction) DOM.btnConfirmRollbackAction.disabled = true;

  showToast(`Rolling back "${taskTitle}"...`, "info");

  try {
    const res = await api.rollback_task(taskId);

    const restoredMsg = (res.restored_files && res.restored_files.length > 0)
      ? ` Restored ${res.restored_files.length} file(s).`
      : "";
    showToast(`✓ "${taskTitle}" rolled back to Pending.${restoredMsg}`, "success");
    closeRollbackModal();
    // Plan will be refreshed via plan_updated event from backend
  } catch (err) {
    showToast(`Rollback error: ${err.message}`, "error");
    if (DOM.btnConfirmRollbackAction) DOM.btnConfirmRollbackAction.disabled = false;
  }
}
