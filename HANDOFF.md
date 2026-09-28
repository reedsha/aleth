# 🚀 DEEPAGENTS STUDIO: MASTER AGENT HANDOFF & ARCHITECTURE SPECIFICATION

> **CRITICAL DIRECTIVE FOR ANY INCOMING AGENT:**
> Read this document completely before modifying or executing any code. This single
> document contains the full architecture, execution flow, component map, strict
> constraints, and current state. You will not need to ask redundant questions or
> hallucinate context.
>
> Everything in §2, §5, §6 and §9 was verified against the working tree. Where a
> claim could drift (file inventories, current plan state), it is stated as of the
> date of the last edit rather than as a permanent fact.

---

## 📌 1. Executive Summary & Objective

**Project Name:** DeepAgents Desktop UI & Multi-Agent Orchestrator Studio
**Primary Goal:** A native desktop application built with **Python (`pywebview`)**,
**HTML5/CSS3**, and **vanilla JavaScript**, styled as an off-code development IDE:
neutral high-contrast dark greys, 1px pane borders, monospaced technical data, and
colour reserved for state rather than decoration. It coordinates multi-agent software
engineering workflows anchored in a dynamic markdown plan file (`PLAN.md`) plus its
machine-state twin (`plan.json`).

### The Problem It Solves

1. Eliminates multi-turn LLM context window explosion by enforcing **stateless
   sessions per prompt**; project memory is stored exclusively on disk in the
   dynamically selected `.md` plan file.
2. Eliminates rogue architectural behavior by enforcing **mandatory delegation**: the
   Lead Architect plans, verifies, and delegates, but **NEVER writes application
   code**. Only Coder sub-agents write code and execute arbitrary shell commands.
   Administrative intents (plan edits, analysis, recommendations) short-circuit to the
   Architect with **zero Coder spawns**.
3. Provides real-time visibility through a **four-pane IDE layout**: collapsible left
   pane (agents, workspace files, environment), centre Plan Workbench with diffs and
   live preview, right plan tracker, and a resizable bottom dock holding the action
   toolbar and the live console — all driven by a **zero-token native markdown
   parser** (`tools/plan_parser.py`) rather than an LLM round-trip.

---

## 🗂️ 2. Repository & File Structure

