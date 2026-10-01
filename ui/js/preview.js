// ui/js/preview.js — The live preview: the workspace's generated interface, rendered in
// place instead of described.
//
// How the document gets in matters. The obvious approach -- point an iframe at the file's
// file:// URL -- does not work here: WebView2 never finishes that local document load, so
// the frame sits white until it is replaced by an error. The preview therefore reads the
// file through the bridge, exactly as the plan workbench reads PLAN.md, and hands the text
// to the frame with srcdoc. That leaves no document fetch at all, which is the same reason
// the frontend is inlined in the first place, and it means the frame is never loaded at
// startup: it has no src and no srcdoc until a preview is asked for.

import { api } from "./api-client.js";
import { showToast } from "./notify.js";
import { sanitizePreviewHtml } from "./safe-dom.js";
import { DOM, state } from "./store.js";

const PREVIEW_FILENAME = "ui_view.html";

// The frame holds workspace output, not a trusted document, so it is sandboxed without
// `allow-scripts` and without `allow-same-origin`: a script that survives the sanitiser
// would run in an opaque origin with no reach into the app. The attribute is stated here as
// well as in ui/index.html so a later edit to the markup cannot quietly re-admit either
// capability, and every srcdoc write goes through one function that applies both.
const PREVIEW_SANDBOX = "allow-forms allow-modals allow-popups";

// An empty document, so closing the pane tears the loaded one down without the frame
// falling back to rendering anything of its own.
const PREVIEW_BLANK = "<!DOCTYPE html><html><head><meta charset=\"utf-8\"></head><body></body></html>";

// srcdoc parsing is synchronous, so this fires only on something genuinely stuck. It stays
// as a guard rather than an expectation: a pane left silently blank would be
// indistinguishable from a preview of a blank page.
const PREVIEW_LOAD_TIMEOUT_MS = 4000;

let previewLoadTimer = null;

function clearPreviewTimer() {
  if (previewLoadTimer) {
    clearTimeout(previewLoadTimer);
    previewLoadTimer = null;
  }
}

function showPreviewError(message) {
  clearPreviewTimer();
  if (DOM.previewFailMsg) DOM.previewFailMsg.textContent = message;
  if (DOM.previewFail) DOM.previewFail.classList.add("active");
}

function hidePreviewError() {
  if (DOM.previewFail) DOM.previewFail.classList.remove("active");
}

// The single srcdoc sink: sanitise, then install. DOMPurify strips `<script>`, `on*`
// handlers and the embedded-content tags from the workspace's document, and the sandbox
// attribute is (re)applied here so the two guarantees travel together.
function setPreviewDocument(frame, html) {
  if (!frame) return;
  frame.setAttribute("sandbox", PREVIEW_SANDBOX);
  frame.setAttribute("srcdoc", sanitizePreviewHtml(html));
}

function loadPreviewFrame(html) {
  const frame = DOM.previewFrame;
  if (!frame) return;

  hidePreviewError();
  clearPreviewTimer();
  previewLoadTimer = setTimeout(() => {
    showPreviewError(
      "The preview did not render. Press Reload to try again, and report it if it keeps " +
      "happening.");
  }, PREVIEW_LOAD_TIMEOUT_MS);

  setPreviewDocument(frame, html);
}

function handlePreviewFrameLoad() {
  // Any load, including the blank document that closing the pane installs, stands the guard
  // down: a timer firing after a successful load would replace a working preview with an
  // error panel.
  clearPreviewTimer();
  hidePreviewError();
}

function syncPreviewToggle(on) {
  if (!DOM.btnTogglePreview) return;
  DOM.btnTogglePreview.classList.toggle("active", !!on);
  DOM.btnTogglePreview.setAttribute("aria-pressed", on ? "true" : "false");
}

// Re-reads the file and re-renders the frame. One function covers "open" and "reload"
// because both need the same answers -- is the bridge there, is there an interface file,
// what is in it -- and splitting them would let the two drift. A missing file names itself
// here rather than surfacing as a blank frame, which the load event cannot tell apart from
// an empty page.
export async function refreshPreview() {
  if (!state.previewOpen) return;

  if (DOM.previewPath) DOM.previewPath.textContent = PREVIEW_FILENAME;

  let result;
  try {
    result = await api.get_preview_source();
  } catch (err) {
    if (!state.previewOpen) return;
    showPreviewError(`Could not read ${PREVIEW_FILENAME}: ${(err && err.message) || err}`);
    return;
  }
  if (!state.previewOpen) return;

  if (!result.found) {
    showPreviewError(
      `No ${PREVIEW_FILENAME} in this workspace yet. Run a task that builds the interface, ` +
      "then press Reload.");
    return;
  }

  loadPreviewFrame(result.content || "");

  // The render is already up; the note only says the file was too big to take whole.
  if (result.truncated) {
    showToast(`${PREVIEW_FILENAME} is very large and was previewed truncated.`, "info");
  }
}

function openPreview() {
  if (!DOM.previewPane) return;
  DOM.previewPane.classList.add("active");
  state.previewOpen = true;
  syncPreviewToggle(true);
  refreshPreview();
}

export function closePreview() {
  state.previewOpen = false;
  if (DOM.previewPane) DOM.previewPane.classList.remove("active");
  clearPreviewTimer();
  hidePreviewError();
  // Installing the blank document ends the rendered one, so a closed preview holds no live
  // page. setAttribute is also the only frame method the startup-contract harness provides.
  setPreviewDocument(DOM.previewFrame, PREVIEW_BLANK);
  syncPreviewToggle(false);
}

export function togglePreview() {
  if (state.previewOpen) closePreview(); else openPreview();
}

export function initPreview() {
  // The frame starts with no document at all, so nothing renders until a preview is asked
  // for. The load listener is the timeout guard's other half.
  if (DOM.previewFrame) DOM.previewFrame.addEventListener("load", handlePreviewFrameLoad);
  closePreview();
}
