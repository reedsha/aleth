// ui/js/env.js — The active environment variables panel in the left sidebar.
//
// Names and a mask only. The backend never sends a value: masking happens in Python, so a
// devtools session cannot read a secret back out of the payload. The per-row reveal shows
// what is safe to show -- whether the variable resolves, and how long its value is -- which
// is enough to tell a set-but-empty variable from a populated one.
//
// **Read through the HTTP client.** The panel asks the gateway with
// `api.get_environment_variables()`, so it works in a plain browser as in the desktop window:
// there is no bridge and no desktop-only path.

import { api } from "./api-client.js";
import { escapeHtml } from "./dom.js";
import { showToast } from "./notify.js";
import { setHtml } from "./safe-dom.js";
import { DOM } from "./store.js";

const ENV_MASK_TEXT = "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022";

const ENV_REVEAL_ICON =
  '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
  'stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>' +
  '<circle cx="12" cy="12" r="3"/></svg>';

function envPanelAvailable() {
  // The panel is served by the gateway, so the only question is whether the client exists at
  // all -- which it does not on a bare document opened outside the app.
  return typeof api.get_environment_variables === "function";
}

function envRowMarkup(entry) {
  const name = escapeHtml((entry && entry.name) || "");
  const isSet = !!(entry && entry.set);
  const length = Number(entry && entry.length) || 0;
  const detail = isSet
    ? `${length} character${length === 1 ? "" : "s"} \u00b7 value stays in the backend`
    : "not set in the environment";

  return (
    `<div class="env-row ${isSet ? "set" : "unset"}">` +
    '<span class="env-dot"></span>' +
    `<span class="env-name" title="${name}">${name}</span>` +
    `<span class="env-mask">${ENV_MASK_TEXT}</span>` +
    `<span class="env-detail">${escapeHtml(detail)}</span>` +
    '<button class="env-reveal" type="button" aria-expanded="false" ' +
    'title="Show what is known about this variable">' + ENV_REVEAL_ICON + "</button>" +
    "</div>"
  );
}

function renderEnvironmentVariables(variables) {
  if (!DOM.sidebarEnvList) return;

  const list = variables || [];
  if (DOM.countEnvVars) DOM.countEnvVars.textContent = String(list.length);

  setHtml(DOM.sidebarEnvList, list.length
    ? list.map(envRowMarkup).join("")
    : '<div class="env-empty">No environment variables are configured.</div>');
}

// One delegated listener rather than one per row: the list is re-rendered wholesale on
// every refresh, which would otherwise leave the old rows' listeners behind.
function handleEnvListClick(event) {
  const target = event && event.target;
  if (!target || typeof target.closest !== "function") return;

  const button = target.closest(".env-reveal");
  if (!button) return;
  const row = button.closest(".env-row");
  if (!row) return;

  const revealed = row.classList.toggle("revealed");
  button.setAttribute("aria-expanded", revealed ? "true" : "false");
  button.title = revealed
    ? "Hide this variable's details"
    : "Show what is known about this variable";
}

export async function refreshEnvironmentVariables() {
  if (!DOM.sidebarEnvList) return;

  if (!envPanelAvailable()) {
    if (DOM.countEnvVars) DOM.countEnvVars.textContent = "0";
    setHtml(DOM.sidebarEnvList,
      '<div class="env-empty">The local gateway is not reachable, so variables cannot be read.</div>');
    return;
  }

  try {
    const res = await api.get_environment_variables();
    renderEnvironmentVariables((res && res.variables) || []);
  } catch (err) {
    // A read failure must not render as the empty state -- "No environment variables are
    // configured." is indistinguishable from a real failure with no signal (audit H9).
    if (DOM.countEnvVars) DOM.countEnvVars.textContent = "0";
    setHtml(DOM.sidebarEnvList,
      '<div class="env-empty">Could not read environment variables from the engine.</div>');
    showToast(`Could not read environment variables: ${(err && err.message) || err}`, "error");
  }
}

export function initEnvironmentPanel() {
  if (DOM.sidebarEnvList) {
    DOM.sidebarEnvList.addEventListener("click", handleEnvListClick);
  }
  refreshEnvironmentVariables();
}