```
deepagents/
├── app.py                     # pywebview host: window, BridgeAPI (the JS-facing API), worker thread
├── main.py                    # Entrypoint: desktop UI by default, headless with --cli
├── registry.py                # AgentRegistry facade (~147 lines) — delegates to orchestration/
├── HANDOFF.md                 # Master handoff document (this file)
│
├── agents/                    # Agent definitions. These double as DATA FILES:
│   │                          #   the prompt editor rewrites them on disk at runtime.
│   ├── __init__.py            # Exports architect_agent, coder_deep, coder_standard
│   ├── architect.py           # software-architect: core prompt + custom directives
│   └── coders.py              # coder-deep and coder-standard worker definitions
│
├── orchestration/             # Extracted from the former monolithic registry.py
│   ├── __init__.py            # Public re-exports of the three modules below
│   ├── agent_catalog.py       # Reflection over agents/ -> the catalogue the UI renders
│   ├── plan_session.py        # Roadmap scaffolding + reading the active plan
│   ├── prompt_editor.py       # Persists edited prompts back into the agents/ sources
│   └── workflow/
│       ├── __init__.py
│       ├── runner.py          # Intent resolution, dispatch, error handling
│       ├── context.py         # WorkflowContext shared by every branch
│       ├── events.py          # THE single definition of the frontend event wire format
│       ├── reasoning.py       # The Architect's System 2 answer as streamed UI text
│       ├── ledger.py          # Living Behavioral Ledger: the 🟢 log line + state summary
│       ├── actions_admin.py   # Administrative bypass: update_plan, analyze, recommend
│       ├── actions_impl.py    # Delegated implementation: fix_bug, next_step, custom
│       └── templates.py       # Deliverable templates (solution engine, ui_view, weather API)
│
├── tools/                     # Tooling package
│   ├── __init__.py            # Clean re-exports of the file tools
│   ├── file_ops.py            # @tool primitives, workspace listing, masked .env reader
│   ├── file_tools.py          # Compatibility facade that re-exports its siblings
│   ├── plan_parser.py         # Markdown <-> plan dict (markdown-it-py AST); sub-steps,
│   │                          #   Global State Summary, [UI] detection, deliverables
│   ├── plan_state.py          # Dual-sync: plan.json <-> the active markdown plan
│   ├── recovery.py            # Backups, codebase/plan audit, sync resolution, rollback
│   ├── code_metrics.py        # Static source metrics (complexity, security) for Analyze
│   ├── test_runner.py         # Runs the test a task wrote; the result view's real verdict
│   ├── git_status.py          # VCS status for the tree (blank outside the workspace's repo)
│   ├── workspace.py           # Project dir + active-plan pointer (process-wide state)
│   ├── shell_tools.py         # Full shell (Coders) + restricted shell (Architect)
│   └── build_ui_bundle.py     # Inlines ui/js/*.js + ui/css/*.css into ui/index.html
│
├── ui/
│   ├── index.html             # Layout markup + two generated bundle regions (style, then UI)
│   ├── css/                   # 19 stylesheets (source of truth), inlined in order
│   │   ├── base.css                     # Design tokens (:root) + reset & base styles
│   │   ├── sidebar.css                  # Left pane: agents dock & workspace card
│   │   ├── stage.css                    # Centre stage & the execution cards
│   │   ├── actions.css                  # Dock action toolbar & the docked command drawer
│   │   ├── plan-tree.css                # Right rail & the workbench roadmap: bento, cards
│   │   ├── modals.css                   # Confirmation, plan setup & workspace files dialogs
│   │   ├── tiered-prompt-editor.css     # Tiered prompt editor (core rules panel)
│   │   ├── ui-vision.css                # Multimodal UI vision parameters
│   │   ├── audit-modal.css              # Audit/summary rail & the plan panels it hosts
│   │   ├── rollback-modal.css           # Rollback modal & shared keyframes
│   │   ├── dock.css                     # Bottom dock geometry & the drag seam
│   │   ├── workbench.css                # Centre-stage plan document & the bento header grid
│   │   ├── code-surface.css             # Highlighting surface for parameter textareas
│   │   ├── console.css                  # Dock console transcript
│   │   ├── preview.css                  # Live preview overlay
│   │   ├── sidebar-panels.css           # Collapse rails + tree/env panels
│   │   ├── settings.css                 # Provider/model route settings panel
│   │   ├── command-palette.css          # Ctrl/Cmd+K action palette
│   │   └── result-view.css              # Polymorphic per-action result surface (appended last)
│   └── js/                    # 21 modules (source of truth), inlined in order
│       ├── state.js           # Global `state` and `DOM` cache
│       ├── dom.js             # Element lookup helpers + escapeHtml()
│       ├── bootstrap.js       # pywebview handshake, opt-in ?demo=1 data, hydration
│       ├── plan-tree.js       # Roadmap from plan.json: bento header, phase cards,
│       │                      #   step cards, sub-step rows, progress meter
│       ├── actions.js         # Action drawer open/close + confirm, execution lifecycle
│       ├── plan-modals.js     # Switch-plan and create-plan rail panels
│       ├── agents.js          # Sidebar agent navigation & the system prompt editor
│       ├── workspace.js       # File browser, codebase-sync audit, rollback modals
│       ├── agent-events.js    # window.onAgentEvent: the inbound event stream renderer
│       ├── visuals.js         # Toast notifications & the offline workflow simulator
│       ├── dock.js            # Bottom dock resize + the drawer's open/close pane state
│       ├── workbench.js       # Plan Workbench: the active plan in the centre stage
│       ├── code-surface.js    # Syntax highlighting for the parameter textareas
│       ├── console.js         # Dock console: terminal-style echo of the agent stream
│       ├── preview.js         # Live preview of the workspace's generated interface
│       ├── sidebar.js         # Collapsible side panes & the workspace file tree
│       ├── env.js             # Environment variable panel (values masked in Python)
│       ├── settings.js        # Provider/model route settings panel
│       ├── command-palette.js # Ctrl/Cmd+K action palette + its key handling
│       ├── result-view.js     # Polymorphic per-action result renderers (diff, dashboard…)
│       └── wire.js            # DOM event wiring & bootstrap — MUST stay last
│
├── tests/
│   ├── test_characterization.py  # stdlib unittest: 98 tests pinning backend behaviour
│   ├── test_laya.py              # stdlib unittest: the System 1 heuristic classifier
│   ├── test_laya_model.py        # stdlib unittest: the env-gated ModernBERT backend
│   └── ui_startup_contract.js    # Node harness: 28 checks pinning bundle + markup contracts
│
├── my_project_workspace/      # Default active project directory (PROJECT_DIR, gitignored)
│   ├── PLAN.md                # Dynamic memory anchor & active roadmap (human-readable)
│   ├── plan.json              # Machine state twin, kept in sync by tools/plan_state.py
│   ├── main.py / weather_api.py / ui_view.html   # Generated production code
│   ├── test_*.py              # Generated test suites
│   └── .deepagents_backups/   # Per-task deliverable snapshots (rollback source)
│
└── venv/                      # Python 3.12 Virtual Environment
```

`main.py` and `app.py` both import the module named `registry`, which is why the
package that registry.py delegates to is called `orchestration/` — a package named
`registry` would shadow the module.

---

## ⚙️ 3. Environment & Quick Launch Commands

All Python dependencies are installed in the local virtual environment (`.\venv`).
There is no `package.json`; Node is used only to run the UI contract harness.

```powershell
# 1. Launch the Native Desktop Application (pywebview):
.\venv\Scripts\python.exe app.py

# 1b. Same, with WebView2 DevTools open (diagnose a window that renders but
#     ignores input: the console and network log show which request or script failed):
.\venv\Scripts\python.exe app.py --debug

# 2. Run Headless Multi-Agent Verification (CLI Mode):
.\venv\Scripts\python.exe main.py --cli "Implement weather service endpoints"

# 3. Test Imports & Syntax Across Entire Project:
.\venv\Scripts\python.exe -c "import app, registry, tools, agents; print('All systems operational!')"

# 4. Verify Plan Tree Native Parser (Zero LLM Tokens):
.\venv\Scripts\python.exe -c "from tools.file_tools import read_file, parse_plan_tree; print(parse_plan_tree(read_file.invoke({'filename': 'PLAN.md'})))"
```

