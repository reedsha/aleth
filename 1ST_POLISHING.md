# 1st Polishing Pass — Implementation Plan

**Status:** planned, not implemented. No code has been changed for this plan yet.
**Scope:** merge four UX/UI recommendations into the existing frontend without breaking
functionality or the pinned contracts.
**Successor document:** `2ND_POLISHING.md` (second, elevated pass).

> This file and `2ND_POLISHING.md` are planning documents, not roadmaps. They are listed in
> `NON_PLAN_MD_FILES` (`tools/workspace.py`) so the plan switcher in the app does not offer
> them, and so selecting one cannot compile a non-plan document into `plan.json`.

---

## The four recommendations covered here

1. Kill the action drawer for a command palette.
2. Introduce a "block"/"card" view for the plan.
3. Consolidate navigation into the sidebars.
4. Soften the contrast and fix typography.

---

## Impact Assessment

### Three findings that change the plan materially

**1. Recommendation #2 is already ~70% built.** The centre stage is *not* a text editor.
`#emptyStateContainer` is the `.plan-workbench`; its primary view is `#workbenchTreeView`, a
card/tree renderer (`ui/js/plan-tree.js`) with per-card status icons, domain pills,
deliverable chips, accordions, and inline **Execute** / **Rollback** buttons, plus a
`🌳 Tree | 📄 Raw MD` toggle (`ui/js/workbench.js`). So #2 is *promotion and enrichment*,
not construction. (`emptyStateContainer` is a legacy misnomer — it has not been an empty
state since the workbench landed.)

**2. Recommendation #1 collides with pinned contracts.** The dock has **six** buttons, not
five (Fix Bug, Next Step, Update Plan, Analyze, Recommend, Custom), and
`tests/ui_startup_contract.js` asserts `class="action-btn` appears **exactly 6 times** in the
markup. Removing them also touches the head watchdog's `required` list (`openActionDrawer`,
`closeActionDrawer`, `setDockDrawerOpen`, `isDockDrawerOpen`), `setActionButtonsDisabled()` in
`ui/js/actions.js`, and `setDockDrawerOpen()` in `ui/js/dock.js` — both of which query
`.action-btn`.

**3. Recommendation #4 is half-done.** The scrollbar theme in `base.css` is *already*
`#121212` track / `#2a2d32` thumb / `#3e4249` hover — the exact palette being requested. The
body surfaces (`#1e1e1e` / `#252526` / `#2d2d30`) and the font sizes are what need changing.

### Files affected

| Area | Files | Nature |
|---|---|---|
| #1 Palette | **new** `ui/js/command-palette.js`, `ui/css/command-palette.css`; `ui/index.html` (markup + watchdog), `ui/js/dom.js`, `ui/js/wire.js`, `ui/js/actions.js`, `ui/js/dock.js`, `ui/css/dock.css`, `ui/css/actions.css` | structural, **highest risk** |
| #2 Blocks | `ui/js/plan-tree.js`, `ui/js/workbench.js`, `ui/css/plan-tree.css` | additive, low risk |
| #3 Nav | `ui/index.html` (sidebar + modal relocation), `ui/css/sidebar.css`, `ui/css/audit-modal.css`, `ui/js/sidebar.js`, `ui/js/wire.js`, `ui/js/plan-modals.js` | structural, medium risk |
| #4 Theme | `ui/css/base.css`, plus size overrides in `stage.css`, `workbench.css`, `plan-tree.css`, `console.css` | token-level, **widest blast radius** |
| Contracts | `tools/build_ui_bundle.py`, `tests/ui_startup_contract.js` | must be edited in lockstep |

### State/style side-effects to watch

- **Four lists must agree** when a UI file is added: `JS_MODULES`/`CSS_MODULES` in
  `tools/build_ui_bundle.py` **and** `MODULES`/`STYLESHEETS` in
  `tests/ui_startup_contract.js`. Forgetting the harness copy produces
  `initCommandPalette is not defined` at startup. New JS appends **before** `wire.js`; new
  CSS appends **last**.
- **`ui/index.html` inlines everything.** Only two generated regions; never add
  `<link>`/`<script src>`; always re-run `tools/build_ui_bundle.py`. Re-run the div-balance
  gate after any markup edit.
