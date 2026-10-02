// ui/js/dag.js — The event-driven DAG renderer (Phase 8).
//
// The roadmap is a directed acyclic graph and is drawn as one: a node per milestone, laid
// out in topological order, with an SVG edge for every real dependency. The edges are the
// store's, not the renderer's -- a node's `dependencies` comes from the `task_dependencies`
// table (projected onto the plan dictionary by storage.db), so the graph on screen is the
// graph the database holds. This renderer does not infer an edge from list order: drawing a
// chronological chain as if it were topology would be a lie, and the note it shows when a
// plan declares no dependencies says so instead.
//
// Every node is coloured strictly by state and carries the state as text too, so the surface
// never depends on colour alone:
//
//   PENDING     grey
//   PLANNED     amber   -- the Artifact Gate's halted state, and the only one with an
//                          Approve control
//   IN_PROGRESS blue
//   COMPLETED   green
//   FAILED      red
//
// It is event-driven: `artifact_planned`, `task_state_updated` and `artifact_approved`
// arrive on the inbound wire (ui/js/bridge-bus.js) and are republished on the internal bus
// by ui/js/agent-events.js; a plan load or save republishes `dag:plan-applied`. Nothing here
// polls, and nothing here assumes a state change -- the colour follows the event.
//
// Selecting a node hands the artifact for that milestone to ui/js/diff-surface.js, which
// renders its AST targets against the bytes they would replace.
import { api } from "./api-client.js";
import { emit, on } from "./bus.js";
import { engineAvailable } from "./connection.js";
import { renderDiffPlaceholder, renderDiffSurface } from "./diff-surface.js";
import { showToast } from "./notify.js";
import { DOM, state } from "./store.js";

// The five states, in the vocabulary the store enforces (storage/db.py: TaskStatus).
const STATUSES = ["pending", "planned", "in_progress", "completed", "failed"];

// The artifacts recorded for this plan, keyed by task id. Written only here; the diff
// surface reads one artifact at a time, handed to it on selection.
const artifacts = new Map();

// Statuses learned from `task_state_updated` that the plan projection has not yet caught up
// with. A planning event writes the store and emits its state change, but does not re-emit
// `plan_updated`, so without this the halted node would still read "pending".
const statusOverrides = new Map();

// The edges of the current render, as `[fromId, toId]`. Kept so the SVG pass can redraw on
// resize without recomputing the graph.
let currentEdges = [];
let selectedTaskId = null;
let lastPlanFilename = null;

function normaliseStatus(status) {
  const value = String(status || "pending");
  return STATUSES.includes(value) ? value : "pending";
}

/** `PLAN.md` -> `PLAN`, the id the store keys a plan by (storage/db.py: plan_id_for). */
function planIdForActivePlan() {
  const filename = String(state.activePlan || "PLAN.md");
  const base = filename.replace(/^.*[\\/]/, "");
  const stem = base.replace(/\.[^.]+$/, "");
  return stem.trim() || "PLAN";
}

function textEl(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
}

/** Every milestone to draw, with its real blockers, in document order. */
function collectTasks() {
  const planJson = state.planJson || {};
  let sections = Array.isArray(planJson.sections) ? planJson.sections : [];

  // The flat view (`steps`) is the fallback: it carries each task's own section label, so
  // the grouping is reconstructed rather than lost.
  if (!sections.length) {
    const flat = Array.isArray(state.planTree) ? state.planTree : [];
    const bySection = new Map();
    for (const task of flat) {
      const key = String(task.section || "Roadmap");
      if (!bySection.has(key)) bySection.set(key, { id: `sec-${key}`, title: key, tasks: [] });
      bySection.get(key).tasks.push(task);
    }
    sections = Array.from(bySection.values());
  }

  const tasks = [];
  for (const section of sections) {
    for (const task of (Array.isArray(section.tasks) ? section.tasks : [])) {
      tasks.push({
        id: String(task.id || task.title || tasks.length),
        title: String(task.title || "(untitled)"),
        status: normaliseStatus(statusOverrides.get(task.id) || task.status),
        tag: task.tag ? String(task.tag) : "",
        section: String(task.section || section.title || ""),
        dependencies: (Array.isArray(task.dependencies) ? task.dependencies : []).map(String),
      });
    }
  }
  return tasks;
}