### The verification gates (run all of them after any change)

```powershell
# A. Frontend contract: bundle freshness, module order, id inventory, markup contracts.
#    28 checks. Also fails if ui/index.html has drifted from ui/js + ui/css.
node tests/ui_startup_contract.js

# B. Backend characterization suite. stdlib unittest — pytest is NOT installed.
.\venv\Scripts\python.exe -m unittest discover -s tests -t .

# C. Just the bundle-staleness check, without the rest of the harness:
.\venv\Scripts\python.exe tools\build_ui_bundle.py --check

# D. Markup balance audit of the hand-authored body region. The bundles are inlined, so a
#    stray </div> here ends a container early and hoists every later sibling out of the
#    rendered tree — the plan sidebar first among them. The two counts must be equal.
.\venv\Scripts\python.exe -c "import pathlib; t=pathlib.Path('ui/index.html').read_text(encoding='utf-8'); r=t[t.index('END STYLE BUNDLE'):t.index('BEGIN UI BUNDLE')]; print('<div', r.count('<div'), '</div>', r.count('</div>'))"
```

One expected non-failure: the backend suite prints
`[DualSync] Error reading plan.json: ...` on a workspace that has no machine state
yet. That is a diagnostic line, not a test failure.

### Frontend change protocol (hard rules)

`ui/js/*.js` and `ui/css/*.css` are the source of truth, but `ui/index.html` embeds
generated copies of them so that startup performs no per-file HTTP request. WebView2
fails individual subresource fetches intermittently, which can leave the window
rendered but completely inert.

Therefore, after editing any `ui/js` or `ui/css` file:

1. Re-run `.\venv\Scripts\python.exe tools\build_ui_bundle.py`.
2. Re-run `node tests/ui_startup_contract.js` — it reports `stale` if you forget.

Additional invariants:

- **Never** split the frontend into multiple `<link>`/`<script src>` tags.
- New JS modules append **before** `wire.js` in `JS_MODULES`; new stylesheets append
  **last** in `CSS_MODULES`. Do not reorder existing entries.
- A new entry point (any function the wiring calls) must be added to the head
  watchdog's `required` list near the top of `ui/index.html`. It currently names 21.
- Hand edits to the body markup of `ui/index.html` are allowed; hand edits **inside**
  the two marked regions are not. Run gate D after any markup edit.
- A failure in a startup step is reported as a visible banner — that is intended
  behaviour, not a bug.

> Because the bundles are inlined, the hand-authored body markup of `ui/index.html`
> now begins far below the generated regions (around line 4360), not near the top.
> Only the two marked regions are generated; the markup between them is hand-edited.

---

## 🏛️ 4. Core Architecture & Two-Tier Agent Taxonomy

Agent definitions are never hardcoded inside UI views. `registry.AgentRegistry`
delegates catalogue building to `orchestration/agent_catalog.py`, which reflects over
`agents/` and categorizes the result into two distinct tiers:

### Tier 1: Main Agents (Coordinators)
- **Agent ID:** `software-architect` (Lead Software Architect)
- **Implementation:** compiled state graph / coordinator (`agents/architect.py`).
- **Permissions:** **Restricted Shell Access** (`execute_restricted_command`, for
  `py_compile`, `pytest`, verification only).
- **Absolute Rule:** the Architect is strictly a planner, verifier, and delegator. It
  **NEVER writes application code** or creates files directly. It **ALWAYS delegates**
  implementation to Coder sub-agents. File writes are limited to the active plan file
  and `plan.json`.
- **Administrative Bypass:** plan edits, codebase analysis, and recommendations are
  executed by the Architect directly, with zero Coder spawns and zero Coder tokens.

### Tier 2: Coder Agents (Sub-Agents)
- **Agent IDs:**
  - `coder-deep` (Senior Backend Coder): complex logic, algorithms, API microservices.
  - `coder-standard` (Junior Developer): scaffolding, boilerplate, unit tests, docs.
- **Permissions:** **Full Shell Access** (`execute_shell_command`) + full file writing.
- **Standardized Logging Protocol:** when a Coder completes a task it writes an entry
  into the active `.md` plan file:

  ```markdown
  ## [<Task Title>]
  - **Status:** Completed
  - **Files Created/Modified:** `<code_file>`, `<test_file>`
  - **Summary of Work:** <Description of deliverables>
  ```

---

## 🔄 5. End-to-End Execution Flow

The old free-text input bar and its "Are you sure…?" confirmation modal are gone.
Execution is now intent-first: the user picks one of six toolbar buttons in the dock
action panel, each of which opens the **action drawer** to collect its
specific parameters (target task, vision mockup, custom directive text). Confirming
that drawer is the confirmation step.