- **`setActionButtonsDisabled` becomes a silent no-op** once the six `.action-btn` elements
  are gone — a second action could then be launched mid-run. Must be retargeted.
- **Dock geometry shifts.** `ui/js/dock.js` computes `MIN_HEIGHT = 160` from "the action
  toolbar is ~146px tall"; removing the grid shrinks the dock head, so the floor and
  `.bottom-dock.drawer-open { min-height: 424px }` both need retuning.
- **The id inventory is a hard gate**: every `getElementById` in `ui/js/dom.js` must exist
  **exactly once** in the markup. Deleting a modal would fail the harness — so #3 must
  *relocate* markup, never delete it.
- **#4 is a token change that cascades.** Most components tint themselves with white overlays
  (`rgba(255,255,255,.05)`), so a darker base makes those tints more visible; expect to
  re-check borders and glass cards.
- **`#emptyStateContainer` is load-bearing** (`dom.js`, `workbench.js`, `actions.js`,
  `plan-tree.js`). Do **not** rename it in this pass.

### Conflicts with settled decisions (resolve before starting)

`PLAN.md`'s `## 🌍 Global State Summary` records as settled: "the right panel is a fixed 60px
rail and is never drag-resizable" and "the action-drawer entry point is renamed consistently
across every reference rather than left as an alias". Recommendation #1 **reverses** the
Connected Tab Action Drawer milestone (§4, marked done), and #3 changes what the right rail
does. These are legitimate reversals, but they are reversals — update the summary bullet in
the same pass so the plan does not contradict the code.

---

## Step-by-Step Action Plan

### Phase 0 — Guardrails (do first, nothing visible)

1. `grep -rn "\.action-btn\|setActionButtonsDisabled\|openActionDrawer\|setDockDrawerOpen" ui/js/`
   and record every call site.
2. Add the new files to **both** module lists and the watchdog `required` list while the
   module is still a no-op, then run all four gates. This proves the plumbing before any
   markup moves.
3. Record the reversal of the two settled decisions above.

### Phase 1 — Command palette, remove the dock strip (#1)

4. Add `ui/js/command-palette.js` (router only; delegates to `openActionDrawer`).
5. Add `ui/css/command-palette.css`; register both in the four lists.
6. Add palette markup + a `⌘` top-bar trigger; cache `#commandPaletteOverlay`,
   `#commandPaletteInput`, `#commandPaletteList`, `#btnCommandPalette` in `ui/js/dom.js`.
7. Wire in `ui/js/wire.js`: `runStartupStep("Command palette", initCommandPalette)`, plus the
   trigger button.
8. **Delete** the `.action-buttons-grid` block (six buttons) from `#actionDockCard`; keep
   `#actionDrawerPanel` (the parameter form, now opened by the palette) and
   `.action-dock-bottom-row` (`#readonlyPlanPill`, `#txtDockProgress`, `#btnDockStop`).
9. Retarget `setActionButtonsDisabled` → `[data-command], #btnCommandPalette`.
10. Simplify `setDockDrawerOpen()` in `ui/js/dock.js` (no `.action-btn` left to mark).
11. Retune the dock floor: `MIN_HEIGHT` in `dock.js` and `.bottom-dock.drawer-open`.
12. Update the harness: the action-btn assertion, plus a new assertion that the palette
    exposes the six intents.

### Phase 2 — Promote the block view (#2)

13. Add an inline **Edit** action to each card (`btn-inline-edit`) alongside Execute/Rollback.
14. `editTaskInWorkbench(step)` — switch the workbench to raw+editing and scroll to the
    task's source line.
15. Render section headers as cards (not bare headings) and give the tree a keyboard path
    (↑/↓ + Enter → Execute).
16. Add `btn-inline-edit` to the harness `contractClasses` list.

### Phase 3 — Consolidate navigation into the sidebars (#3)

17. **3a** Convert the left sidebar's four stacked blocks into tabs:
    `Workspace | Agents | Files | Env`, keeping the inner ids (`#sidebarMainAgentsList`,
    `#sidebarCoderAgentsList`, `#sidebarTree`, `#sidebarEnvList`) so `agents.js`,
    `sidebar.js` and `env.js` need no change.
