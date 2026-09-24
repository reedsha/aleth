# 🚀 DEEPAGENTS STUDIO: MASTER AGENT HANDOFF & ARCHITECTURE SPECIFICATION

> **CRITICAL DIRECTIVE FOR ANY INCOMING AGENT:**  
> Read this document completely before modifying or executing any code. This single document contains the full architecture, execution flow, component map, strict constraints, and current state. You will not need to ask redundant questions or hallucinate context.

---

## 📌 1. Executive Summary & Objective

**Project Name:** DeepAgents Desktop UI & Multi-Agent Orchestrator Studio  
**Primary Goal:** A high-performance, dark glassmorphic native desktop application built with **Python (`pywebview`)**, **HTML5/CSS3**, and **JavaScript** that coordinates multi-agent software engineering workflows anchored 100% in a dynamic markdown plan file (`PLAN.md`).

### The Problem It Solves:
1. Eliminates multi-turn LLM context window explosion by enforcing **stateless sessions per prompt**; project memory is stored exclusively on disk in the dynamically selected `.md` plan file.
2. Eliminates rogue architectural behavior by enforcing **mandatory delegation**: the Lead Architect plans, verifies, and delegates, but **NEVER writes application code**. Only Coder sub-agents write code and execute arbitrary shell commands.
3. Provides real-time visibility through a **3-column desktop UI** featuring live dual-agent streaming cards and a **zero-token native markdown tree tracker** in the right sidebar.

---

## 🗂️ 2. Repository & File Structure

```
c:\Users\muham\OneDrive\Desktop\deepagents\
├── app.py                     # PyWebView BridgeAPI & desktop GUI entrypoint
├── main.py                    # Multi-mode entrypoint (Desktop UI or headless CLI)
├── registry.py                # Dynamic Agent Registry, prompt hot-reloading & workflow runner
├── HANDOFF.md                 # Master handoff document (this file)
│
├── agents/                    # Agent definition modules
│   ├── __init__.py            # Agent package exports
│   ├── architect.py           # software-architect coordinator definition & prompt
│   └── coders.py              # coder-deep and coder-standard worker sub-agents
│
├── tools/                     # Tooling package
│   ├── __init__.py            # Clean exports of file and shell tools
│   ├── file_tools.py          # State-anchored file I/O & zero-token parse_plan_tree()
│   └── shell_tools.py         # Full shell (Coders) & restricted shell (Architect)
│
├── ui/                        # High-contrast, dark glassmorphic frontend
│   ├── index.html             # 3-column layout; ends with the inlined js/ bundle
│   ├── styles.css             # Glassmorphic styling, animations, fluid ambient glows
│   └── js/                    # Frontend modules (source of truth), inlined in order
│       ├── state.js           # Global `state` and `DOM` cache
│       ├── dom.js             # Element lookup + escapeHtml()
│       ├── bootstrap.js       # PyWebView handshake, offline fallback, hydration
│       ├── plan-tree.js       # Plan tree rendering, progress meter, step extraction
│       ├── actions.js         # Action parameter modal & confirmed task execution
│       ├── plan-modals.js     # Switch / create plan modals
│       ├── agents.js          # Sidebar agents & prompt editor
│       ├── workspace.js       # Files, audit and rollback modals
│       ├── agent-events.js    # Bridge event stream → agent cards
│       ├── visuals.js         # Toasts and the orb animation
│       └── wire.js            # DOM event wiring; must stay last
│
├── my_project_workspace/      # Default active project directory (PROJECT_DIR)
│   ├── PLAN.md                # Dynamic memory anchor & active roadmap (source of truth)
│   ├── main.py / weather_api.py # Generated production code
│   └── test_*.py              # Automated pytest suites
│
└── venv/                      # Python 3.12 Virtual Environment
```

---

## ⚙️ 3. Environment & Quick Launch Commands

All Python dependencies are installed in the local virtual environment (`.\venv`).

```powershell
# 1. Launch the Native Desktop Application (PyWebView):
.\venv\Scripts\python.exe app.py

# 2. Run Headless Multi-Agent Verification (CLI Mode):
.\venv\Scripts\python.exe main.py --cli "Implement weather service endpoints"

# 3. Test Imports & Syntax Across Entire Project:
.\venv\Scripts\python.exe -c "import app, registry, tools, agents; print('All systems operational!')"

# 4. Verify Plan Tree Native Parser (Zero LLM Tokens):
.\venv\Scripts\python.exe -c "from tools.file_tools import read_file, parse_plan_tree; print(parse_plan_tree(read_file.invoke({'filename': 'PLAN.md'})))"

# 5. Regenerate the inline frontend bundle (run after editing ui/js/*.js):
.\venv\Scripts\python.exe tools\build_ui_bundle.py
```