```
[Action Control Panel]  (bottom dock, six flat toolbar buttons)
      │  fix_bug │ next_step │ update_plan │ analyze │ recommend │ custom
      ▼
[Action Drawer]  (openActionDrawer(actionType, extraParams))
      │  User reviews the targeted task / supplies the directive, then confirms
      ▼
[BridgeAPI.start_execution(user_message, action_type, action_params)]  (app.py)
      │  Spawns a daemon worker thread; wires emit_event -> window.evaluate_js
      ▼
[AgentRegistry.run_agent_workflow]  (registry.py -> orchestration/workflow/runner.py)
      │
      ├── resolve_intent(): an explicit action_type is never overridden; only
      │      "custom" is auto-detected from an [ACTION: ...] tag in the message.
      ├── load_plan_state() + get_active_plan_filename()
      ├── emit "workflow_started", then "architect_spawn"
      │
      └── Dispatch:
             update_plan -> actions_admin.update_plan_action   (no Coder spawn)
             analyze     -> actions_admin.analyze_action       (no Coder spawn)
             recommend   -> actions_admin.recommend_action     (no Coder spawn)
             fix_bug     -> actions_impl.fix_bug_action        (snapshot key "bugfix")
             next_step   -> actions_impl.next_step_action      (snapshot key = task id)
             custom      -> actions_impl.custom_action         (snapshot key "custom")
```

### The delegated (Coder) branch

```
1. Snapshot the task's deliverables into
   .deepagents_backups/<key>/  together with _meta.json.
2. emit "delegation"      -> the right-pane tree pulses the target card
   emit "coder_spawn"     -> a Coder card slides in beside the Architect card
3. The Coder writes source and test files, ticks the task's checkbox in the plan,
   and appends its standardized progress entry.
4. emit "plan_updated"    -> applyPlanData() -> right-pane tree + progress meter
                             + the workbench document all re-render from the new
                             payload. This is the single funnel for plan data.
5. Architect verification: restricted shell (py_compile / pytest), then
   emit "architect_summary" with the approval and the proposed next steps.
6. emit "workflow_complete" -> the console settles, the dock Stop button hides,
   and the diff pane has something to show.
```

### The event wire format

Every payload reaches JavaScript through pywebview's `evaluate_js`, and the UI
switches on `type` in `ui/js/agent-events.js`. `orchestration/workflow/events.py` is
the single definition of this format — key order is part of the contract, because
payloads are JSON-serialised in insertion order.

| `type` | Meaning |
| --- | --- |
| `workflow_started` | A run began; carries the message, plan file and resolved intent |
| `architect_spawn` | The Architect card is created |
| `log` | One streamed line of agent narration (`log_type` drives styling) |
| `tool_call` / `tool_result` | A tool invocation and its paired outcome; animated as one unit |
| `delegation` | The Architect has chosen a Coder and a target task |
| `coder_spawn` | The Coder's card is created |
| `coder_summary` | The Coder's deliverable summary |
| `architect_summary` | The Architect's verification verdict and next steps |
| `plan_updated` | Plan data changed; the payload is the plan + its parsed tree |
| `agent_error` | A run failed; carries `can_retry` |
| `workflow_stopped` | A stop request was honoured |
| `workflow_complete` | The run finished (`status` is `finished` or `error`) |
| `workspace_changed` | The active project directory changed |
| `agents_updated` | The agent catalogue changed (e.g. a prompt was saved) |

> Do **not** add an `"exists"` key to `plan_updated` payloads — the characterization
> suite pins the exact wire shape in three places.

---

## 🖥️ 6. Desktop UI Specifications & Reference Aesthetic

The interface is an **off-code development IDE**: flat neutral dark greys in the
spirit of the VS Code default theme, sharp 1px borders between panes instead of drop
shadows, monospaced technical data (`--font-mono`), crisp sans-serif UI text
(`--font-ui`), and colour reserved for **functional state** only — blue `#0ea5e9` for
selection/primary, cyan `#06b6d4` for running, amber `#f59e0b` for warning/in-progress,
red `#ef4444` for stop/danger. There are no decorative gradients or ambient glows.

**Layout chain:**

```
body{height:100vh;overflow:hidden}
└── .app-layout{display:flex;height:100vh}
    ├── aside.sidebar#leftSidebar          (250px, collapses to a 52px rail)
    ├── main.main-stage
    │   ├── header.top-status-bar          (48px)
    │   └── section.workspace-view#chatWorkspaceView   (column)
    │       ├── .stage-center              (row: workbench + diff pane)
    │       └── section.bottom-dock#bottomDock
    └── aside.right-plan-sidebar#rightPlanSidebar   (320px, collapses to a 56px rail)
```

### 1. Left Pane — 250px, collapsible to a ~52px rail

- **Brand row:** `DEEPAGENTS` + `STUDIO` badge + the collapse chevron (`btnToggleLeftSidebar`).
- **Workspace card:** shows the active folder (`my_project_workspace`). Clicking it
  opens the native OS folder dialog; the `Change` button does the same. Dialog cleanly
  exits on the first click of `Cancel` or `X` without reopening.
