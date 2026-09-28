// ui/js/state.js — Global application state (`state`) and DOM cache (`DOM`).
/**
 * DeepAgents Studio Frontend Application Logic
 * Integrates JSON-Anchored Plan Tracker (Dual-Sync Hydration Engine),
 * Action Control Panel (Intent-Driven Gatekeeper Architecture),
 * Action Parameter Modal with Custom Instructions, and 50/50 Split Multi-Agent Orchestration.
 */

// Global State
const state = {
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
const DOM = {};