18. **3b** Relocate the audit modal's markup into a right-rail slide-out panel: same ids,
    container class `modal-overlay` → `side-panel`. `openAuditModal`/`closeAuditModal` only
    toggle `display`, so they keep working unchanged.
19. **3c** Point `#btnNavFiles` at the sidebar **Files** tab; keep `#filesModalOverlay` in the
    DOM (id inventory) but stop opening it from there, with an "expand to modal" affordance
    inside the tab so search stays reachable.
20. **3d** Add a **Plans** tab; point the rail's `⇆` at it instead of the switch modal. Leave
    the create-plan modal as-is (it needs a form).

### Phase 4 — Contrast and typography (#4)

21. Retune the `:root` tokens in `ui/css/base.css` (`#121212` / `#1a1c23` / `#2a2d35`).
22. Bump the plan/code font sizes by +1.5px and set a base `body` size.
23. Semi-bold the section headers.
24. (Optional) Tighten `--radius-*` from 8/14/20/26 → 2/4/6 to match the IDE brief.

---

## Code Diffs / Snippets

### Phase 0 — the four lists

`tools/build_ui_bundle.py` and `tests/ui_startup_contract.js`:

```diff
 JS_MODULES = [
     ..., "preview.js", "sidebar.js", "env.js", "settings.js",
+    "command-palette.js",
     "wire.js",                     # wire.js must stay last
 ]

 CSS_MODULES = [
     ..., "sidebar-panels.css", "settings.css",
+    "command-palette.css",         # new stylesheets append last
 ]
```

`ui/index.html` head watchdog:

```diff
 var required = [
   "initDOMElements", "initEventListeners", "initAutoScrollListeners", "initDockResize",
   "initWorkbench", "initCodeSurfaces", "initTerminalConsole",
   "initPreview", "initSidebars", "initEnvironmentPanel",
   "renderPlanTree", "openActionDrawer", "closeActionDrawer",
   "setDockDrawerOpen", "isDockDrawerOpen", "repaintCodeSurfaces",
   "openRollbackModal", "handleConfirmRollback", "showToast", "handleAgentEvent",
   "handleRetagUiClick", "openSettingsModal", "closeSettingsModal",
+  "initCommandPalette", "openCommandPalette", "closeCommandPalette"
 ];
```

### Phase 1 — new module (complete file)

`ui/js/command-palette.js`:

```js
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
```

### Phase 1 — markup

Add near the other modals in `ui/index.html`:

```html
  <!-- COMMAND PALETTE (Ctrl/Cmd + K): the launcher that replaced the dock button strip -->
  <div class="palette-overlay" id="commandPaletteOverlay" style="display: none;">
    <div class="palette-card" role="dialog" aria-label="Command palette">
      <div class="palette-search">
        <span class="palette-search-icon">⌘</span>
        <input type="text" id="commandPaletteInput" class="palette-input"
               placeholder="Run a command…" autocomplete="off" spellcheck="false">
        <span class="palette-esc">esc</span>
      </div>
      <div class="palette-list" id="commandPaletteList"></div>
    </div>
  </div>
```

Top-bar trigger (next to Preview / Files):

```html
          <button class="btn-top-action" id="btnCommandPalette" type="button" title="Command palette (Ctrl/Cmd + K)">
            <span>⌘</span><span>Commands</span>
          </button>
```

Remove the strip — in `#actionDockCard`, delete the whole `.action-buttons-grid` block (all
six `.action-btn` buttons) and leave:

```html
          <div class="action-dock-card" id="actionDockCard">
            <!-- The six intent buttons are gone: the command palette (Ctrl/Cmd+K) and the
                 task cards' own inline buttons are the entry points now. What remains here
                 is the parameter form of the chosen command, plus the read-only status row. -->
            <div class="action-drawer-panel" id="actionDrawerPanel"> … unchanged … </div>
            <div class="action-dock-bottom-row"> … unchanged … </div>
          </div>
```

### Phase 1 — retarget the run lock and the drawer mark

`ui/js/actions.js`:

```diff
-function setActionButtonsDisabled(disabled) {
-  const btns = document.querySelectorAll(".action-btn");
-  btns.forEach(btn => {
-    btn.disabled = disabled;
-    btn.style.opacity = disabled ? "0.4" : "1";
-    btn.style.pointerEvents = disabled ? "none" : "auto";
-  });
-}
+function setActionButtonsDisabled(disabled) {
+  // The six dock buttons are gone, so querying .action-btn would silently no-op and a
+  // second action could be launched mid-run. The palette items are the same controls now.
+  const controls = document.querySelectorAll(".palette-item, #btnCommandPalette");
+  controls.forEach(el => {
+    el.disabled = disabled;
+    el.style.opacity = disabled ? "0.4" : "1";
+    el.style.pointerEvents = disabled ? "none" : "auto";
+  });
+}
```

`ui/js/dock.js`:

```diff
 function setDockDrawerOpen(open, actionType) {
   const dock = DOM.bottomDock;
   if (!dock) return;
-
-  const marked = document.querySelectorAll(".action-btn.active");
-  for (let i = 0; i < marked.length; i++) marked[i].classList.remove("active");
 
   if (!open) {
     dock.classList.remove("drawer-open");
     return;
   }
-
-  const btn = actionType ? document.querySelector('.action-btn[data-action="' + actionType + '"]') : null;
-  if (btn) btn.classList.add("active");
+  // There is no button to mark any more: the palette closes on selection, so the drawer
+  // opening is itself the acknowledgement that a command was chosen.
   dock.classList.add("drawer-open");
 }
```

`ui/css/dock.css` and `ui/js/dock.js` (retune the floor):

```diff
-.bottom-dock.drawer-open { min-height: 424px; }
+.bottom-dock.drawer-open { min-height: 320px; }   /* was 424: the ~90px button strip is gone */
```

```diff
-  const MIN_HEIGHT = 160;
+  const MIN_HEIGHT = 120;   /* the dock head is the status row now, not a 146px button strip */
```

### Phase 1 — harness updates

```diff
-  const actions = (markup.match(/class="action-btn[ "]/g) || []).length;
-  check("the six action buttons are still in the markup", actions === 6, `${actions} found`);
+  // The six intent buttons moved into the command palette, so the dock strip is gone and
+  // the palette must expose the same six commands. Pin the new shape, not the old one.
+  const dockButtons = (markup.match(/class="action-btn[ "]/g) || []).length;
+  check("the dock button strip is gone", dockButtons === 0, `${dockButtons} found`);
+
+  const palette = fs.readFileSync(path.join("ui", "js", "command-palette.js"), "utf8");
+  const commands = (palette.match(/\{ id: "/g) || []).length;
+  check("the command palette exposes six commands", commands === 6, `${commands} found`);
```

### Phase 2 — inline Edit on each card

`ui/js/plan-tree.js` — extend the click exclusion and add the handler:

```diff
       itemEl.addEventListener("click", (e) => {
         if (e.target.closest(".btn-inline-execute") ||
             e.target.closest(".btn-inline-rollback") ||
+            e.target.closest(".btn-inline-edit") ||
             e.target.closest(".btn-inline-stop")) return;
         itemEl.classList.toggle("expanded");
       });
```

```js
      // Inline edit: open the plan source in edit mode, scrolled to this task's line. Editing
      // always means the raw view (ui/js/workbench.js), so this is a jump, not a second editor.
      const editBtn = itemEl.querySelector(".btn-inline-edit");
      if (editBtn) {
        editBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          editTaskInWorkbench(step);
        });
      }
```

`ui/js/workbench.js` — new helper:

```js
// Scrolls the raw source to the task the user asked to edit. The task's title is the anchor
// because that is what the markdown carries; a miss simply opens the top of the file, which
// is still an edit session rather than a dead click.
function editTaskInWorkbench(step) {
  if (!step || !DOM.planEditorInput) return;
  handleWorkbenchEdit();
  const needle = String(step.title || "").trim();
  if (!needle) return;
  const at = DOM.planEditorInput.value.indexOf(needle);
  if (at < 0) return;
  const line = DOM.planEditorInput.value.slice(0, at).split("\n").length;
  DOM.planEditorInput.scrollTop = Math.max(0, (line - 4) * 18);
  DOM.planEditorInput.setSelectionRange(at, at + needle.length);
}
```