- **MAIN AGENTS / CODER AGENTS:** lists with count pills. Clicking an agent switches
  the stage to the prompt editor view.
- **WORKSPACE FILES:** the same listing the Files modal reads, grouped into folders in
  the frontend. Click a folder row to expand/collapse; nested files are indented, root
  files sit below the folders. Capped at 300 rendered rows.
- **ENVIRONMENT:** one row per variable in the app's own `.env` — **name, set/unset
  flag and character count only; the value never crosses the bridge**. The eye icon
  toggles a second line with that metadata and the note "value stays in the backend".
  An absent `.env` is not an error.
- **Prompt editor view (full-stage):** back button, tiered editor with the core rules
  rendered read-only, tool pills, character counter, `Reset`, and
  **`Save Custom Directives`** (persists into the `agents/*.py` source and reloads in
  memory with zero app restarts).

Both panes collapse by changing the `<aside>` width (a class + the `--sidebar-*-rail`
tokens), **never** with `display:none` — see bug 8 in §8.

### 2. Centre Pane — the Plan Workbench

The centre stage is no longer a blank void with a hero prompt. It renders the active
plan as a read/write document:

- **Bar:** breadcrumbs (`workspace › PLAN.md`), a `● unsaved` marker, the task-count
  stat, and `Edit` / `Save` / `Discard`.
- **Body:** a code-editor style line-number gutter beside the rendered document; in
  edit mode the same surface becomes a textarea. `Save` writes the plan and re-syncs
  `plan.json`.
- **Status strip:** the plan path and its metadata.
- **Execution stage:** hidden until a run starts, then a 50/50 grid — Architect card on
  the left (blue accents, `#60a5fa`), Coder card on the right (slate accents,
  `#cbd5e1`), each with live monospaced logs, a status pill, a summary container, and a
  close button that unlocks on completion.
- `.stage-center` is a **row** whose second child is the tracked-edits (`diff`) pane;
  it is also the `position:relative` anchor for the preview overlay.
- **Live preview** (`Preview` in the top status bar): reads the workspace's generated
  interface through the bridge and injects it with `srcdoc`. The iframe sandbox is
  `allow-scripts allow-forms allow-modals allow-popups` — deliberately **no**
  `allow-same-origin`. Because the content is injected inline, a page that links
  **relative** assets will not resolve them.

### 3. Right Pane — 320px, collapsible to a ~56px rail

- **Header:** `PLAN TRACKER` badge + the active filename. Header actions, in order:
  collapse (`btnToggleRightSidebar`), audit sync, switch plan (`⇆`), create plan (`+`).
- **Progress card:** `Roadmap Completion` and the `X/Y (Z%)` ratio with a bar.
- **Task cards** (not flat text — no raw markdown syntax is rendered):
  - Completed: solid green check, title struck through, and **both** an inline `Diff`
    and inline `Rollback` button.
  - In progress: amber pulsing indicator and an inline `Stop` button.
  - Pending: hollow circle and an inline `Execute` play button.
  - Metadata: a domain pill (`[UI]`) only when the task genuinely carries `is_ui`, plus
    file chips (`📄 auth.py`) under the title.
  - Clicking a card expands its accordion drawer with nested detail bullets.
- **Global State Summary card** sits above the sections whenever the plan carries a
  `## 🌍 Global State Summary` heading. That block is standing context, not a section, so
  the parser captures it into `plan.json.state_summary` and the tree renders it as a card
  instead of an empty section header.
- **Sub-steps** (`sub_steps[]`) render inside their parent card's drawer, under a group
  progress bar reading `done/total`, one row each with a scaled-down status ring. They are
  part of the parent card and never tasks of their own: no `task-N` id, no metric slot, no
  effect on `Execute Next Step`.
- Rollback asks for confirmation through the rollback modal; a task with no recorded
  edits reports "This task has recorded no file edits."

### 4. Bottom Dock — 240px when the console is shown, resizable (floor 120px)

Anchored flush below the centre pane — no longer a floating island.
Pane order is load-bearing:

```
#dockResizeHandle  ->  #actionControlPanelContainer  ->  #dockLogs
                         ├── #actionPanelLockOverlay      ├── .console-bar (title + actions)
                         └── #actionDockCard              └── #consoleStream
                             └── #actionDrawerPanel  (the open action's parameter form;
                                                      the dock raises its own floor while
                                                      it is open)
```

- **Action entry point:** the command palette (Ctrl/Cmd+K) or the top bar's `Commands`
  button. The six-button intent strip was a permanent ~90px tax on the dock; it is gone
  (Wave 4) and the palette opens the **command drawer** instead — a pane of the dock, not an
  overlay. It is `display:none` at rest; `.bottom-dock.drawer-open` reveals it.
- **No status row.** Wave 7 retired the read-only `Active Plan:` pill and the dock progress
  pill (the top bar already showed the plan) and moved the completion figure and `Stop Task`
  into the top status bar; the console toggle is at the foot of the right rail. With no drawer
  open the dock head is `display:none`, so at idle the dock is just its top border — unless the
  plan-lock notice is up, which is the one thing that still gives the head height.