/** Layers and real edges, from the declared dependencies.
 *
 * A node's layer is its longest path from a root (``1 + max(layer of its blockers)``), so a
 * milestone is drawn strictly to the right of everything it waits on. Nodes that share a
 * layer are concurrent -- nothing in that column blocks anything else in it -- and they
 * stack vertically, which is what makes parallelism visible instead of reading as a
 * sequence. Ties are broken by document order so the layout is stable.
 *
 * A dependency naming an unknown node is dropped (the store drops dangling edges too), and a
 * cycle -- which the store should never hold -- cannot recurse forever: the memo is seeded
 * before the walk.
 */
function graphOf(tasks) {
  const byId = new Map(tasks.map((task) => [task.id, task]));
  const index = new Map(tasks.map((task, position) => [task.id, position]));
  const parents = new Map(tasks.map((task) => [task.id, []]));
  const edges = [];

  for (const task of tasks) {
    for (const dep of task.dependencies) {
      if (!byId.has(dep) || dep === task.id) continue;
      parents.get(task.id).push(dep);
      edges.push([dep, task.id]);
    }
  }

  const memo = new Map();
  const layerOf = (id) => {
    if (memo.has(id)) return memo.get(id);
    memo.set(id, 0);
    let value = 0;
    for (const parent of parents.get(id) || []) {
      value = Math.max(value, layerOf(parent) + 1);
    }
    memo.set(id, value);
    return value;
  };

  const layers = [];
  for (const task of tasks) {
    const layer = layerOf(task.id);
    while (layers.length <= layer) layers.push([]);
    layers[layer].push(task.id);
  }
  for (const layer of layers) {
    layer.sort((a, b) => index.get(a) - index.get(b));
  }

  return { layers, edges };
}

function buildNode(task) {
  const element = document.createElement("div");
  element.className = `dag-node dag-node--task dag-node--${task.status}`;
  element.dataset.nodeId = task.id;
  element.dataset.status = task.status;

  element.appendChild(textEl("div", "dag-node-title", task.title));
  const meta = task.section
    ? `${task.status.replace("_", " ").toUpperCase()} \u00b7 ${task.section}`
    : task.status.replace("_", " ").toUpperCase();
  element.appendChild(textEl("div", "dag-node-meta", meta));

  if (task.tag) element.appendChild(textEl("span", "dag-node-tag", task.tag));

  // No Approve control on the node itself. The graph shows state; acting on one milestone is
  // the inspector's job, and a button on every planned node put the same action in two places
  // and made the graph jump as nodes changed height.

  element.addEventListener("click", () => selectTask(task.id));
  return element;
}

/** Draws one SVG edge per real dependency, bowing long edges below the row. */
function renderEdges() {
  const flow = document.getElementById("dagFlow");
  const svg = document.getElementById("dagEdges");
  if (!flow || !svg) return;

  const nodes = new Map();
  flow.querySelectorAll(".dag-node").forEach((element) => nodes.set(element.dataset.nodeId, element));

  const width = flow.scrollWidth;
  const height = flow.scrollHeight;
  svg.setAttribute("width", String(width));
  svg.setAttribute("height", String(height));
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

  const paths = [];
  for (const [fromId, toId] of currentEdges) {
    const from = nodes.get(fromId);
    const to = nodes.get(toId);
    if (!from || !to) continue;
    const x1 = from.offsetLeft + from.offsetWidth;
    const y1 = from.offsetTop + from.offsetHeight / 2;
    const x2 = to.offsetLeft;
    const y2 = to.offsetTop + to.offsetHeight / 2;
    // An edge to the next node is a short link; one that skips nodes dips below the row so
    // it is visibly routed around them rather than drawn through them.
    const dip = x2 - x1 > 240 ? Math.min(72, 24 + (x2 - x1) / 12) : 0;
    const bend = Math.max(20, (x2 - x1) / 2);
    const stroke = to.dataset.status === "completed" ? "var(--dag-completed)" : "var(--dag-edge)";
    paths.push(
      `<path class="dag-edge" stroke="${stroke}" ` +
        `d="M ${x1} ${y1} C ${x1 + bend} ${y1 + dip}, ${x2 - bend} ${y2 + dip}, ${x2} ${y2}" />`,
    );
  }
  svg.innerHTML = paths.join("");
}

