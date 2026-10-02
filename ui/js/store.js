// ui/js/store.js — The store: the application's shared state, its DOM cache, and the
// only operations that may change a field more than one module writes.
//
// Ownership model, enforced by the module system rather than by convention:
//
// * `state` and `DOM` live here and are imported by the modules that read them. A module
//   can only reach what it imports, so "who touches this field?" is answered by the import
//   graph instead of by searching the directory.
// * `DOM` is *populated* by exactly one module (`dom.js`'s `initDOMElements`) and read by
//   everyone else. It is a lookup table, not shared mutable state.
// * A `state` field written from more than one module is written **only** through a named
//   operation below, so each change to the genuinely shared surface has one definition and
//   one place to grow an invariant. A field written from a single module is written
//   directly: wrapping it would add indirection and buy nothing.
//
// That rule is checkable, not aspirational: `_analyze_state.py`-style analysis over
// `ui/js` reports any field written from more than one module without a mutator.

// Global State
export const state = {
  mainAgents: [],
  coderAgents: [],
  workspaceDir: "./my_project_workspace",
  activePlan: "PLAN.md",
  planTree: [],
  planJson: null,
  availablePlans: [],
  activeAgentForEditor: null,
  isExecuting: false,
  autoScrollArchitect: true,
  autoScrollCoder: true,
  architectCardOpen: false,
  coderCardOpen: false,
  pendingPrompt: "",
  selectedAction: null,
  targetTaskId: null,
  targetTaskTitle: null,
  // The execution id the engine accepted the current run under. The shadow that run writes into
  // is keyed to it (Phase 21), so the UI must hold it to name the run again -- an artifact
  // approval, or the merge review. Written only by `actions.js` when a launch is confirmed.
  activeIntentId: null,
  // Whether the open action drawer is asking for the plan-wide run (Phase 18) rather than one
  // node. Set by `openActionDrawer` and read by the confirm handler.
  runWholePlan: false,
  // Ids of the agents currently generating work. Held here, and not only flipped on the DOM,
  // because renderSidebarAgents() rebuilds the lists wholesale (for example on agents_updated)
  // and would drop a class set directly on a node; the renderer re-applies this set on rebuild.
  activeAgentIds: new Set(),
  // The plan tree's accordion state. renderPlanTree() rebuilds the tree's markup wholesale on
  // every plan load, save, sync and rollback, so an expand/collapse set directly on a card is
  // lost the moment anything refreshes the plan. Phase cards default to open and step cards to
  // closed, so each set records only the exceptions: the phases the user folded away, and the
  // steps the user opened.
  collapsedPlanPhases: new Set(),
  expandedPlanTasks: new Set(),
  // Whether the live preview pane is on screen.
  previewOpen: false,
  // Whether the dock's console is on screen, and whether it was put up by something that
  // demands attention (an agent error, a refused instruction) rather than by the run cycle
  // or the user. The transcript is a run-scoped pane: it opens for a run and folds away at
  // idle, unless it is pinned, in which case it outlives the run so the reason is not lost.
  consoleVisible: false,
  consolePinned: false,
  // Whether each sidebar is collapsed to its icon rail. Restored from localStorage at
  // startup, so the workspace layout a developer chose survives a relaunch.
  sidebarCollapsed: false,
  planSidebarCollapsed: false,
  // Which left-sidebar tab is showing ("agents" | "files" | "env"). Presentation only:
  // every panel keeps its id and its renderer, so switching never re-renders.
  sidebarTab: "agents",
  // The polymorphic result view. `resultViewOpen` is whether the overlay is on screen and
  // `resultViewKind` the action whose result it holds -- kept across a dismissal so the
  // workbench's Result button can bring it back. The next three are the payloads the
  // terminal workflow_complete event does not carry: the architect's summary (analyze,
  // recommend, fix_bug's root cause), whether that summary was a refusal (a refusal is a
  // request for input, not a result), and the roadmap as it stood before plan_updated
  // overwrote it. All are cleared when a new run starts.
  resultViewOpen: false,
  resultViewKind: null,
  lastSummary: null,
  lastRunRefused: false,
  planTreeBeforeUpdate: null
};