- The dock is a CSS **container** (`container-type: inline-size`); the command drawer reflows
  by container width (`@container (max-width: 860px)` drops the subtitle, `620px` stacks the
  body). This is why collapsing a side pane must produce a real width change.

### 5. Modals

`position:absolute` children of `<body>` that already cover the window — **do not
relocate them**. They cover: the plan switch/create dialogs, the workspace file browser,
the codebase/plan audit, and the rollback confirmation. The action parameter form is **not**
here any more — it is the docked command drawer inside `#bottomDock` (§6.4), multimodal
UI-vision section for `is_ui` tasks and all.

---

## 🛡️ 7. Token Efficiency & Context Protection Rules

1. **Middle Truncation on Shell Output:**
   In `tools/shell_tools.py`, outputs exceeding `MAX_SHELL_OUTPUT_CHARS = 2400` are
   truncated with head/tail preserved:
   ```
   [First 1200 chars] ... [Omitted X characters] ... [Last 1200 chars]
   ```
2. **Middle Truncation on File Reads:**
   In `tools/file_ops.py`, files exceeding `MAX_FILE_READ_CHARS = 6000` are truncated
   similarly (except `.md` plan files).
3. **Environment Values Never Leave Python:**
   `tools/file_ops.py::read_environment_variables()` returns names, a set/unset flag
   and a character count only — never a value. Max `MAX_ENVIRONMENT_VARIABLES = 60`.
4. **Stateless Session Guarantee:**
   No chat history is accumulated across turns. Each prompt initiates a clean session
   anchored exclusively to the plan file and `plan.json`.
5. **Zero-Token Plan Parsing:**
   The plan tree is produced by `tools/plan_parser.py` (markdown-it-py AST + regex),
   not by an LLM call.

---

## 🐛 8. Bugs Resolved (Do Not Reintroduce)

1. **`parse_plan_tree` IndexError:**
   - *Cause:* the plan parser had flawed operator precedence between a `steps` check and
     bullet detection, so an early bullet with an empty `steps` list threw `IndexError`.
     (This logic now lives in `tools/plan_parser.py`; the bug was originally in the
     monolithic `tools/file_tools.py`.)
   - *Fix:* rewritten with explicit grouping, header task detection, and graceful
     fallback step creation.
2. **Missing Plan Silent Fail:**
   - *Cause:* if the plan file didn't exist, the workflow skipped initialization and
     appended directly to an empty file.
   - *Fix:* the workflow checks plan existence and scaffolds a full roadmap first.
3. **Folder Dialog Reopening Loop:**
   - *Cause:* `pywebview.create_file_dialog` returning empty triggered an unconditional
     `tkinter` fallback.
   - *Fix:* explicit cancellation detection returns `{"cancelled": True}` immediately.
4. **Window Renders But Ignores Every Click (frontend):**
   - *Cause:* the frontend was split into per-file `js/*.js` / `styles.css` subresources,
     and pywebview serves the UI from a localhost HTTP server. WebView2 intermittently
     drops an individual fetch; a missing module threw while wiring listeners, which
     silently aborted the rest and left the window rendered but inert (and
     `debug=False` hides the console).
   - *Fix:* every module and stylesheet is inlined into `ui/index.html` by
     `tools/build_ui_bundle.py`, so startup performs no subresource fetch. A head
     watchdog names any missing entry points, and `tests/ui_startup_contract.js` pins
     the bundles to their sources. Validated over repeated real launches.
   - *Do not* re-add `<script src>` / `<link rel="stylesheet">` tags for local files,
     and always re-run the build script after editing `ui/js/` or `ui/css/`.
5. **UI Template Selected For Backend Tasks:**
   - *Cause:* `templates.select` matched its UI keywords as bare substrings, so any
     title merely containing the letters won: `"Build a weather API"` became an HTML
     dashboard because of the `ui` in `build`, and `"Code review"` because of the
     `view` in `review`.
   - *Fix:* keywords are matched on word boundaries (`\b(?:ui|frontend|interface|view)s?\b`),
     so the weather template wins for those titles while plurals such as
     `"Dashboard views"` still match.
6. **Non-Plan Markdown Offered As A Switchable Plan:**
   - *Cause:* `list_plan_files` returned every root `.md`, so a tracker or README was
     listed as a plan; selecting one made the Dual-Sync engine compile a non-plan
     document, surfacing as a blank plan tree with every action locked.
   - *Fix:* `NON_PLAN_MD_FILES` (progress / readme / changelog, case-insensitive) is
     skipped. The files stay visible in the file explorer.
7. **Offline Fallback Disguised A Dead Bridge:**
   - *Cause:* `initFallbackMode` seeded a fake project whenever `window.pywebview` was
     absent, so a failed handshake produced a fully populated window showing a project
     the user does not have.
   - *Fix:* the demo data is opt-in (`?demo=1`) for layout previews only; the default
     path reports a visible "bridge unavailable" failure instead. The 400 ms fallback
     timer is also cancelled when `pywebviewready` arrives first.
