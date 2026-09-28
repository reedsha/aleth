# 2nd Polishing Pass — Elevated Recommendations

**Status:** recommendations captured, not yet turned into an implementation plan.
**Relationship to pass 1:** these four refinements layer on top of `1ST_POLISHING.md`. Item 1
below (the console) interacts with pass 1's Phase 1 — if the dock button strip is removed
there, the dock head shrinks to the status row, which is exactly the shape an elastic console
wants.

> This file and `1ST_POLISHING.md` are planning documents, not roadmaps. They are listed in
> `NON_PLAN_MD_FILES` (`tools/workspace.py`) so the plan switcher does not offer them and
> selecting one cannot compile a non-plan document into `plan.json`.

---

## 1. Elastic, Closable Console

### Recommendation

> The persistent single-line console wastes valuable vertical real estate. Hide it entirely
> during idle states. Implement a standard Ctrl + ` toggle to summon it, add a top
> drag-handle to allow resizing it up to 30% of the viewport, and configure it to
> auto-reveal only when an agent throws an error or requires manual intervention.

### Current state (grounded)

- The console is `.dock-logs` (`#dockLogs`) inside `#bottomDock`, rendered as the dock's
  flexible remainder — **always visible**, never hidden.
- Geometry: `.bottom-dock { height: 240px }` by default; `#dockResizeHandle` is a 10px seam
  driven by `initDockResize()` in `ui/js/dock.js`, clamped to **60% of the pane**
  (`maxHeight()`), with `MIN_HEIGHT = 160`.
- Existing controls in the console bar: **⤢** expands it over the workbench
  (`toggleConsoleOverlay`), **⧉** detaches it to a real second pywebview window
  (`detachConsole`), **Clear** empties it. See `ui/js/console.js` / `ui/css/console.css`.
- **No Ctrl + ` binding** exists; no idle-hiding; no auto-reveal on error.

### Landing points

`ui/js/console.js` (state + toggle), `ui/js/dock.js` (clamp + seam visibility),
`ui/css/console.css`, `ui/css/dock.css`, `ui/js/wire.js` (the key binding),
`ui/js/agent-events.js` (auto-reveal triggers), `ui/js/dom.js`, `ui/index.html`.

### Shape of the change

- A `consoleVisible` state; **Ctrl + `** toggles it; the dock's default becomes
  collapsed-at-idle.
- Clamp the seam to **30% of the viewport** instead of 60% of the pane.
- Auto-reveal hooks, all already on the event wire:
  - `agent_error` → `renderErrorBadge()` in `ui/js/agent-events.js`;
  - `workflow_stopped`;
  - the two "needs a human" summaries: the `fix_bug` sufficiency refusal (status
    *Input Required*) and the Normalization Gate refusal (status *Needs Manual Structure*).
- Hide `#dockResizeHandle` while the console is hidden, or the seam resizes nothing.

### Decisions needed

- **Stop must stay reachable.** `#btnDockStop` lives in `.action-dock-bottom-row`, which is
  the dock head — hiding the console must not hide Stop during a run.
- Does the auto-reveal *stay* open after the error, or fold itself away once the run ends?
  (Recommend: stay open until the next successful run, so the error is not lost.)
- Pass 1 removes the six-button strip; confirm this item lands **after** that, so the dock
  height maths is only retuned once.

---

## 2. Animate the Agentic State

### Recommendation

> The static green dots for "Main Agents" and "Coder Agents" fail to communicate active
> cognitive load. Introduce a subtle, pulsating "thinking" animation or a glowing border
> around the avatar when an agent is actively generating code to confirm the "dual-brain" is
> working and the app hasn't frozen.

### Current state (grounded)

- `renderSidebarAgents()` (`ui/js/agents.js`) builds each `.agent-list-item` with an empty
  `<span class="agent-status-indicator"></span>` — an indicator that **no code ever sets
  state on**. That is the "static green dot".
- There is already a pulsing precedent to reuse rather than invent:
  - `.agent-list-item.delegation-highlight { animation: delegationPulse 0.9s ease-in-out infinite alternate }`
    (`ui/css/sidebar.css`), toggled by `triggerDelegationAnimation()` in
    `ui/js/agent-events.js`;
  - `.status-dot.running { animation: pulseDot 1s infinite alternate }` (`ui/css/stage.css`).

### Landing points

`ui/js/agent-events.js` (set/clear the state), `ui/js/agents.js` (apply it on render),
`ui/css/sidebar.css` (the animation), `ui/index.html`.

### Shape of the change

- On `architect_spawn` / `coder_spawn`, add `.thinking` to `#navAgent_<id>`; clear it on
  `coder_summary`, `architect_summary`, `agent_error` and `workflow_complete`.
- Reuse the `delegationPulse` / `pulseDot` keyframe idiom so the motion reads as one system.

### Correctness detail worth noting

`renderSidebarAgents()` re-renders the lists wholesale (for example on `agents_updated`),
which **drops any class set directly on the DOM**. The thinking state therefore has to be
held in `state` (an `activeAgentIds` set) and re-applied inside `renderSidebarAgents()`,
not toggled only from the event handler — otherwise a registry refresh silently stops the
animation mid-run.

### Decisions needed

- Should the *main* agent pulse for the whole run, or only while it is between delegations?
- Does the avatar glow want the accent palette (cyan/amber) or stay monochrome with motion
  alone? Pass 1's item 4 reserves colour for state, so motion-plus-tint is in keeping.