// DOM Cache
export const DOM = {};

// ---------------------------------------------------------------------------
// The shared mutable surface
// ---------------------------------------------------------------------------
//
// Seven fields below are written from more than one module; they are the whole of the
// cross-module mutation surface (the other 232 written fields each have a single writer).
// Reads stay direct -- `state.availablePlans`, `state.lastSummary` -- because a read has no
// invariant to protect; these operations exist so that every *write* to a shared field is
// a named act.

/** The plans the switcher and the Plans tab offer, replaced only on a real answer.
 *
 * Written by `plan-tree.js` (from the plan payload), `plan-modals.js` and `sidebar.js`
 * (from `get_plan_files`). A failed or malformed bridge call must leave the list the UI
 * is already showing alone, so anything that is not an array is ignored rather than
 * stored as empty.
 */
export function setAvailablePlans(plans) {
  if (Array.isArray(plans)) state.availablePlans = plans;
}

/** Whether the console was opened by something demanding attention.
 *
 * Written by `agent-events.js` (a run clears the previous run's pin) and `console.js`
 * (an error, a refusal or a failed verification pins it). A pinned transcript outlives
 * the run that produced it, so the reason it is up is not lost when the run folds away.
 */
export function setConsolePinned(pinned) {
  state.consolePinned = Boolean(pinned);
}

/** Which agent card is expanded in the centre stage.
 *
 * Written by `agent-events.js` (a spawn opens one and closes the other) and `wire.js`
 * (the card's own close button). `architect` and `coder` are the only two card kinds.
 */
export function setAgentCardOpen(which, open) {
  if (which === "architect") state.architectCardOpen = Boolean(open);
  else if (which === "coder") state.coderCardOpen = Boolean(open);
}

/** The architect's summary for the result view to render.
 *
 * The terminal `workflow_complete` event carries no summary, so the event handler parks
 * it here and the result renderers read it. Written by `agent-events.js`, cleared by
 * `result-view.js` when a new run starts.
 */
export function setRunSummary(summary) {
  state.lastSummary = summary || null;
}

/** Marks the parked summary as a refusal rather than a completion.
 *
 * A refusal (the fix_bug sufficiency gate, the plan Normalization Gate) is a request for
 * input, so the result view stays closed and the console carries the reason. Separate
 * from `setRunSummary` on purpose: a later run that completes normally must not be able
 * to inherit an earlier run's refusal, so only `clearRunSummary` turns it off.
 */
export function markRunRefused() {
  state.lastRunRefused = true;
}

export function clearRunSummary() {
  state.lastSummary = null;
  state.lastRunRefused = false;
}

/** Keeps the roadmap as it was before `plan_updated` overwrote it.
 *
 * The result view's roadmap diff is `planTreeBeforeUpdate` vs `planTree`, and the event
 * handler has to capture it *before* applying the new tree -- it cannot be reconstructed
 * afterwards. Captured by `agent-events.js`, cleared by `result-view.js`.
 */
export function capturePlanTreeBeforeUpdate() {
  state.planTreeBeforeUpdate = (state.planTree || []).slice();
}

export function clearPlanTreeBeforeUpdate() {
  state.planTreeBeforeUpdate = null;
}

// ---------------------------------------------------------------------------
// Diagnostics
// ---------------------------------------------------------------------------

/** Cache entries that did not resolve to an element, by name.
 *
 * `DOM` is filled by `initDOMElements` from the document, and every renderer and listener
 * reads it, so an entry that is null is the exact signature of the historical "window
 * renders but ignores input" fault: the markup lost an id, or a control was renamed on
 * one side only. Named rather than counted, because the name is the fix.
 *
 * Called by the wiring after it caches the elements (which reports them as a startup
 * failure) and exposed to the outside world through `window.Aleth.diagnostics` in
 * `main.js`, which is how the Playwright suite asserts it without reaching into module
 * scope.
 */
export function unresolvedElements() {
  return Object.entries(DOM)
    .filter(([, element]) => !element)
    .map(([name]) => name);
}