8. **Collapsing A Side Pane Froze The Dock's Layout:**
   - *Cause:* `.bottom-dock` is `container-type: inline-size`, and the action labels are
     hidden by a container query. Hiding a pane with `display:none` removes it from the
     layout without changing any width the dock can observe, so the centre content
     reflowed while the dock kept its old label/icon arrangement — and the toolbar
     buttons stayed visually pinned in place.
   - *Fix:* collapse changes the pane's **width** to a rail token
     (`--sidebar-left-rail`, `--sidebar-right-rail`) and flips a class, never
     `display:none`. The dock then reflows correctly.
9. **Indented Checkboxes Became Extra Milestones:**
   - *Cause:* the milestone scan stops at the first `list_item_close`, so a task's first
     nested checkbox was absorbed into `details` (with its literal `[ ] ` prefix) and every
     later one was hardened into a sibling top-level task. A 16-task roadmap parsed as 21
     tasks, and the progress meter counted sub-steps as milestones.
   - *Fix:* `_collect_sub_steps` in `tools/plan_parser.py` runs as a **separate token pass**
     (the milestone scan itself is unchanged) and folds the indented checkboxes into the
     parent's `sub_steps[]`, flattened at any indent depth. The claimed items are skipped
     by index, not by title, so two identically titled tasks cannot collide. The compiler
     re-indents them, and the round trip is byte-stable.
10. **`[UI]` Tag Matched Anywhere In A Title:**
   - *Cause:* `is_ui` was a bare substring test over the whole title, so a task that merely
     *mentioned* the tag (e.g. ``Wire Laya zero-token `[UI]` task tagging into …``) was
     stripped mid-sentence and left an orphaned empty code span, plus a pill it had not
     earned. Same bug class as #5, in the parser instead of the template selector.
   - *Fix:* `_split_ui_tag` recognises `[UI]` only as a standalone token, which is exactly
     the form the compiler writes (`- [ ] [UI] Title`). A backticked or mid-sentence `[UI]`
     is left alone. *Consequence:* a plan whose only `[UI]` mention is inside backticks now
     shows **no** domain pill — that is correct, but it is a visible change.
11. **`## 🌍 Global State Summary` Was Erased On Save:**
   - *Cause:* the heading parsed as a section with zero tasks, its prose bullets were
     dropped from `plan.json`, and the compiler re-emitted nothing — so the first in-app
     save deleted the plan's standing context.
   - *Fix:* `_collect_state_summary` captures the block into `state_summary` and the
     heading is excluded from `sections[]`; `compile_plan_json_to_markdown` re-emits it
     directly under the H1. Any in-app write (Save, executing a task, rollback, plan
     switch) now preserves it.
12. **An Orphan `</div>` Took The Whole Right Pane Off Screen:**
    - *Cause:* moving the action form out of the floating overlay and into the dock was done
      by a script that sliced the old markup up to a `<div class="modal-footer">` marker.
      The slice swallowed the `</div>` that closed `.modal-body`, so the dock ended one
      nesting level early and every later sibling — the plan sidebar among them — was hoisted
      out of the rendered tree. Because it is a parse fault and not a logic fault, it
      presented **even with the drawer closed**, and it looked intermittent only because the
      hoisted node sometimes still landed on screen.
    - *Fix:* the extra closer is gone and the markup balances (`<div>` count == `</div>`
      count across the region between the two bundle regions, 201 and 201 today). Re-run that
      audit after any hand edit to `ui/index.html`; do not hand-edit inside the generated
      bundle regions themselves.
13. **`.drawer-action-btn` Lost To A Bundle-Order Tie:**
    - *Cause:* `tools/build_ui_bundle.py` inlines `actions.css` **before** `modals.css`, and
      `.drawer-action-btn` / `.btn-dialog-primary` have equal specificity (0,1,0). The later
      rule won, so the drawer's `height: 24px; padding: 0 12px; font-size: 11.5px` was
      overridden by `flex: 1; padding: 10px 18px; font-size: 13px` and a 13px label spilled
      out of a 24px button.
    - *Fix:* the drawer's button rules are scoped `.action-drawer-panel .drawer-action-btn`
      (0,2,0), which beats the modal rule whatever the order. Do not unscope them, and do not
      reorder the stylesheet list in `STYLESHEETS` to "fix" this.
14. **The Drawer's Commit Row Belonged At The Bottom:**
    - *Cause:* Confirm/Cancel were placed in `.drawer-head`, so the form read as sitting
      underneath the buttons instead of above them.
    - *Fix:* a `.drawer-foot` row closes `#actionDrawerPanel` and only the `×`
      (`#btnCloseParamModal`) stays in the head, as a pane's close affordance. Heights:
      drawer 184px, dock floor 424px — keep the invariant *dock `min-height` = its 240px
      default + the drawer's height*, or the console shrinks when a drawer opens.

---

## 📋 9. Current Status of `PLAN.md`

Active plan: `my_project_workspace/PLAN.md`, titled **"DeepAgents Studio Architecture
Upgrade Roadmap"** — 4 sections, **17 milestones, 4 complete**, plus a **Global State
Summary** (6 bullets) and **45 nested sub-steps** across 16 of the 17 milestones. The user
edits this file by hand; it is *not* rewritten until the first in-app Save.

