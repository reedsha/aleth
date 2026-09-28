# 🚀 DEEPAGENTS STUDIO: MASTER AGENT HANDOFF & ARCHITECTURE SPECIFICATION

> **CRITICAL DIRECTIVE FOR ANY INCOMING AGENT:**
> Read this document completely before modifying or executing any code. This single
> document contains the full architecture, execution flow, component map, strict
> constraints, and current state. You will not need to ask redundant questions or
> hallucinate context.
>
> Everything in §2, §5, §6 and §9 was verified against the working tree at the time of
> the last edit. Where a claim can drift (file inventories, current plan state), it is
> stated as of that edit rather than as a permanent fact. §9 in particular reflects
> `PLAN.md` as of the last rewrite — **re-read `PLAN.md` itself before trusting it**.

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
3. Provides real-time visibility through a **four-pane IDE layout**: a tabbed left
   sidebar (Agents / Plans / Files / Env), a centre **Dual-View Workbench** that hosts the
   plan document *and* the plan tracker (bento header + roadmap cards), the polymorphic
   per-action **result view**, and a resizable bottom dock holding the **command drawer**
   and the run-scoped console — all driven by a **zero-token native markdown parser**
   (`tools/plan_parser.py`) rather than an LLM round-trip.

---

## 🗂️ 2. Repository & File Structure