```diff
 const contractClasses = [
   "action-btn", "plan-tree-item", "plan-tree-section", "btn-inline-execute",
-  "btn-inline-rollback", "agent-list-item", "active-agent",
+  "btn-inline-rollback", "btn-inline-edit", "agent-list-item", "active-agent",
```

### Phase 3 — sidebar tabs (ids preserved)

```html
      <div class="sidebar-tabs" role="tablist" aria-label="Sidebar sections">
        <button class="sidebar-tab active" id="tabSidebarWorkspace" type="button" role="tab" aria-selected="true">Workspace</button>
        <button class="sidebar-tab" id="tabSidebarAgents" type="button" role="tab" aria-selected="false">Agents</button>
        <button class="sidebar-tab" id="tabSidebarFiles" type="button" role="tab" aria-selected="false">Files</button>
        <button class="sidebar-tab" id="tabSidebarEnv" type="button" role="tab" aria-selected="false">Env</button>
      </div>
```

Each existing `agent-section-block` gains `data-tab="…"`; the inner ids are untouched, so
`agents.js`, `sidebar.js` and `env.js` keep rendering into the same nodes.

```js
// One tab at a time. The panels keep their ids and their renderers, so this is presentation
// only -- switching tabs never re-renders and never loses a panel's scroll position.
function setSidebarTab(tab) {
  state.sidebarTab = tab;
  ["workspace", "agents", "files", "env"].forEach((name) => {
    const panel = document.querySelector(`.agent-section-block[data-tab="${name}"]`);
    if (panel) panel.hidden = name !== tab;
    const button = document.getElementById(`tabSidebar${name[0].toUpperCase()}${name.slice(1)}`);
    if (button) {
      const on = name === tab;
      button.classList.toggle("active", on);
      button.setAttribute("aria-selected", String(on));
    }
  });
}
```

### Phase 3 — audit modal → right-rail panel

```diff
-  <div class="modal-overlay" id="auditModalOverlay" style="display: none;">
+  <!-- A rail panel, not a centre-screen modal: the report is cross-referenced against the
+       code the user is reading, so it slides out from the right instead of covering it.
+       Same id, same ids inside, and the open/close helpers only toggle `display`. -->
+  <div class="side-panel" id="auditModalOverlay" style="display: none;">
```

```css
/* ui/css/audit-modal.css */
.side-panel {
  position: absolute; top: 0; right: 0; bottom: 0;
  width: min(460px, 42vw);
  background: var(--bg-secondary);
  border-left: 1px solid var(--border-subtle);
  box-shadow: -18px 0 40px rgba(0, 0, 0, 0.55);
  display: flex; flex-direction: column;
  z-index: 60;
  animation: panelSlideIn 0.24s cubic-bezier(0.16, 1, 0.3, 1);
}
@keyframes panelSlideIn { from { transform: translateX(24px); opacity: 0; } to { transform: none; opacity: 1; } }
```

### Phase 4 — palette and type

`ui/css/base.css`:

```diff
 :root {
-  --bg-primary: #1e1e1e;
-  --bg-secondary: #252526;
-  --bg-elevated: #2d2d30;
-  --bg-sidebar: rgba(37, 37, 38, 0.94);
-  --bg-glass-card: rgba(45, 45, 48, 0.78);
+  --bg-primary: #121212;
+  --bg-secondary: #1a1c23;
+  --bg-elevated: #2a2d35;
+  --bg-sidebar: rgba(26, 28, 35, 0.94);
+  --bg-glass-card: rgba(42, 45, 53, 0.78);
   --bg-glass-hover: rgba(255, 255, 255, 0.07);
 
-  --border-subtle: #333333;
-  --border-glass: #3c3c3c;
+  --border-subtle: #2a2d35;
+  --border-glass: #3a3d45;
```

```diff
 body {
   font-family: var(--font-ui);
+  font-size: 14px;              /* the plan and code panes inherit this floor */
   background-color: var(--bg-primary);
```