> **Frontend build step:** `ui/js/*.js` are the source of truth, but `ui/index.html`
> embeds a generated copy of them so that startup performs no per-module HTTP
> request. Editing a module without rerunning `tools/build_ui_bundle.py` leaves the
> app on stale code; `node tests/ui_startup_contract.js` fails if the two drift apart.

---

## 🏛️ 4. Core Architecture & Two-Tier Agent Taxonomy

Agent definitions are never hardcoded inside UI views. `registry.py` inspects and categorizes agents into two distinct tiers:

### Tier 1: Main Agents (Coordinators)
- **Agent ID:** `software-architect` (Lead Software Architect)
- **Implementation:** Compiled state graph / coordinator (`agents/architect.py`).
- **Permissions:** **Restricted Shell Access** (`execute_restricted_command` for `py_compile`, `pytest`, verification only).
- **Absolute Rule:** Architect is strictly a planner, verifier, and delegator. It **NEVER writes application code** or creates files directly. It **ALWAYS delegates** implementation to Coder sub-agents.

### Tier 2: Coder Agents (Sub-Agents)
- **Agent IDs:**
  - `coder-deep` (Senior Backend Coder): Complex logic, algorithms, API microservices.
  - `coder-standard` (Junior Developer): Scaffolding, boilerplate, unit tests, documentation.
- **Permissions:** **Full Shell Access** (`execute_shell_command`) + full file writing tools.
- **Standardized Logging Protocol:** When a Coder completes a task, it writes an entry into the active `.md` plan file:
  ```markdown
  ## [<Task Title>]
  - **Status:** Completed
  - **Files Created/Modified:** `<code_file>`, `<test_file>`
  - **Summary of Work:** <Description of deliverables>
  ```

---

## 🔄 5. End-to-End Execution Flow

When a user submits a prompt in the desktop UI:

```
[User Input Bar]
      │
      ▼
[Confirmation Modal] ("Are you sure you want to execute against PLAN.md?")
      │ (User confirms)
      ▼
[BridgeAPI.start_execution()] (app.py) -> Launches background worker thread
      │
      ▼
[AgentRegistry.run_agent_workflow()] (registry.py)
      │
      ├── 1. Read Active Plan (PLAN.md): Anchors state without message history bloat.
      │      (If PLAN.md does not exist, auto-generates structured roadmap).
      │
      ├── 2. Architect Initiation: Emits "architect_spawn", logs thinking stream.
      │
      ├── 3. Scope Evaluation & Mandatory Delegation: Selects coder-deep or coder-standard.
      │      Emits "delegation" -> Triggers sidebar pulse animation.
      │
      ├── 4. Coder Spawn (50/50 Split Screen): Slides Architect left, spawns Coder right.
      │
      ├── 5. Coder Implementation:
      │      - Reads PLAN.md requirements.
      │      - Runs shell commands (e.g., environment checks, pip, runtime).
      │      - Writes production source code and test files.
      │      - Marks first pending checkbox `- [ ]` -> `- [x]` in PLAN.md.
      │      - Appends standardized progress entry to PLAN.md.
      │      - Emits "plan_updated" -> Right sidebar tree syncs in real time.
      │      - Emits "coder_summary" with deliverables.
      │
      ├── 6. Architect Automated Verification Loop:
      │      - Reads PLAN.md to audit logged updates.
      │      - Executes restricted shell verification (`python -m py_compile <file>`).
      │      - Emits "architect_summary" with approval & proposed next steps.
      │
      └── 7. Workflow Finalization:
             - Input bar re-enables; Stop button reverts to Send.
             - Card [X] close buttons unlock (disabled during runtime).
```

---

## 🖥️ 6. Desktop UI Specifications & Reference Aesthetic

The interface strictly adopts a dark glassmorphic design inspired by high-end AI workstations:

### 1. Left Sidebar (250px Width)
- **Top Workspace Card:** Displays active folder (`my_project_workspace`). Clicking "Change" opens native OS folder dialog.
  - *Fix Applied:* Dialog cleanly exits on first click of 'Cancel' or 'X' without reopening.
- **Section Headers:** `MAIN AGENTS` and `CODER AGENTS` with count pills.
- **Prompt Editor View:** Clicking any agent in the list smoothly transitions the stage to an inline system prompt editor with line numbers, tool pills, character counter, "Reset", and "Save" (persists to `.py` file on disk and reloads in memory with 0 app restarts).