---

## 3. File Tree Information Diet

### Recommendation

> The left sidebar displays exact file sizes (e.g., 39.3 KB) next to every document. This is
> rarely the primary metric a developer needs at a glance. Strip the file sizes to declutter
> the panel and replace that right-aligned space with color-coded version control or sync
> status indicators (e.g., M for modified, U for untracked).

### Current state (grounded)

- `renderTreeContents()` in `ui/js/sidebar.js` emits each leaf as
  `<span class="tree-size">${formatTreeFileSize(file.size)}</span>` inside a
  `.tree-file-row`.
- The listing is `list_workspace_files()` (`tools/file_ops.py`), surfaced through
  `app.get_workspace_info()`. It returns `{name, path, size}` — **there is no VCS or sync
  status field anywhere in the payload**.
- The harness pins `sidebar-tree`, `tree-file-row` and `tree-folder` as cross-module class
  contracts, so those names must survive.

### Landing points

`ui/js/sidebar.js`, `ui/css/sidebar.css`, `tools/file_ops.py`, `app.py`, and probably a new
`tools/git_status.py`.

### Blocker — decide the signal source before building

**The app's own workspace is `my_project_workspace/`, which is gitignored in this repo.**
`git status` inside it reports nothing, so a VCS badge would be permanently blank in the
default setup. Three honest options:

1. **Git status, when the workspace is a repo.** Shell out to `git status --porcelain` over
   `PROJECT_DIR`, guarded by "is this a git repo"; no badge otherwise. Correct for a
   user-selected real project; empty for the sandboxed default.
2. **"Changed since the last run"**, derived from `.deepagents_backups/` snapshots — the only
   signal that means something for the sandbox. Reads as agent-modified vs untouched.
3. **Both**, with git taking precedence when available.

Recommendation: decide between 1 and 3 first; it changes whether a backend pass is needed at
all. A frontend-only version (drop the sizes, render nothing yet) is a safe half-step that
still delivers the decluttering half of the recommendation.

### Shape of the change

- Delete the `.tree-size` span; add a right-aligned `.tree-vcs` badge with a
  colour-coded letter (`M` amber, `U` green, clean = nothing).
- Add the status source on the backend and carry it on each entry in the listing.
- `formatTreeFileSize()` becomes dead once sizes are dropped — remove it, or keep it for the
  Files modal which still shows sizes.

---

## 4. Progress Bar State Nuance

### Recommendation

> The "Roadmap Completion" bar currently fills with a single solid teal color. Because AI
> generation is unpredictable, segment this bar to show distinct task states: solid green for
> verified completion, an animated yellow segment for the active task, and a red segment for
> tasks that fail and require human intervention.

### Current state (grounded)

- Markup: `.plan-progress-card` → `.progress-bar-track` → `.progress-bar-fill`
  (`#planProgressBarFill`), in the workbench tree header.
- Fill: `updateProgressMeter(completed, total)` in `ui/js/plan-tree.js` sets
  `width: percent%` on a single div whose background is a fixed
  `linear-gradient(90deg, #0ea5e9, #10b981)`. The same function mirrors the figure into the
  dock pill `#txtDockProgress`.
- The label `#txtPlanProgressRatio` reads `completed/total (pct%)`.

### Blocker — there is no "failed" state to render

`updateProgressMeter` is handed only `completed` and `total`. The plan AST has exactly three
marks: `_status_from_mark()` in `tools/plan_parser.py` maps `[x] → completed`,
`[-] → in_progress`, `[ ] → pending`. **Nothing maps to a failure**, and Rollback resets a
task to `pending` — so today a task that failed and a task never started are
indistinguishable. A red segment has no data behind it yet.

### Options

| Option | Cost | Durability |
|---|---|---|
| (a) Add a fourth mark (`[!]`) for failed | parser + compiler + metrics + tree + CSS + tests | durable, survives reload |
| (b) Derive "failed" in the frontend from the run's `agent_error` | frontend only | not persisted; lost on reload |
| (c) Segment with what exists now: green `completed`, animated amber `in_progress`, neutral remainder | frontend only | immediate, but no red |

**Recommendation:** land **(c)** immediately — it is pure frontend, needs no schema change, and
already communicates the nuance the recommendation is asking for — then do **(a)** as a
follow-up once the failed-state semantics are agreed.

### Decisions needed (for option (a))

- Does a failed task block the next task, or does the run continue?
- Is a failure retried automatically, or does it wait for a human (which is what the red
  segment implies)?
- What mark does the plan markdown use, and does it round-trip through
  `compile_plan_json_to_markdown` without the token leaking into the title the way an
  unregistered `[UI]` once did?

### Landing points

`ui/js/plan-tree.js` (the count + render), `ui/css/plan-tree.css` (segments + keyframes); for
option (a) additionally `tools/plan_parser.py`, `tools/plan_state.py`,
`orchestration/workflow/*` and the pinned characterization tests.

---

## Suggested ordering within this pass

1. **Progress bar (c)** — smallest, self-contained, immediate visual payoff.
2. **Agentic state** — reuses an existing animation idiom; needs the `state`-held flag noted above.
3. **Elastic console** — after pass 1's dock changes, so geometry is retuned once.
4. **File tree diet** — last, because it is the only one that may need a backend decision before
   any code is written.
