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
  // The snapshot key the tracked-edits pane should read once the current run lands.
  lastRunDiffKey: null
};

// DOM Cache
const DOM = {};
