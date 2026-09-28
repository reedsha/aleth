// ui/js/command-palette.js — keyboard-first action launcher.
//
// The six intents were six permanent buttons in the bottom dock, spending ~90px of vertical
// space on a strip a keyboard user never needs. They are a palette now: Ctrl/Cmd+K opens it,
// typing filters, Enter runs. It is a *router*, not a second implementation -- choosing a
// command calls the same openActionDrawer() the task cards' inline Execute buttons call, so
// the parameter form, its validation and the confirm path are all unchanged.

const COMMANDS = [
  { id: "next_step",   icon: "⚡", label: "Execute Next Step",  hint: "Implement the next pending milestone" },
  { id: "fix_bug",     icon: "🐛", label: "Fix Bug",            hint: "Diagnose and patch a reported defect" },
  { id: "update_plan", icon: "✏️", label: "Update / Edit Plan", hint: "Refine roadmap milestones" },
  { id: "analyze",     icon: "🔍", label: "Analyze Codebase",   hint: "Structural report on the workspace" },
  { id: "recommend",   icon: "💡", label: "Get Recommendation", hint: "Strategic next steps" },
  { id: "custom",      icon: "💬", label: "Custom Directive",   hint: "Free-form instruction for the Architect" },
];

let paletteFiltered = COMMANDS.slice();
let paletteIndex = 0;

function isCommandPaletteOpen() {
  return !!(DOM.commandPaletteOverlay && DOM.commandPaletteOverlay.style.display === "flex");
}

function openCommandPalette() {
  if (!DOM.commandPaletteOverlay || !DOM.commandPaletteInput) return;
  paletteIndex = 0;
  DOM.commandPaletteInput.value = "";
  renderCommandPalette("");
  DOM.commandPaletteOverlay.style.display = "flex";
  if (DOM.commandPaletteInput.focus) DOM.commandPaletteInput.focus();
}

function closeCommandPalette() {
  if (DOM.commandPaletteOverlay) DOM.commandPaletteOverlay.style.display = "none";
}

function renderCommandPalette(query) {
  if (!DOM.commandPaletteList) return;
  const needle = String(query || "").trim().toLowerCase();
  paletteFiltered = needle
    ? COMMANDS.filter((c) => `${c.label} ${c.hint} ${c.id}`.toLowerCase().includes(needle))
    : COMMANDS.slice();
  if (paletteIndex >= paletteFiltered.length) paletteIndex = Math.max(0, paletteFiltered.length - 1);

  DOM.commandPaletteList.innerHTML = paletteFiltered.map((c, i) =>
    `<button type="button" class="palette-item${i === paletteIndex ? " active" : ""}" data-command="${escapeHtml(c.id)}">` +
      `<span class="palette-icon">${c.icon}</span>` +
      `<span class="palette-label">${escapeHtml(c.label)}</span>` +
      `<span class="palette-hint">${escapeHtml(c.hint)}</span>` +
    `</button>`
  ).join("") || `<div class="palette-empty">No matching command</div>`;
}

function runCommandPaletteSelection() {
  const chosen = paletteFiltered[paletteIndex];
  if (!chosen) return;
  // The palette closes first: the drawer it opens lives in the dock, and leaving the overlay
  // up would keep the keyboard captured by a surface that is no longer the subject.
  closeCommandPalette();
  openActionDrawer(chosen.id); // the same entry point the task cards use
}

function handleCommandPaletteKey(event) {
  if (event.key === "ArrowDown") {
    paletteIndex = Math.min(paletteIndex + 1, paletteFiltered.length - 1);
    renderCommandPalette(DOM.commandPaletteInput.value);
    event.preventDefault();
  } else if (event.key === "ArrowUp") {
    paletteIndex = Math.max(paletteIndex - 1, 0);
    renderCommandPalette(DOM.commandPaletteInput.value);
    event.preventDefault();
  } else if (event.key === "Enter") {
    runCommandPaletteSelection();
    event.preventDefault();
  } else if (event.key === "Escape") {
    closeCommandPalette();
    event.preventDefault();
  }
}

// No DOMContentLoaded here on purpose: wire.js owns the only one, and a second would be a
// startup-order hazard (see tests/ui_startup_contract.js).
function initCommandPalette() {
  if (DOM.commandPaletteInput) {
    DOM.commandPaletteInput.addEventListener("input", () => renderCommandPalette(DOM.commandPaletteInput.value));
    DOM.commandPaletteInput.addEventListener("keydown", handleCommandPaletteKey);
  }
  if (DOM.commandPaletteList) {
    DOM.commandPaletteList.addEventListener("click", (e) => {
      const item = e.target.closest(".palette-item");
      if (!item) return;
      const at = paletteFiltered.findIndex((c) => c.id === item.dataset.command);
      if (at >= 0) paletteIndex = at;
      runCommandPaletteSelection();
    });
  }
  if (DOM.commandPaletteOverlay) {
    DOM.commandPaletteOverlay.addEventListener("click", (e) => {
      if (e.target === DOM.commandPaletteOverlay) closeCommandPalette();
    });
  }
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && (event.key === "k" || event.key === "K")) {
      event.preventDefault();
      if (isCommandPaletteOpen()) closeCommandPalette(); else openCommandPalette();
    }
  });
}