function renderEdgeNote(flow, edges) {
  const host = flow.parentElement;
  if (!host) return;
  const existing = host.querySelector(".dag-edge-note");
  if (edges.length) {
    if (existing) existing.remove();
    return;
  }
  const note = existing || document.createElement("div");
  note.className = "dag-edge-note";
  note.textContent = "No dependencies are declared in this plan, so the milestones are drawn in document order.";
  if (!existing) host.insertBefore(note, flow);
}

function render() {
  const flow = document.getElementById("dagFlow");
  if (!flow) return;

  flow.querySelectorAll(".dag-node, .dag-empty").forEach((element) => element.remove());

  const tasks = collectTasks();
  const graph = graphOf(tasks);
  currentEdges = graph.edges;
  renderEdgeNote(flow, graph.edges);

  if (!tasks.length) {
    flow.appendChild(textEl("div", "dag-empty", "No milestones to draw. Create a plan or add tasks to see the artifact DAG."));
    renderEdges();
    return;
  }

  const byId = new Map(tasks.map((task) => [task.id, task]));
  // One column per layer, so concurrent milestones stack vertically and a blocker always
  // sits in a column to the left of everything it gates.
  for (const layer of graph.layers) {
    const column = document.createElement("div");
    column.className = "dag-layer";
    for (const id of layer) {
      const task = byId.get(id);
      if (task) column.appendChild(buildNode(task));
    }
    flow.appendChild(column);
  }
  renderEdges();
  applySelection();
  renderInspector();
}

function applySelection() {
  const flow = document.getElementById("dagFlow");
  if (!flow) return;
  flow.querySelectorAll(".dag-node").forEach((element) => {
    element.classList.toggle("dag-node--selected", element.dataset.nodeId === selectedTaskId);
  });
}

function selectTask(taskId, { announce = true } = {}) {
  selectedTaskId = taskId;
  applySelection();
  // Selecting reveals the inspector: the rail renders one entity, so a selection that stayed
  // behind a closed panel would be a click that appeared to do nothing.
  openDagPanel();
  // Published so the document tree highlights the same entity. Both views listen and both
  // announce; the guard is the id comparison, so an echo cannot loop.
  if (announce) emit("node:selected", { taskId });
}

/** The inspector rail is on screen. */
export function openDagPanel() {
  if (DOM.dagPanelOverlay) DOM.dagPanelOverlay.style.display = "flex";
  if (DOM.btnDagPanel) DOM.btnDagPanel.setAttribute("aria-expanded", "true");
  renderInspector();
  // The stage just got narrower (or wider), so the SVG edges are recomputed against the new
  // bounds rather than left at the widths they had before the inspector took its column.
  emit("dag:view-shown");
}

export function closeDagPanel() {
  if (DOM.dagPanelOverlay) DOM.dagPanelOverlay.style.display = "none";
  if (DOM.btnDagPanel) DOM.btnDagPanel.setAttribute("aria-expanded", "false");
  emit("dag:view-shown");
}

export function toggleDagPanel() {
  const open = !!DOM.dagPanelOverlay && DOM.dagPanelOverlay.style.display !== "none";
  if (open) closeDagPanel();
  else openDagPanel();
}

function setInspectorText(id, value) {
  const element = document.getElementById(id);
  if (element) element.textContent = value;
}

/** The inspector rail: global metrics when nothing is selected, ONE node's payload when it is.
 *
 * The previous shape rendered every milestone's artifact as a scrolling list of cards. That is
 * gone: the rail answers "what is this one node" or "how is the plan doing", never both, and
 * never a list.
 */