```
deepagents/
├── app.py                     # pywebview host: window, BridgeAPI (the JS-facing API), worker thread
├── main.py                    # Entrypoint: desktop UI by default, headless with --cli
├── registry.py                # AgentRegistry facade (~158 lines) — delegates to orchestration/
├── env_boot.py                # Loads .env; a shadowed export is reported, not silently used
├── HANDOFF.md                 # Master handoff document (this file)
│
├── agents/                    # Agent definitions. These double as DATA FILES:
│   │                          #   the prompt editor rewrites them on disk at runtime.
│   ├── __init__.py            # Exports architect_agent, coder_deep, coder_standard
│   ├── architect.py           # software-architect: core prompt + custom directives
│   ├── coders.py              # coder-deep and coder-standard worker definitions
│   ├── laya.py                # System 1: the zero-token heuristic classifier (`classify`)
│   ├── laya_model.py          # System 1's env-gated ModernBERT backend (same seam)
│   └── model_routing.py       # route names (`openai:policy/x`) + architect_model()/coder_model()
│
├── orchestration/             # Extracted from the former monolithic registry.py
│   ├── __init__.py            # Public re-exports of the modules below
│   ├── agent_catalog.py       # Reflection over agents/ -> the catalogue the UI renders
│   ├── plan_session.py        # Roadmap scaffolding + reading the active plan
│   ├── plan_tagging.py        # The offline [UI] re-tagging pass (Laya), background thread
│   ├── prompt_editor.py       # Persists edited prompts back into the agents/ sources
│   ├── system2.py             # THE one place a model request is made (vision-capable)
│   └── workflow/
│       ├── __init__.py
│       ├── runner.py          # Intent resolution, dispatch, error handling
│       ├── context.py         # WorkflowContext + the Line-Anchored Context Slicer
│       ├── events.py          # THE single definition of the frontend event wire format
│       ├── reasoning.py       # The Architect's System 2 answer as streamed UI text
│       ├── ledger.py          # Living Behavioral Ledger: the 🟢 log line + state summary
│       ├── generation.py      # Plan slice -> one Coder deliverable (System 2 + fallback)
│       ├── actions_admin.py   # Administrative bypass: update_plan, analyze, recommend, normalize
│       ├── actions_impl.py    # Delegated implementation: fix_bug, next_step, custom
│       └── templates.py       # Deliverable templates (solution engine, ui_view, weather API)
│
├── tools/                     # Tooling package
│   ├── __init__.py            # Clean re-exports of the file tools
│   ├── file_ops.py            # @tool primitives, workspace listing, masked .env reader
│   ├── file_tools.py          # Compatibility facade that re-exports its siblings
│   ├── plan_parser.py         # Markdown <-> plan dict (markdown-it-py AST); sub-steps,
│   │                          #   Global State Summary, [UI] detection, deliverables
│   ├── plan_state.py          # Dual-sync: plan.json <-> the active markdown plan;
│   │                          #   raises PlanWriteError when a write fails
│   ├── task_tags.py           # The tag vocabulary (UI_TAG) shared by parser & UI
│   ├── recovery.py            # Backups, codebase/plan audit, sync resolution, rollback,
│   │                          #   and the plan-revision snapshot/revert
│   ├── code_metrics.py        # Static source metrics (complexity, security) for Analyze
│   ├── test_runner.py         # Runs the test a task wrote; the result view's real verdict
│   ├── git_status.py          # VCS status for the tree (snapshot fallback outside a repo)
│   ├── workspace.py           # PROJECT_DIR (sandbox) + PLAN_DIR (repo root) pointers
│   ├── shell_tools.py         # Full shell (Coders) + restricted shell (Architect)
│   ├── settings.py            # Provider endpoint + model-route settings (on disk)
│   ├── laya_bench.py          # Dev-only: Laya checkpoint I/O benchmark
│   ├── system2_cost.py        # Dev-only: measures what a prompt costs at the real API
│   └── build_ui_bundle.py     # Inlines ui/js/*.js + ui/css/*.css into ui/index.html
│
├── ui/
│   ├── index.html             # Layout markup + two generated bundle regions (style, then UI)
│   ├── css/                   # 19 stylesheets (source of truth), inlined in order
│   │   ├── base.css                     # Design tokens (:root) + reset & base styles
│   │   ├── sidebar.css                  # Left pane: agent/plan/file/env tabs & workspace card
│   │   ├── stage.css                    # Centre stage & the execution cards
│   │   ├── actions.css                  # The docked command drawer & its parameter form
│   │   ├── plan-tree.css                # Workbench roadmap: bento header, cards, progress bands
│   │   ├── modals.css                   # Modal base + the workspace-files dialog
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
├── tests/                     # stdlib unittest (pytest is NOT installed) + one Node harness
│   ├── test_characterization.py  # Behavioral-equivalence harness (the bulk of the suite)
│   ├── test_laya.py              # System 1 heuristic classifier
│   ├── test_laya_model.py        # The env-gated ModernBERT backend
│   ├── test_generation.py        # Plan slice -> deliverable; the System 2 seam + vision input
│   ├── test_system2.py           # The provider seam (config, routing, failure -> None)
│   ├── test_plan_tagging.py      # The offline [UI] re-tagging pass
│   ├── test_context_slicer.py    # Line-Anchored Context Slicing
│   ├── test_architect_reasoning.py / test_ledger.py / test_code_metrics.py
│   ├── test_settings.py / test_task_tags.py / test_test_runner.py
│   ├── test_git_status.py / test_normalization_gate.py / test_env_boot.py
│   ├── _plan_prebak.md           # The PREVIOUS plan's last copy — do not delete
│   └── ui_startup_contract.js    # Node harness: 53 checks pinning bundle + markup contracts
│
├── PLAN.md / plan.json        # THE ACTIVE PLAN lives in the PLAN DIR == this repo root
│                              #   (PLAN.md is tracked; plan.json is gitignored)
├── 1ST/2ND/3RD_POLISHING.md   # The three polishing plans + their progress logs. Listed in
│                              #   NON_PLAN_MD_FILES, so the switcher never offers them.
│
├── my_project_workspace/      # PROJECT_DIR: the gitignored sandbox for GENERATED code
│   ├── main.py / weather_api.py / ui_view.html   # Legacy artifacts of the earlier weather plan
│   ├── test_*.py / PROGRESS.md
│   └── .deepagents_backups/   # Per-task deliverable snapshots (rollback source),
│                              #   plus plan_revision/ — the before-image the result view's
│                              #   "Revert Changes" restores from
│
└── venv/                      # Python 3.12 Virtual Environment
```

`main.py` and `app.py` both import the module named `registry`, which is why the
package that registry.py delegates to is called `orchestration/` — a package named
`registry` would shadow the module.