### 2. Center Stage (Main Workspace)
- **Hero State:** Clean *"How can I help today?"* with an iridescent dynamic HTML5 canvas fluid orb (animated with bezier curves and specular reflection).
- **Execution Stage:** Hidden until execution starts. Renders a 50/50 split grid with:
  - Left: Architect Card (blue accents, live monospaced terminal logs, status pill).
  - Right: Coder Card (purple accents, live monospaced terminal logs, status pill).
  - Summary containers and close `[X]` buttons (unlocked upon completion).
- **Floating Input Card:**
  - Top Banner: `⚡ Note: Sending a message triggers immediate execution against the current plan step.`
  - Middle: Multi-line textarea (`Ask me anything ...`).
  - Bottom Toolbar:
    - Left: `[ 📎 Plan: PLAN.md ]` pill (opens plan switcher) and `[ ⚙ Tools ]` pill.
    - Right: Voice button and Send button (electric cyan `#0ea5e9` circle with up arrow). Morphs to glowing red Stop button during execution.

### 3. Right Sidebar (300px Plan Tree Tracker)
- **Native Markdown Extraction:** `tools.file_tools.parse_plan_tree()` uses regex/AST with zero LLM/API cost.
- **Progress Overview:** Dynamic progress bar showing `X/Y (Z%)` completion.
- **Clickable Tree Items:**
  - `[x]` Completed: Green checkmark `✓` with struck-through title.
  - `[-]` In Progress: Cyan pulsing dot `●`.
  - `[ ]` Pending: Dim circle `○`.
  - Clicking any task expands an accordion drawer showing nested bullet details and files modified.
- **Header Actions:**
  - `⇆` Switch Plan button: Opens modal to select among existing `.md` files.
  - `+` Create Plan button: Opens modal to input a new project idea and auto-scaffold a structured plan.

---

## 🛡️ 7. Token Efficiency & Context Protection Rules

1. **Middle Truncation on Shell Output:**  
   In `tools/shell_tools.py`, outputs exceeding `MAX_SHELL_OUTPUT_CHARS = 2400` are truncated with head/tail preserved:
   ```
   [First 1200 chars] ... [Omitted X characters] ... [Last 1200 chars]
   ```
2. **Middle Truncation on File Reads:**  
   In `tools/file_tools.py`, files exceeding `MAX_FILE_READ_CHARS = 6000` are truncated similarly (except `.md` plan files).
3. **Stateless Session Guarantee:**  
   No LangChain chat history is accumulated across turns. Each prompt initiates a clean session anchored exclusively to `PLAN.md`.

---

## 🐛 8. Bugs Resolved (Do Not Reintroduce)

1. **`parse_plan_tree` IndexError:**
   - *Cause:* In `tools/file_tools.py`, `elif steps and line_clean.startswith("- **") or line_clean.startswith("- *"):` had flawed operator precedence. If `steps` was empty and a bullet appeared, `steps[-1]` threw `IndexError`.
   - *Fix:* Rewritten with explicit grouping, header task detection (`## [Task: ...]`), and graceful fallback step creation.
2. **Missing Plan Silent Fail:**
   - *Cause:* If `PLAN.md` didn't exist, the workflow skipped initialization and appended directly to an empty file.
   - *Fix:* `registry.py` now checks plan existence and calls `create_new_plan_file` to scaffold a full roadmap before execution.
3. **Folder Dialog Reopening Loop:**
   - *Cause:* `pywebview.create_file_dialog` returning empty was triggering an unconditional `tkinter` fallback.
   - *Fix:* Explicit cancellation detection returns `{"cancelled": True}` immediately.

---

## 📋 9. Current Status of `PLAN.md`

Current contents of `my_project_workspace/PLAN.md`:
- [x] Initial specification and requirements formulation
- [x] Project scaffolding and runtime dependencies
- [ ] **Build core domain models and application logic** *(NEXT IMMEDIATE TASK)*
- [ ] Implement service endpoints and controllers
- [ ] Implement data validation and error handling
- [ ] Construct automated test suite with pytest
- [ ] Verify functionality via restricted shell execution
- [ ] Deliverable audit and deployment readiness check
- [x] FastAPI Weather Service Implementation (Verified deliverables: `weather_api.py`, `test_weather_api.py`)

---

## 🎯 10. How Incoming Agents Should Continue

1. **To advance the current roadmap:**
   - Prompt the system (or run CLI) targeting the next pending item:  
     `"Build core domain models and application logic for the weather microservice"`
2. **To inspect or modify agent prompts:**
   - Open the desktop app (`app.py`), click any agent in the left sidebar, edit the prompt, and click "Save System Prompt".
3. **To switch or create a new project:**
   - Click the Folder card at the top of the left sidebar to select a new directory.
   - Click the `+` button in the right sidebar to scaffold a fresh `.md` plan.
