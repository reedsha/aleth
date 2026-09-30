// ui/js/settings.js — The provider/model settings modal.
//
// Two rules shape this panel, and both are about not losing a working setup:
//
//  * The API key is write-only. It is never sent *to* the window, so the field starts
//    empty and its placeholder only says whether one is configured. Leaving it empty
//    keeps the current key -- a blank box must never delete a working credential.
//  * Emptying a *route* or the base URL does clear it, so the built-in default takes over.
//    A route with no value is a legitimate way to reset it, and the default is shown next
//    to the field so the result is visible before saving.

import { escapeHtml } from "./dom.js";
import { refreshEnvironmentVariables } from "./env.js";
import { setHtml } from "./safe-dom.js";
import { DOM } from "./store.js";

function settingsHasBridge() {
  return !!(window.pywebview && window.pywebview.api &&
    typeof window.pywebview.api.get_settings === "function");
}

function settingsMask(length) {
  return "\u2022".repeat(8) + (length ? ` set (${length} characters) \u2014 type to replace` : "");
}

function settingsFieldMarkup(field) {
  const name = escapeHtml((field && field.name) || "");
  const inputId = "setting-" + name;
  const isSecret = !!(field && field.secret);
  const isSet = !!(field && field.set);

  const value = isSecret ? "" : escapeHtml((field && field.value) || "");
  const placeholder = isSecret
    ? (isSet ? settingsMask(Number(field.length) || 0) : "Not set \u2014 paste a key")
    : escapeHtml((field && field.default) || "Not set");

  // The value actually in force, which for an unset route is its default. Shown for the
  // fields whose value is visible, never for the key.
  const effective = !isSecret && (field && field.effective)
    ? `<span class="settings-help settings-effective">In force: ${escapeHtml(field.effective)}</span>`
    : "";

  return (
    `<label class="settings-field" for="${inputId}">` +
    `<span class="settings-label">${escapeHtml((field && field.label) || name)}</span>` +
    `<input class="modal-text-input settings-input" id="${inputId}" ` +
    `data-setting-name="${name}" spellcheck="false" autocomplete="off" ` +
    `type="${isSecret ? "password" : "text"}" value="${value}" placeholder="${placeholder}">` +
    `<span class="settings-help">${escapeHtml((field && field.help) || "")}</span>` +
    effective +
    "</label>"
  );
}

function renderSettings(payload) {
  if (!DOM.settingsFields) return;
  const fields = (payload && payload.fields) || [];
  setHtml(DOM.settingsFields, fields.length
    ? fields.map(settingsFieldMarkup).join("")
    : '<div class="settings-help">No settings are available.</div>');
}

function setSettingsStatus(message, kind) {
  if (!DOM.settingsStatus) return;
  DOM.settingsStatus.textContent = message || "";
  DOM.settingsStatus.className = kind ? "settings-status " + kind : "settings-status";
}

async function openSettingsModal() {
  if (!DOM.settingsModalOverlay) return;
  DOM.settingsModalOverlay.style.display = "flex";
  setSettingsStatus("");

  if (!settingsHasBridge()) {
    renderSettings(null);
    setSettingsStatus("The desktop app is needed to change settings.", "error");
    return;
  }
  try {
    renderSettings(await window.pywebview.api.get_settings());
  } catch (_err) {
    setSettingsStatus("Could not read the current settings.", "error");
  }
}

function closeSettingsModal() {
  if (DOM.settingsModalOverlay) DOM.settingsModalOverlay.style.display = "none";
}

function collectSettingsValues() {
  const values = {};
  if (!DOM.settingsFields || typeof DOM.settingsFields.querySelectorAll !== "function") {
    return values;
  }
  DOM.settingsFields.querySelectorAll("[data-setting-name]").forEach((input) => {
    const name = input.getAttribute("data-setting-name");
    const isSecret = input.type === "password";
    const value = String(input.value == null ? "" : input.value).trim();
    // A blank key field is left out entirely: the backend treats a blank value as "clear",
    // and clearing a secret because a box was left empty would be the worst default here.
    if (isSecret && !value) return;
    values[name] = value;
  });
  return values;
}

async function saveSettingsFromForm() {
  if (!settingsHasBridge()) {
    setSettingsStatus("The desktop app is needed to change settings.", "error");
    return;
  }
  setSettingsStatus("Saving\u2026");
  // Disable while in flight so a double-click cannot issue two saves (audit M10).
  const btn = DOM.btnSaveSettings;
  if (btn) btn.disabled = true;
  try {
    const res = await window.pywebview.api.save_settings(collectSettingsValues());
    renderSettings(res);
    if (res && res.success) {
      setSettingsStatus("Saved. Applies to the next run \u2014 no restart needed.", "success");
      if (typeof refreshEnvironmentVariables === "function") refreshEnvironmentVariables();
    } else {
      setSettingsStatus((res && res.error) || "The settings could not be saved.", "error");
    }
  } catch (_err) {
    setSettingsStatus("The settings could not be saved.", "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

export function initSettingsPanel() {
  if (DOM.btnOpenSettings) DOM.btnOpenSettings.addEventListener("click", openSettingsModal);
  if (DOM.btnCloseSettingsModal) DOM.btnCloseSettingsModal.addEventListener("click", closeSettingsModal);
  if (DOM.btnCancelSettings) DOM.btnCancelSettings.addEventListener("click", closeSettingsModal);
  if (DOM.btnSaveSettings) DOM.btnSaveSettings.addEventListener("click", saveSettingsFromForm);
  if (DOM.settingsModalOverlay) {
    DOM.settingsModalOverlay.addEventListener("click", (event) => {
      if (event.target === DOM.settingsModalOverlay) closeSettingsModal();
    });
  }
}