**Two directories, do not conflate them.** `tools/workspace.py` owns both pointers:
`PROJECT_DIR` = `my_project_workspace/` (the gitignored **sandbox** that generated code,
deliverables and backups land in), and `PLAN_DIR` = the **repo root** (where `PLAN.md` and
its `plan.json` twin live, so the roadmap is version-controlled). A plan edit therefore
changes a tracked file in the repo root, while generated code lands in the sandbox.

---

## ⚙️ 3. Environment & Quick Launch Commands

All Python dependencies are installed in the local virtual environment (`.\venv`).
There is no `package.json`; Node is used only to run the UI contract harness.

```sh
# 1. Launch the native desktop application (pywebview):
./venv/Scripts/python.exe app.py

# 1b. Same, with WebView2 DevTools open (diagnose a window that renders but
#     ignores input: the console and network log show which request or script failed):
./venv/Scripts/python.exe app.py --debug

# 2. Run headless multi-agent verification (CLI mode):
./venv/Scripts/python.exe main.py --cli "Implement weather service endpoints"

# 3. Test imports & syntax across the project:
./venv/Scripts/python.exe -c "import app, registry, tools, agents; print('All systems operational!')"

# 4. Verify the plan tree native parser (zero LLM tokens):
./venv/Scripts/python.exe -c "from tools.file_tools import read_file, parse_plan_tree; print(parse_plan_tree(read_file.invoke({'filename': 'PLAN.md'})))"
```