function renderInspector() {
  const metrics = document.getElementById("inspectorMetrics");
  const node = document.getElementById("inspectorNode");
  if (!metrics || !node) return;

  const tasks = collectTasks();
  const selected = selectedTaskId ? tasks.find((task) => task.id === selectedTaskId) : null;

  if (!selected) {
    const total = tasks.length;
    const done = tasks.filter((task) => task.status === "completed").length;
    const running = tasks.filter((task) => task.status === "in_progress").length;
    const failed = tasks.filter((task) => task.status === "failed").length;
    setInspectorText("inspectorCompletion", `${total ? Math.round((done / total) * 100) : 0}%`);
    setInspectorText("inspectorWorkers", String(running));
    setInspectorText(
      "inspectorHealth",
      failed ? `${failed} failed` : (running ? "Running" : "Ready"),
    );
    metrics.hidden = false;
    node.hidden = true;
    return;
  }

  setInspectorText("inspectorNodeTitle", selected.title);
  const state = document.getElementById("inspectorNodeState");
  if (state) {
    state.textContent = selected.status.replace("_", " ").toUpperCase();
    state.className = `inspector-node-state inspector-state--${selected.status}`;
  }
  setInspectorText(
    "inspectorNodeMeta",
    `${selected.section || "Roadmap"}${selected.tag ? ` \u00b7 ${selected.tag}` : ""}`,
  );

  const artifact = artifacts.get(selected.id);
  const summary = document.getElementById("inspectorArtifact");
  if (summary) {
    summary.textContent = artifact
      ? `${artifact.summary || "Implementation plan"}`
        + (artifact.estimated_impact ? ` \u2014 ${artifact.estimated_impact}` : "")
      : "No proposed artifact for this milestone yet.";
  }

  const host = document.getElementById("inspectorActions");
  if (host) {
    host.textContent = "";

    if (selected.status === "planned" && artifact) {
      const approve = document.createElement("button");
      approve.type = "button";
      approve.className = "dag-approve-btn";
      approve.textContent = "Approve artifact";
      approve.addEventListener("click", () => approveArtifact(selected.id, approve));
      host.appendChild(approve);

      // A rejection is a review, not a veto: the critique is what makes the retry a *different*
      // question, so the control is disabled natively until there is something to say. The backend
      // refuses an empty critique too, but a UI that offers the click and then rejects the payload
      // is a UI that wastes the reviewer's time.
      const feedback = document.createElement("textarea");
      feedback.id = "reject-feedback";
      feedback.className = "reject-feedback";
      feedback.rows = 2;
      feedback.placeholder = "What is wrong with this plan? (required to reject)";
      feedback.setAttribute("aria-label", "Rejection feedback");

      const reject = document.createElement("button");
      reject.type = "button";
      reject.className = "dag-reject-btn";
      reject.textContent = "Reject artifact";
      reject.disabled = true;
      feedback.addEventListener("input", () => {
        reject.disabled = feedback.value.trim().length === 0;
      });
      reject.addEventListener("click", () => rejectArtifact(selected.id, feedback, reject));

      host.appendChild(feedback);
      host.appendChild(reject);
    }
  }

  metrics.hidden = true;
  node.hidden = false;
  if (artifact) renderDiffSurface(artifact);
  else renderDiffPlaceholder("No proposed artifact for this milestone. One is proposed when it is planned.");
}

/** Rejects a planned artifact: persists the critique and lets the swarm re-dispatch the node.
 *
 * The state is not assumed. The backend emits ``task_state_updated`` for the demotion, and a local
 * override covers the window before it arrives -- but the panel closes and the field is wiped on
 * success, because the reviewer's words now belong to that node's history, not to the next one.
 */
async function rejectArtifact(taskId, feedback, button) {
  const note = String((feedback && feedback.value) || "").trim();
  if (!note) return;  // the control is disabled for this; belt and braces
  if (!engineAvailable()) {
    showToast("The engine is unreachable. Reconnect before rejecting an artifact.", "error");
    return;
  }

  if (button) button.disabled = true;
  try {
    await api.reject_artifact(taskId, note, planIdForActivePlan());
    showToast("Artifact rejected; the milestone is back in the queue.", "success");
    if (feedback) feedback.value = "";
    selectedTaskId = null;
    closeDagPanel();
    // Reflect the demotion locally so the canvas does not sit on a stale PLANNED node while the
    // backend's task_state_updated travels.
    statusOverrides.set(String(taskId), "pending");
    render();
  } catch (err) {
    showToast(`Rejection failed: ${(err && err.message) || err}`, "error");
    if (button) button.disabled = false;
  }
}