Then +1.5px on the reading surfaces (they are the smallest text in the app):

```diff
/* ui/css/workbench.css */
-.wb-src { font-size: 11.5px; }
+.wb-src { font-size: 13px; }
```
```diff
/* ui/css/plan-tree.css */
-.step-detail-bullet { font-size: 10px; }
+.step-detail-bullet { font-size: 11.5px; }
```
```diff
/* ui/css/console.css */
-.console-stream { font-size: 11.5px; }
+.console-stream { font-size: 12.5px; }
```

Semi-bold section headers:

```diff
/* ui/css/sidebar.css */
-.section-title { letter-spacing: 0.8px; color: var(--text-dim); }
+.section-title { letter-spacing: 0.8px; color: var(--text-muted); font-weight: 600; }
```

---

## Verification Steps

**After every phase (all four gates, from the repo root):**

```powershell
node tests/ui_startup_contract.js                                  # contract: expect 0 FAIL
.\venv\Scripts\python.exe tools\build_ui_bundle.py --check         # 19 -> 21 modules, 17 -> 18 stylesheets
.\venv\Scripts\python.exe -m unittest discover -s tests -t .       # 332 tests, OK
.\venv\Scripts\python.exe -c "import pathlib; t=pathlib.Path('ui/index.html').read_text(encoding='utf-8'); r=t[t.index('END STYLE BUNDLE'):t.index('BEGIN UI BUNDLE')]; print('<div', r.count('<div'), '</div>', r.count('</div>'))"
```

Run `tools/build_ui_bundle.py` (no `--check`) after **any** `ui/js` or `ui/css` edit, before
the gates.

**Phase-specific checks:**

- **Phase 1** — the harness must show the new `dock button strip is gone (0 found)` and
  `command palette exposes six commands (6 found)`. Then confirm the drawer still opens:
  `grep -c "actionDrawerPanel" ui/index.html` should be ≥ 2 (markup + dom.js).
- **Phase 2** — `grep -c "btn-inline-edit" ui/js/plan-tree.js ui/css/plan-tree.css` matches the
  number of places you added it; the contract's class list includes it.
- **Phase 3** — the id-inventory check is the real test: it fails loudly if relocating the
  audit modal lost an id. `grep -c 'id="auditStatusPill"' ui/index.html` must still be exactly 1.
- **Phase 4** — no automated check; this is a visual pass.

**Launch checklist (eyeball this — the harness cannot see the window).** Launch 3+ times,
since the historical failure mode here is intermittent ("renders but ignores input"):

1. Window opens; console bar shows **⤢ / ⧉ / Clear**; the dock no longer has the six-button strip.
2. **Ctrl+K** opens the palette; type `fix` → only "Fix Bug"; Enter → the parameter form appears
   in the dock; Cancel closes it.
3. Click a task card's inline **Execute** → the same form opens onto that task.
4. Click a card's inline **✎ Edit** → the workbench flips to Raw MD in edit mode, scrolled near
   that task; Save/Discard work.
5. Left sidebar tabs switch Workspace/Agents/Files/Env; the file tree and env list still
   populate; nothing re-renders blank.
6. Right rail `🛡️` → the sync report slides in from the right (not a centred modal) and its
   close/sync buttons respond.
7. Theme: surfaces are `#121212`/`#1a1c23`; no pure black; plan/code text is visibly larger;
   section headers are bolder.
8. Start a run, then confirm the palette trigger and its items are **disabled** while executing,
   and re-enabled on completion.

**Regression guard to add:** extend the harness with one check that `setActionButtonsDisabled`
still reaches something — e.g. assert `.palette-item` appears in `ui/js/actions.js`. Without it,
the silent-no-op regression above would pass every gate while letting a second run start mid-run.

---

## Recommended sequencing

**Phase 0 → 4 → 2 → 3 → 1.** The theme and the card enrichment are the visible wins with the
least structural risk, and Phase 1 — which breaks the pinned action-btn contract — lands last,
when everything else is already verified.

**Lower-risk alternative for #1:** add the palette *alongside* the existing strip (zero contract
churn) and remove the strip in a later pass. This keeps the "without breaking functionality"
guarantee trivially intact at the cost of one extra pass.