The **shell is `sh`** (Git Bash / the agent's terminal), so use forward slashes:
`./venv/Scripts/python.exe`. A PowerShell session would use `.\venv\Scripts\python.exe`;
the two are otherwise identical.

### The verification gates (run all of them after any change)

```sh
# A. Frontend contract: bundle freshness, module order, id inventory, markup contracts.
#    53 checks. Also fails if ui/index.html has drifted from ui/js + ui/css.
node tests/ui_startup_contract.js

# B. Backend suite. stdlib unittest — pytest is NOT installed. 408 tests today.
./venv/Scripts/python.exe -m unittest discover -s tests -t .

# C. Just the bundle-staleness check, without the rest of the harness:
./venv/Scripts/python.exe tools/build_ui_bundle.py --check

# D. Markup balance audit of the hand-authored body region. The bundles are inlined, so a
#    stray </div> here ends a container early and hoists every later sibling out of the
#    rendered tree — the plan workbench first among them. The two counts must be equal
#    (230 and 230 today).
./venv/Scripts/python.exe -c "import pathlib; t=pathlib.Path('ui/index.html').read_text(encoding='utf-8'); r=t[t.index('END STYLE BUNDLE'):t.index('BEGIN UI BUNDLE')]; print('<div', r.count('<div'), '</div>', r.count('</div>'))"
```

Any `ui/js` or `ui/css` edit **requires** gate A first (after the build script) — it is
the only thing that proves the inlined bundle matches its sources.

Expected non-failures in the output, none of which is a test failure:
`[DualSync] Error reading plan.json: ...` (a workspace with no machine state yet, **and**
the `test_a_corrupt_plan_json_is_reported_not_hidden` case), `[BridgeAPI] Detached console
failed: no gui`, and the two `[System2]` lines from the mocked-provider tests.

### Frontend change protocol (hard rules)

`ui/js/*.js` and `ui/css/*.css` are the source of truth, but `ui/index.html` embeds
generated copies of them so that startup performs no per-file HTTP request. WebView2
fails individual subresource fetches intermittently, which can leave the window
rendered but completely inert.

Therefore, after editing any `ui/js` or `ui/css` file:

1. Re-run `./venv/Scripts/python.exe tools/build_ui_bundle.py`.
2. Re-run `node tests/ui_startup_contract.js` — it reports `stale` if you forget.

Additional invariants:

- **Never** split the frontend into multiple `<link>`/`<script src>` tags.
- New JS modules append **before** `wire.js` in `JS_MODULES`; new stylesheets append
  **last** in `CSS_MODULES`. Do not reorder existing entries.
- A new entry point (any function the wiring calls) must be added to the head
  watchdog's `required` list near the top of `ui/index.html`. It currently names 35.
- Hand edits to the body markup of `ui/index.html` are allowed; hand edits **inside**
  the two marked regions are not. Run gate D after any markup edit.
- A failure in a startup step is reported as a visible banner — that is intended
  behaviour, not a bug.

> Because the bundles are inlined, the hand-authored body markup of `ui/index.html`
> begins **after** the UI bundle region (currently closing around line 12660), not near
> the top. Only the two marked regions are generated; the markup between and after them
> is hand-edited.

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
- **Standardized logging protocol (Living Behavioral Ledger):** completion is recorded on
  the task itself, **never** as a new `##` section. The Coder's own prompt
  (`agents/coders.py`) asks for **one** `🟢 Behavioral Log:` line beneath the task's
  `- [x]` checkbox:

  ```markdown
  - [x] Build the forecast endpoint
    - 🟢 Behavioral Log: added app/api.py with the forecast route and a paired test.
  ```

  The workflow adds the machine-owned lines beside it — `Deliverables: ``a.py``, ``test_a.py`` `
  on completion, `Verification failed: …` on a failed gate — and writes the task's
  `files[]` directly. The parser does **not** infer `files[]` from prose: a
  `Files Created/Modified:` line is kept verbatim as a `details` entry only (pinned by
  `test_deliverables_are_kept_as_raw_detail_text`).

---

## 🔄 5. End-to-End Execution Flow

The old free-text input bar and its "Are you sure…?" confirmation modal are gone.
Execution is intent-first: the **command palette** (Ctrl/Cmd+K, or the top bar's
`Commands` button) is the action entry point — the six-button dock strip was removed in
Wave 4. Choosing an intent opens the **command drawer**, a pane of the dock, to collect
that action's parameters (target task, vision mockup, bug evidence, custom directive).
Confirming the drawer is the confirmation step.

```
[Command Palette]  (Ctrl/Cmd+K — the six intents the dock strip used to hold)
      │  fix_bug │ next_step │ update_plan │ analyze │ recommend │ custom
      ▼
[Command Drawer]  (openActionDrawer(actionType, extraParams)) — a pane of #bottomDock
      │  User reviews the targeted task / supplies the directive, then confirms
      ▼
[BridgeAPI.start_execution(user_message, action_type, action_params)]  (app.py)
      │  SERVER-SIDE RUN LOCK: refuses while a run is live (the client lock can be
      │  defeated by a reload). Otherwise spawns a daemon worker thread; emit_event ->
      │  window.evaluate_js
      ▼
[AgentRegistry.run_agent_workflow]  (registry.py -> orchestration/workflow/runner.py)
      │
      ├── resolve_intent(): an explicit action_type is never overridden; only
      │      "custom" is auto-detected from an [ACTION: ...] tag in the message.
      ├── load_plan_state() + get_active_plan_filename()
      ├── emit "workflow_started", then "architect_spawn"
      │
      └── Dispatch:
             update_plan -> actions_admin.update_plan_action    (no Coder spawn)
             analyze     -> actions_admin.analyze_action        (no Coder spawn)
             recommend   -> actions_admin.recommend_action      (no Coder spawn)
             normalize   -> actions_admin.normalize_plan_action (no Coder spawn)
             fix_bug     -> actions_impl.fix_bug_action         (snapshot key "bugfix")
             next_step   -> actions_impl.next_step_action       (snapshot key = task id)
             custom      -> actions_impl.custom_action          (snapshot key "custom")
```

Two other runs do not come from the palette but take the same lock and Stop:
`normalize_plan` (the Normalization Gate's "Format via Architect") and
`retag_plan_with_laya` (the offline `[UI]` re-tagging pass — a background thread that
streams `laya_tagging_started` / `_progress` / `_done`). The Retry buttons call
`retry_execution`, which is `start_execution` under the same lock.

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
6. emit "workflow_complete" -> the console settles, Stop hides, and the action's own
   **result view** mounts in the centre stage (unless the run was halted or errored — a
   halt keeps the transcript, a crash shows `Run Failed`). The Update Plan result offers a
   one-click **Revert Changes**, restoring the plan-revision snapshot taken before the
   write.
```

### The event wire format

Every payload reaches JavaScript through pywebview's `evaluate_js`, and the UI
switches on `type` in `ui/js/agent-events.js`. `orchestration/workflow/events.py` is
the single definition of this format — key order is part of the contract, because
payloads are JSON-serialised in insertion order.

| `type` | Meaning |
| --- | --- |
| `workflow_started` | A run began; carries the plan file and resolved intent |
| `architect_spawn` | The Architect card is created |
| `log` | One streamed line of agent narration (`log_type` drives styling) |
| `tool_call` / `tool_result` | A tool invocation and its paired outcome; animated as one unit |
| `delegation` | The Architect has chosen a Coder and a target task |
| `coder_spawn` | The Coder's card is created |
| `coder_summary` | The Coder's deliverable summary |
| `architect_summary` | The Architect's verification verdict and next steps |
| `plan_updated` | Plan data changed; the payload is the plan + its parsed tree |
| `agent_error` | A run failed |
| `workflow_stopped` | A stop request was honoured |
| `workflow_complete` | The run finished (`status` is `finished` or `error`) |
| `workspace_changed` | The active project directory changed |
| `agents_updated` | The agent catalogue changed (e.g. a prompt was saved) |
| `laya_tagging_started` / `_progress` / `_done` | The offline `[UI]` re-tagging pass; `_done` carries `changed` + `engine` |

> Payloads carry only what the UI reads. `can_retry`, the spawn-event `role`, and
> `workflow_started.message` were removed (audit L2); `delegation.task` stays because the
> console now renders the delegated task text.
>
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
    ├── aside.sidebar#leftSidebar          (250px, collapses to a rail; 4 tabs)
    ├── main.main-stage
    │   ├── header.top-status-bar          (48px: Ready · active plan · completion · Stop · Commands · Preview)
    │   └── section.workspace-view#chatWorkspaceView   (column)
    │       ├── .stage-center              (workbench + execution stage + result view + preview)
    │       └── section.bottom-dock#bottomDock   (command drawer + console)
    └── aside.right-plan-sidebar#rightPlanSidebar   (fixed rail; collapses, never drags)
```

### 1. Left Pane — 250px, collapsible to a ~52px rail

- **Brand row:** `DEEPAGENTS` + `STUDIO` badge + the collapse chevron (`btnToggleLeftSidebar`).
- **Tabs:** Agents · Plans · Files · Env (`#tabSidebarAgents` / `Plans` / `Files` / `Env`,
  wired in `ui/js/wire.js` → `setSidebarTab`). Each section below is one tab panel; the
  Plans tab is also the single entry point for plan selection (§6.3).
- **Workspace card:** shows the active folder (`my_project_workspace`). Clicking it
  opens the native OS folder dialog; the `Change` button does the same. Dialog cleanly
  exits on the first click of `Cancel` or `X` without reopening.
- **MAIN AGENTS / CODER AGENTS:** lists with count pills. Clicking an agent switches
  the stage to the prompt editor view.
- **WORKSPACE FILES (Files tab):** the same listing the Files modal reads, grouped into
  folders in the frontend. Click a folder row to expand/collapse. Each leaf carries a
  colour-coded VCS letter (`.tree-vcs`, amber `M` changed / green `U` new) sourced from
  `tools/git_status.py` — git when the workspace is its own work tree, the
  `.deepagents_backups/` snapshots otherwise. The file sizes are gone.
- **ENVIRONMENT (Env tab):** one row per variable in the app's own `.env` — **name, set/unset
  flag and character count only; the value never crosses the bridge**. The eye icon
  toggles a second line with that metadata and the note "value stays in the backend".
  An absent `.env` is not an error; the tab's gear opens Settings.
- **Prompt editor view (full-stage):** back button, tiered editor with the core rules
  rendered read-only, tool pills, character counter, `Reset`, and
  **`Save Custom Directives`** (persists into the `agents/*.py` source and reloads in
  memory with zero app restarts).

Both panes collapse by changing the `<aside>` width (a class + the `--sidebar-*-rail`
tokens), **never** with `display:none` — see bug 8 in §8.

### 2. Centre Pane — the Plan Workbench (and the plan tracker)

`#chatWorkspaceView` → `.stage-center` → `.plan-workbench#emptyStateContainer`. The centre
now does three jobs; **the roadmap — tracker included — lives here**, not in the right rail:

- **Bar:** breadcrumbs (`workspace › PLAN.md` — `#crumbWorkspace` / `#crumbPlanFile`), a
  `● unsaved` marker (`#crumbDirty`), the `[ 🌳 Tree View | 📄 Raw MD ]` toggle
  (`#btnWorkbenchTreeView` / `#btnWorkbenchRawMd`), and `Edit` / `Save` / `Discard`.
  `Save` writes the plan and re-syncs `plan.json`.
- **Tree view** (`#workbenchTreeView`, the default).
- **Bento header** — a fixed-height grid so switching plans cannot make the roadmap below
  it jump: `#bentoSummaryTile` (left, the plan's standing context, expandable into the
  audit rail), `#bentoProgressTile` and `#bentoStatsTile` stacked in the right column. The
  progress tile carries `Roadmap Completion` and `#txtPlanProgressRatio`.
- **Roadmap cards** (`#planTreeContainer`): the three tiers — phase card → step card →
  sub-step row. The progress meter (`#planProgressBarFill` / `…Active` / `…Failed`) reads
  `plan.json`'s metrics. Card details are in §6.3's notes (they moved, they did not change).
- **Raw MD view:** the same document as an editable, gutter-numbered source
  (`#planDoc` / `#planEditorInput`, metadata in `#txtWorkbenchMeta`).
- **Execution stage** (`#executionStage`, hidden at idle): a 50/50 grid — Architect card
  left (blue accents, `#60a5fa`), Coder card right (slate accents, `#cbd5e1`), each with
  live monospaced logs, a status pill, a summary container, and a close button that
  unlocks on completion.
- **Result view** (`#resultView`): a full-stage overlay mounted by the terminal event and
  routed by `state.selectedAction` — the **only** place code-level diffs are shown.
- **Live preview** (`Preview` in the top status bar): a full-stage overlay inside
  `.stage-center`. Reads the workspace's generated interface through the bridge and injects
  it with `srcdoc`. The iframe sandbox is `allow-scripts allow-forms allow-modals
  allow-popups` — deliberately **no** `allow-same-origin`. Because the content is injected
  inline, a page that links **relative** assets will not resolve them.

### 3. Right Pane — the Plan Tracker rail (fixed width, no drag)

`#rightPlanSidebar` is a **fixed-width collapsible rail** — never drag-resizable (the
`#rightSidebarResizeHandle` seam and its listener were retired in Wave 3).

- **Header:** the active plan filename + actions, in order: collapse
  (`#btnToggleRightSidebar`), audit sync (`#btnAuditPlanSync`), create plan
  (`#btnCreatePlanModal`). There is **no** Switch Plan (⇆) button: plan selection has
  exactly one entry point — the **left sidebar's Plans tab** (`#tabSidebarPlans`, whose
  expand button is `#btnExpandSwitchPlan`).
- **Footer:** the console toggle (`#btnConsoleToggle`, a 28px `.rail-console-toggle`,
  Ctrl+`), pinned at the rail's foot in `.plan-sidebar-footer`; it stays visible in both
  the expanded and collapsed states.
- **The roadmap card notes below moved to the centre workbench (§6.2)** when the rail
  became fixed-width. They are recorded here because the *behaviour* is unchanged:
  - Completed: solid green check, title struck through, inline `Rollback` + `Edit`.
  - Failed: `Retry` (`Execute` re-run) + `Fix` + `Edit`; a `✕` ring.
  - Pending: hollow circle, inline `Execute` + `Edit`.
  - Running: amber pulsing indicator; while a run is live the **card of the task the run
    was launched against** hosts an inline **Stop** (it used to hang off an `in_progress`
    status the workflow never writes, so it could never appear).
  - Metadata: the task's **domain pill** (any tag — `[UI]`, `[FE]`, …, from `tag`), plus
    file chips (`📄 auth.py`) under the title.
  - Clicking a card expands its accordion drawer (nested detail bullets, behavioral log,
    sub-steps with a `done/total` bar). Sub-steps are part of the parent card, never tasks:
    no `task-N` id, no metric slot, no effect on `Execute Next Step`.
- The **Global State Summary** renders in the bento summary tile above the roadmap whenever
  the plan carries a `## 🌍 Global State Summary` heading — the parser captures it into
  `plan.json.state_summary`, so it is standing context, not a section.
- Rollback asks for confirmation through the rollback modal; a task with no recorded edits
  reports "This task has recorded no file edits."

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

Two kinds of overlay live at the `<body>` level — **do not relocate them**:

- **Full-window modals** (`position:absolute` children of `<body>` that cover the window):
  the workspace **file browser** and the **rollback confirmation**.
- **Right-rail slide-in `.side-panel`s** (same element ids, different layout):
  `#auditModalOverlay` (the audit report), the **plan switcher** and **create-plan**
  dialogs. Their open/close handlers only toggle `display`, so preserving the ids kept the
  wiring intact when the centred modals became rail panels.

The action parameter form is **not** here — it is the docked command drawer inside
`#bottomDock` (§6.4), multimodal UI-vision section for `[UI]` tasks and all.

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
   similarly — except `.md` plan files **and** `.json` (the plan's machine state).
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
      count across the region between the two bundle regions, **230 and 230** today). Re-run
      that audit after any hand edit to `ui/index.html`; do not hand-edit inside the generated
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
15. **The Architect Reported "Verified & Approved" Without Reading Its Own Check:**
    - *Cause:* `custom_action` ran `py_compile`, stored the output in `v_res`, and never
      inspected it — the verdict was a literal string. A custom directive that wrote broken
      code was reported as verified.
    - *Fix:* the verdict comes from `command_failed(v_res)`, mirroring `next_step_action`.
16. **A Failed Deliverable Write Was Narrated As Success:**
    - *Cause:* `tools/file_ops.py::write_file` returns an error **string** on failure rather
      than raising, and the workflow branches ignored the return value — so a failed write
      still emitted "Successfully patched" and `next_step` still marked the task completed.
    - *Fix:* one `_write_checked()` guard at all six call sites raises, so the runner's
      existing `agent_error` + terminal `workflow_complete` path aborts the action before a
      false verdict (or a completed mark) is recorded.
17. **A Plan "Saved" When It Was Not, And A Corrupt Plan Looked Empty:**
    - *Cause:* `save_plan_state` caught its write/compile failures, printed them, and still
      returned the dict — so every caller emitted `plan_updated` for state the disk never
      received. Separately, an unreadable `plan.json` silently degraded to an empty plan.
    - *Fix:* it raises `PlanWriteError` now, and a load remembers why it failed
      (`last_plan_load_error()` → `load_error` on the payload), which the UI reports.
18. **A Crashed Run Read As A Clean Finish:**
    - *Cause:* the runner emits `workflow_complete{status:"error"}` on any exception, but
      `finalizeWorkflow` treated every non-`stopped` status as success — "Ready" plus a
      "Task concluded successfully!" toast, and it even mounted a result view.
    - *Fix:* `status` now branches to **Run Failed** (red dot, error toast); only `finished`
      mounts the result view; and the client run lock is backed by a **server-side** refusal
      in `start_execution` — a reload mid-run can no longer defeat it, because the UI
      re-arms from `get_run_state()`.

---

## 📋 9. Current Status of `PLAN.md`

**The active plan is `PLAN.md` at the repo root** (`PLAN_DIR == PROJECT_ROOT`), titled
**"DeepAgents Studio Architecture Upgrade Roadmap"** — a *tracked* file, with its
`plan.json` twin beside it (gitignored). It is **not** in `my_project_workspace/`; that
directory is the sandbox for generated code (§2).

As of the last rewrite, **every task on the roadmap is complete — 17/17 (100%)**, across
four sections:

| Section | Tasks | State |
| --- | --- | --- |
| 1. Data Layer, Parser & Context Engine | 4 | all `[x]` |
| 2. Laya System 1 Integration | 7 | all `[x]` |
| 3. Center-Stage & Workbench Redesign | 3 | all `[x]` |
| 4. Connected Dock, Console & Visual Polish | 3 | all `[x]` |

> Re-read `PLAN.md` for the authoritative detail; its `## 🌍 Global State Summary` carries
> the settled decisions. This table is a snapshot, not a substitute.

Notes for whoever continues:

- The plan **is** written by the app now, not only by hand: `add_plan_task` (the one-click
  "Add to Plan"), the Update Plan admin bypass, rollback and a plan switch all reach
  `save_plan_state`. An automatic before-image of the last revision is kept in
  `my_project_workspace/.deepagents_backups/plan_revision/` — what the result view's
  **Revert Changes** restores.
- `tests/_plan_prebak.md` is the **previous** plan ("FastAPI Cloud Weather Microservice",
  10 tasks). It is untracked and, since `plan.json` was rehydrated, it is the last copy —
  do not delete it without asking.
- `my_project_workspace/` still holds the generated artifacts of the earlier weather plan
  (`main.py`, `test_main.py`, `weather_api.py`, `test_weather_api.py`, `ui_view.html`,
  `test_ui_view.py`, `PROGRESS.md`) and `.deepagents_backups/` with keys `bugfix`,
  `custom`, `task-1` … `task-7`. They are unrelated to the active plan.
- The workspace is gitignored, so `list_directory`/`find_path` do not show it — use the
  shell. If a plan edit appears to do nothing, remember `load_plan_state` rehydrates only
  when `md_mtime > json_mtime` (strict); force it with `load_plan_state(force_sync=True)`.
  A corrupt `plan.json` is now reported rather than silently loading as empty (§8 bug 17).

---

## 🎯 10. How Incoming Agents Should Continue

1. **The roadmap is complete (17/17).** `PLAN.md` at the repo root is the record. To start
   something new, add a milestone (Update Plan, or the palette's Custom action) or create a
   fresh plan (§10.4). Task-keyed rollback/diff snapshots use the `task-N` ids (`task-1` …
   `task-17` here); sub-steps have ids like `task-1-sub-2` and own no snapshots.
2. **To pick up the user's backlog:** read `PLAN.md` (repo root) first — it is the roadmap,
   not a description of it. Its `## 🌍 Global State Summary` is the standing context; keep
   those bullets true when a milestone lands.
3. **To inspect or modify agent prompts:**
   - Open the desktop app (`app.py`), click any agent in the left pane's **Agents** tab,
     edit the prompt, and click **`Save Custom Directives`**. The core rules tier is
     read-only.
4. **To switch or create a new project:**
   - Click the workspace card at the top of the left pane to select a new directory.
   - Plan selection is the left sidebar's **Plans** tab; the rail has no ⇄ button (it was
     removed in Wave 7).
   - Click the `+` in the **right rail's header** to scaffold a fresh `.md` plan. A new plan
     is seeded with a Global State Summary by `orchestration/plan_session.py`.
5. **Before claiming any change is done:**
   - Run all the gates in §3, then actually launch the app. A window that renders but
     ignores input is the historical failure mode here, and it is intermittent — launch
     several times before trusting a UI change.
6. **Uncommitted work as of this handoff:** nothing — the working tree is clean. Since the
   Connected Tab Action Drawer commit, the three polishing passes (`1ST`–`3RD_POLISHING.md`)
   and their audit follow-ups landed in layered commits: the bento workbench and command
   palette, the elastic console, the polymorphic result views, a real `add_plan_task` and a
   plan-revision revert, a server-authoritative run lock, image-vision input for `[UI]`
   tasks, and the pre-launch audit fixes (Go 15–19). The gates in §3 are green (408 tests,
   contract, `--check`, div balance). The roadmap's *own* plan file remains the source of
   truth for what is left — re-read it rather than trusting a list here.