/** Approves a planned milestone's artifact, which releases the run the plan halted.
 *
 * The colour change is not assumed here: the backend emits `task_state_updated`, which
 * repaints the node. A refusal (a stale UI, a double click) is surfaced as the error it is.
 */
async function approveArtifact(taskId, button) {
  const artifact = artifacts.get(taskId);
  const planId = (artifact && artifact.plan_id) || planIdForActivePlan();

  // Approval releases the execution pass, so it is an execution trigger like any other.
  if (!engineAvailable()) {
    showToast("The engine is unreachable. Reconnect before approving an artifact.", "error");
    return;
  }

  if (button) {
    button.disabled = true;
    button.textContent = "Approving\u2026";
  }
  try {
    // The run's id travels with the approval, so the released execution writes into the shadow
    // of the run that proposed the artifact rather than a fresh one nobody can review (Phase 21).
    const res = await api.approve_artifact(taskId, planId, state.activeIntentId);
    showToast("Artifact approved; execution released.", "success");
    // Approval dispatches the execution pass, so the UI re-arms the same run lock a
    // normal launch arms.
    if (res.dispatched) emit("run:begin");
    else if (res.note) showToast(res.note, "info");
  } catch (err) {
    showToast(`Approval failed: ${(err && err.message) || err}`, "error");
    if (button) {
      button.disabled = false;
      button.textContent = "Approve";
    }
  }
}

// ---------------------------------------------------------------------------
// The event wire
// ---------------------------------------------------------------------------
// Registered at module top level, before the bootstrap can emit, so the first plan load is
// already handled (main.js imports this before wire.js registers the DOMContentLoaded boot).

on("dag:plan-applied", (planData) => {
  const filename = (planData && planData.filename) || state.activePlan;
  if (filename !== lastPlanFilename) {
    // A different plan: its artifacts and the selection belong to the old one.
    artifacts.clear();
    selectedTaskId = null;
  }
  lastPlanFilename = filename;
  // The plan projection is authoritative for status, so an override recorded before it was
  // refreshed has served its purpose (a planning event does not re-emit `plan_updated`; a
  // completed run does).
  statusOverrides.clear();
  render();
});

on("dag:artifact-planned", (artifact) => {
  if (!artifact || !artifact.task_id) return;
  const taskId = String(artifact.task_id);
  artifacts.set(taskId, artifact);
  statusOverrides.set(taskId, "planned");
  render();
  // The plan just halted: select it so the inspector holds its payload for review.
  selectedTaskId = taskId;
  render();
  openDagPanel();
});

// The canvas had no measurable width while hidden, so its edges are drawn when it is shown.
on("dag:view-shown", () => {
  render();
});

// Selection is shared with the document tree: whichever view the user clicks, the same entity
// is highlighted in both and drives the inspector.
on("node:selected", (payload) => {
  const taskId = payload && payload.taskId;
  if (taskId && taskId !== selectedTaskId) selectTask(taskId, { announce: false });
});

on("dag:artifact-approved", (event) => {
  if (!event || !event.task_id) return;
  statusOverrides.set(String(event.task_id), "in_progress");
  render();
});

on("dag:task-state", (event) => {
  if (!event || !event.task_id) return;
  statusOverrides.set(String(event.task_id), normaliseStatus(event.status));
  render();
});

// ---------------------------------------------------------------------------
// Edge redraw on resize
// ---------------------------------------------------------------------------
// The flow's width is CSS-driven, so the edges are redrawn whenever it changes size --
// including when the workbench switches back from the raw-markdown view, where the panel
// had no measurable width at all.
function watchFlowSize() {
  const flow = document.getElementById("dagFlow");
  if (!flow || typeof ResizeObserver !== "function") return;
  const observer = new ResizeObserver(() => renderEdges());
  observer.observe(flow);
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", watchFlowSize, { once: true });
  } else {
    watchFlowSize();
  }
}