`State` below is the parent checkbox's own mark. A milestone whose parent box is `[ ]`
reports as pending even where some of its sub-steps are already ticked — that is the plan's
own state, not a parser artifact.

| # | Section | Milestone | Sub-steps | State |
| --- | --- | --- | --- | --- |
| 1 | Data Layer, Parser & Context | Upgrade AST parser (`tools/plan_parser.py`) for advanced schema extraction | 2/3 | pending |
| 2 | | Implement Line-Anchored Context Slicer in `context.py` | 0/1 | pending |
| 3 | | Update Architect directives in `agents/architect.py` | 0/2 | pending |
| 4 | | Implement AST Normalization Gate backend | 0/2 | pending |
| 5 | Laya System 1 | Build the Laya decision engine as `agents/laya.py` | 4/4 | **done** |
| 6 | | Replace the substring Gatekeeper gate in `actions_impl.py` | 3/3 | **done** |
| 7 | | Route Coder deep-vs-standard selection through `laya.classify` | 6/6 | **done** |
| 8 | | Wire zero-token `[UI]` tagging into `tools/plan_parser.py` | 0/2 | pending |
| 9 | | Laya input sufficiency gate for `🐛 Fix Bug` | 0/2 | pending |
| 10 | | Pre-flight plan drift probability check | 0/0 | pending |
| 11 | | Laya ModernBERT backend behind the same `classify` seam | 1/3 | pending |
| 12 | Center-Stage & Workbench | Transform center stage into Dual-View Workbench | 0/3 | pending |
| 13 | | Redesign Right Panel into a 60px fixed Utility Rail | 0/2 | pending |
| 14 | | Remove code-level diff inspection views entirely | 0/4 | pending |
| 15 | Connected Dock & Console | Connected Tab Action Drawer in `#bottomDock` | 4/4 | **done** |
| 16 | | Hybrid Console System | 0/2 | pending |
| 17 | | Apply dark IDE UI polish | 1/2 | pending |

Notes for whoever continues:

- Milestone 15 (the Connected Tab Action Drawer) landed in the session this handoff
  documents; milestones 5-7 are the Laya System 1 work recorded in §2. Beyond those four,
  every remaining roadmap item is still open, including all of §3 (the dual-view workbench,
  the fixed 60px rail, diff removal) and §4-2 (the hybrid console).
- `tests/_plan_prebak.md` is the **previous** plan ("FastAPI Cloud Weather Microservice",
  10 tasks). It is untracked and, since `plan.json` was rehydrated, it is the last copy —
  do not delete it without asking.
- `my_project_workspace/` still holds the generated artifacts of the earlier weather plan
  (`main.py`, `test_main.py`, `weather_api.py`, `test_weather_api.py`, `ui_view.html`,
  `test_ui_view.py`, `PROGRESS.md`) and `.deepagents_backups/` with keys `bugfix`,
  `custom`, `task-1` … `task-7`. They are now unrelated to the active plan.
- The workspace is gitignored, so `list_directory`/`find_path` do not show it — use the
  shell. If a plan edit appears to do nothing, remember `load_plan_state` rehydrates only
  when `md_mtime > json_mtime` (strict); force it with
  `load_plan_state(force_sync=True)`.

---

## 🎯 10. How Incoming Agents Should Continue

1. **To advance the current roadmap:** click `Execute Next Step` in the dock toolbar. The
   next pending target is `task-1` — "Upgrade AST parser (`tools/plan_parser.py`) for
   advanced schema extraction", whose one open sub-step is the inline `🟢 Behavioral Log:`
   extraction. Task-keyed rollback/diff snapshots use the `task-N` ids, which for this plan
   run `task-1` … `task-17`; sub-steps have ids like `task-1-sub-2` but own no snapshots.
2. **To pick up the user's backlog:** read `my_project_workspace/PLAN.md` first — it is the
   roadmap, not a description of it. Its `## 🌍 Global State Summary` is the standing
   context; keep those bullets true when a milestone lands.
3. **To inspect or modify agent prompts:**
   - Open the desktop app (`app.py`), click any agent in the left pane, edit the
     prompt, and click **`Save Custom Directives`**. The core rules tier is read-only.
4. **To switch or create a new project:**
   - Click the workspace card at the top of the left pane to select a new directory.
   - Click the `+` button in the right pane's header to scaffold a fresh `.md` plan. A new
     plan is seeded with a Global State Summary by `orchestration/plan_session.py`.
5. **Before claiming any change is done:**
   - Run all the gates in §3, then actually launch the app. A window that renders but
     ignores input is the historical failure mode here, and it is intermittent — launch
     several times before trusting a UI change.
6. **Uncommitted work as of this handoff:** nothing — the working tree is clean at the
   commit that landed the Connected Tab Action Drawer. The roadmap's next item is §3-1, the
   Dual-View Workbench (`ui/js/workbench.js`); §3-2 (the 60px fixed rail) and §3-3
   (retiring `ui/js/diff-pane.js` from `STYLESHEETS`/`MODULES`, the header watchdog and
   `tests/ui_startup_contract.js`) follow, then §4-2 (the hybrid console).
