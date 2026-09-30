# Project Plan: Aleth Studio Architecture Upgrade Roadmap

## 🌍 Global State Summary
- **Architecture:** Off-Code Development IDE with System 1 (Laya) & System 2 (Architect/Coder) dual-brain orchestration.
- **Current Core State:** pywebview shell over a compiled Rust core (`crates/deepagents_core`: markdown plan parser, dual-sync state engine, Line-Anchored Context Slicer), a Vite-built frontend served from `dist/` through a WebView2 virtual host, and the 6-action command palette operational.
- **Delivery:** The frontend is built by `npm run build` into `dist/` and served through WebView2's own host mapping (`aleth.local` in `app.py`), so the document and its assets are ordinary https requests -- not a `file://` document, and not a bundle inlined into `ui/index.html`. `ui/js` is ES modules with `ui/js/main.js` as the single entry point, the shared state is owned by `ui/js/store.js`, and `eslint` with `no-undef` runs over the sources as a gate.
- **Token Reality (live):** System 2 is wired on every code-writing path, and the administrative intents (Update Plan, Analyze, Recommend, analytical Custom directives) now answer through the Architect, reporting their own token usage. Each in-flight model call also streams its reasoning into the agent card. Laya remains the zero-token System 1 gate.
- **Target Layout:** Dual-view center stage (Interactive Plan Tree ↔ Raw MD) that additionally mounts one purpose-built result view per action (code diff, roadmap diff, structured dashboard, proposal cards); a tabbed left sidebar (Agents / Files / Plans / Env); a collapsible Plan Tracker rail on the right; a command palette (Ctrl/Cmd+K) as the action entry point; a top status bar that carries the whole run cluster (Ready state, active plan, completion figure, and Stop while a run is live); and a run-scoped elastic console toggled from the foot of the right rail.
- **Target Logic:** Sub-50ms Laya gating, Line-Anchored Context Slicing, inline Behavioral Ledger logging, and AST Normalization Gate. The result view's verdicts are measured rather than asserted: `tools/test_runner.py` executes the test file a task wrote and reports the real pass/fail, and `tools/code_metrics.py` derives the Analyze dashboard's complexity and security figures from the source with the stdlib parser. The plan schema carries **four** status marks — `[x]` completed, `[-]` in progress, `[ ]` pending, `[!]` failed — and the Architect's verification (compile plus the task's own tests) is the writer for `[!]`: a task only becomes `[x]` when its verification passes, while an inconclusive check (a missing runner, a collection error, no test file) leaves it completed rather than inventing a failure.
- **Settled Decisions:** The right panel is a fixed-width collapsible rail and is never drag-resizable. The action-drawer entry point is the command palette, renamed consistently across every reference rather than left as an alias. Plan selection has exactly one entry point — the left sidebar's Plans tab; the rail's Switch Plan (⇆) button was removed in Wave 7 after it turned out to be the tab's only route in the first place. The Create Plan, Switch Plan and audit dialogs are right-rail slide-in `.side-panel`s rather than centred modals. The standalone diff pane (`ui/js/diff-pane.js`) was removed outright, and code-level diff inspection is deliberately reinstated in exactly one place: the centre stage's result view, mounted on the terminal event and routed by `state.selectedAction`. No diff buttons return to the rail or the task cards. The bottom dock keeps no permanent status row: its read-only plan pill and progress figure were retired in Wave 7 (the top bar already showed the plan), the completion figure and Stop moved into that top bar, and the console toggle moved to the foot of the right rail. The dock itself stays, hosting the command drawer and the run-scoped console — the console was **kept bottom-docked** by explicit user choice over the alternative of widening the rail to host it.

---

## 1. Data Layer, Parser & Context Engine
- [x] [BE] Upgrade AST parser (`tools/plan_parser.py`) for advanced schema extraction
  - [x] [BE] Add `## 🌍 Global State Summary` header parsing and generation logic
  - [x] [BE] Add 2-space and 4-space indented checkbox parsing into a `sub_steps: []` array per task
  - [x] [FE] Add inline `🟢 Behavioral Log:` extraction directly beneath `- [x]` checkboxes
  - Deliverables: `main.py`, `test_main.py`
  - Files: `main.py`, `test_main.py`
- [x] [BE] Implement Line-Anchored Context Slicer in `orchestration/workflow/context.py`
  - [x] [BE] Construct prompts using `[Global State Summary (~50 tokens)] + [Target Task Line Slice (~100 tokens)]`
- [x] [BE] Update Architect directives in `agents/architect.py`
  - [x] [BE] Add milestone wrap-up directive to dynamically compress finished milestones into core facts
  - [x] Update Coder logging protocol to auto-append `🟢 Behavioral Log:` entries under completed checkboxes
  - Deliverables: `main.py`, `test_main.py`
  - Files: `main.py`, `test_main.py`
- [x] [BE] Implement AST Normalization Gate backend
  - [x] [BE] Add AST structure check (`##` headers and `- [ ]` checkboxes) to `tools/plan_state.py`
  - [x] [BE] Wire 1-click "Format via Architect" normalization handler into `orchestration/workflow/actions_admin.py`

## 2. Laya System 1 Integration (Zero-Token Decision Engine)
- [x] [BE] Build the Laya decision engine as `agents/laya.py` with no new dependencies
  - [x] [BE] Implement a pure-Python classifier exposing `classify(text, context) -> Verdict`
  - [x] Return `intent`, `domain`, `coder`, `confidence` and human-readable `reasons` on every verdict
  - [x] [BE] Keep the module free of torch / transformers imports so the current stdlib-only venv runs it
  - [x] [TEST] Do not touch `runner.resolve_intent` or `templates.select`, which are already zero-token and deterministic
- [x] [BE] Replace the substring Gatekeeper gate in `orchestration/workflow/actions_impl.py`
  - [x] [BE] `custom_action` L443 decides Coder-spawn vs admin bypass on 9 naive substrings
  - [x] [BE] Route the decision through `laya.classify` instead of the substring scan
  - [x] [TEST] Keep `test_custom_directive_bypasses_the_coder_for_analytical_prompts` green as the regression gate
- [x] [API] Route Coder deep-vs-standard selection through `laya.classify`
  - [x] `custom` hardcodes `target_coder_id = "coder-deep"`, so route it through `laya.coder_for_domain`
  - [x] [TEST] That keeps the pinned 'add a login endpoint' directive on the deep Coder while sending a genuine test-writing request to the standard Coder
  - [x] `next_step` uses an ad-hoc `is_ui or "core" in title` check, so route it through `laya.route_coder`
  - [x] Leave `fix_bug` hardcoded deep: a root-cause diagnosis has no routing decision to make
  - [x] [API] Preserve the pinned routing corpus: 'Project scaffolding and runtime dependencies' stays coder-standard and 'Build core domain models and application logic' stays coder-deep
  - [x] [TEST] Rule explicitly on the legacy `core` substring test, which also matches 'score' and 'encore', before changing it
- [x] [BE] Wire zero-token `[UI]` task tagging into `tools/plan_parser.py` during local AST hydration
  - [x] [FE] Reuse the `templates` UI keyword vocabulary rather than inventing a parallel word list
  - [x] [FE] Only write an inferred tag when the line carries none, so an explicit tag is never overridden
- [x] [BE] Implement Laya input sufficiency gate for `🐛 Fix Bug` in `orchestration/workflow/actions_impl.py`
  - [x] [FE] Today the only gate is client-side in `ui/js/actions.js`, so the backend accepts an empty report
  - [x] [TEST] Mirror the client-side rule server-side and return a clear refusal instead of a Coder spawn
- [x] [BE] Implement pre-flight plan drift probability check before spinning up codebase reconciliation audits
- [x] [API] Add the Laya ModernBERT backend behind the same `classify` seam (deferred)
  - [x] [BE] torch and transformers are installed (`laya 0.3.20`); checkpoints download on the first `Router.predict` call
  - [x] Keep it env-gated and optional so the heuristic engine remains the zero-dependency fallback
  - [x] [BE] Add a warm-up thread so first-call latency excludes model load, not just inference

## 3. Center-Stage & Workbench Redesign
- [x] [FE] Transform center stage into Dual-View Workbench (`ui/js/workbench.js`)
  - [x] [FE] Build Interactive Plan Tree as primary view with visual connectors (`|`) and drill-down chevrons (`>`)
  - [x] [FE] Add top-right view toggle `[ 🌳 Tree View | 📄 Raw MD ]` to switch between visual nodes and code editor text
  - [x] [FE] Render nested sub-steps and `🟢 Behavioral Log` entries directly inside task node cards
- [x] [FE] Redesign Right Panel into 60px fixed Utility Rail (`ui/js/sidebar.js` / `ui/css/sidebar-panels.css`)
  - [x] [FE] Rehouse core actions into compact icon buttons: Audit Sync (🛡️), Switch Plan (⇆), Create Plan (➕), Console Toggle (💻)
  - [x] [FE] Wave 7 correction: Switch Plan (⇆) is removed from the rail. `#tabSidebarPlans` was never wired (Agents, Files and Env were), so the rail's ⇆ was the only route to the Plans tab; that tab is wired now and is the single entry point for plan selection (`ui/js/wire.js`, `ui/js/sidebar.js`). The Create Plan and Switch Plan dialogs are right-rail slide-in `.side-panel`s now, matching the audit report.
  - [x] [FE] Wave 7: the Console Toggle (💻) finally lives in the rail, in a `.plan-sidebar-footer` pinned to the foot of `.right-plan-sidebar` — the home this task always named for it. It is a 28px rail icon (`.rail-console-toggle`) rather than the dock pill it became in Wave 5; `#btnConsoleToggle` and its listener are unchanged, and the footer is kept out of the collapsed rail's `display:none` group so it shows in both states.
  - [x] [FE] Retire the `#rightSidebarResizeHandle` seam and its drag listener, since a fixed rail has no width to drag
- [x] [FE] Remove code-level diff inspection views entirely
  - [x] [FE] Remove the inline `Diff` buttons from the right sidebar task cards
  - [x] [FE] Retire `ui/js/diff-pane.js` and `ui/css/diff-pane.css` from the bundle order in `tools/build_ui_bundle.py`
  - [x] Keep the console strictly for terminal execution logs and process health monitoring
  - [x] [TEST] Update the header watchdog list and `tests/ui_startup_contract.js` for the removed entry point
  - [x] [FE] Deliberate reversal, recorded here rather than discovered mid-stream: code-level diff inspection is reinstated in exactly one place — the centre stage's polymorphic result view (`ui/js/result-view.js`), mounted on the terminal event and routed by `state.selectedAction`. No `Diff` buttons return to the rail or the task cards.

## 4. Connected Dock, Console & Visual Polish
- [x] [FE] Implement Connected Tab Action Drawer in `#bottomDock` (`ui/js/dock.js` / `ui/css/actions.css`)
  - [x] [FE] Replace the floating center-stage popup while keeping its entry point contract intact
  - [x] [TEST] Rename the `openActionParamModal` entry point consistently across `ui/js/actions.js`, the header watchdog and the startup contract test
  - [x] [FE] Build the expander panel so it visually merges with the active action button directly above it
  - [x] [FE] Embed prompt textareas and visual artifact attachment dropzones inside the expander
- [x] [FE] Implement Hybrid Console System (`ui/js/console.js` / `ui/css/console.css`)
  - [x] [FE] Build slide-up overlay drawer in bottom dock for inline execution logging
  - [x] [FE] Add "Detach Console" feature using `pywebview.create_window()` for secondary monitor workflow tracking
  - [x] [FE] Wave 7: retire the dock's read-only status row (`#readonlyPlanPill`, `#planStatusDot`, `#txtBottomActivePlan` — duplicates of the top bar) and relocate the completion figure (`#txtTopProgress`) and **Stop** (`#btnStopRun`) into the top bar. The console toggle moves to the rail's foot, so `#bottomDock` keeps only the command drawer and the console: at idle its head is `display:none` and only the plan-lock notice (`.plan-locked`) keeps it on screen (`ui/css/dock.css`, `ui/js/plan-tree.js`).
- [x] [FE] Apply dark IDE UI polish
  - [x] [FE] Add custom dark scrollbar styles (`::-webkit-scrollbar`) with `#121212` track and `#2a2d32` / `#3e4249` thumb in `ui/css/base.css`
  - [x] [BE] Build Normalization Gate UI modal in `ui/js/plan-modals.js` for unstructured `.md` imports

## 5. Compiled Core, Mapped Assets & Modern Gates
- [x] [BE] Extract the plan engine into a compiled Rust core (`crates/deepagents_core`, PyO3 + maturin)
  - [x] [BE] Port the markdown AST walker to `pulldown-cmark` behind a markdown-it-shaped token stream, slicing raw source by offset so the parser's regexes see exactly what markdown-it showed them
  - [x] [BE] Port the dual-sync state engine: one `fs2` lock over both representations, atomic temp-file writes with a retrying rename
  - [x] [BE] Port middle truncation (file read, shell output, test runner) and the Line-Anchored Context Slicer into the core
  - [x] [BE] Keep the tag vocabulary in `tools/task_tags.py` and pass it in, so the plan tree and the delegation path cannot drift about what a tag is
  - [x] [TEST] Pin the port against the existing suite: 408 tests green, byte-identical round-trip on `PLAN.md`, parse under 1ms per call
- [x] [FE] Replace the inlined frontend bundle with a Vite build served through a WebView2 virtual host
  - [x] [FE] Delete `tools/build_ui_bundle.py` and the two generated regions in `ui/index.html` (12,660 lines -> 1,107)
  - [x] [FE] Add `vite.config.mjs` and `ui/build/vite-plugin-classic-bundle.mjs`, emitting one `assets/app.js` and one `assets/app.css` into `dist/` (superseded in §6: the sources are ES modules now and the concatenating plugin is gone)
  - [x] [BE] Map `aleth.local` onto `dist/` in `app.py` with `SetVirtualHostNameToFolderMapping`, installing it before the window's first navigation
  - [x] [TEST] Prove the built bundle actually runs: origin, stylesheet, module globals, rendered plan tree and no failure banner, in a real window
- [x] [TEST] Modernize the verification gates
  - [x] [TEST] Delete `tests/ui_startup_contract.js`; assert the running app's structure instead of counting markup
  - [x] [TEST] Add `playwright.config.mjs` and `tests/ui/structural.spec.mjs` (9 structural checks over `dist/`)
  - [x] [TEST] Run the backend suite in parallel: `pytest.ini` with `-n auto` (408 tests in ~26s, was ~50s serial)
  - [x] [TEST] Add the Rust core's own suite: `cargo test` (39 tests over the tokenizer, parser, state and slicing)

## 6. ES Modules & The Store Boundary
- [x] [TEST] Close the testing gap before touching the code: the structural suite proved the shell mounted but exercised no interaction at all
  - [x] [TEST] Drive real gestures and keystrokes in a browser: the dock resize seam (a true drag, with its floor and ceiling), command palette filtering, arrow navigation, Enter and Escape
  - [x] [TEST] Drive the inbound agent wire and assert what the user sees: a log reaching the console, a coder card coming on screen, a terminal event mounting the result view
  - [x] [TEST] Prove the new tests are not vacuous: break each behaviour at runtime and confirm the assertion fails
- [x] [BE] Put the shared state behind one explicit interface (`ui/js/store.js`)
  - [x] [BE] Audit the write surface: 260 write sites, of which 211 are `dom.js` filling its own element cache and only 7 fields are written from more than one module
  - [x] [BE] Give each genuinely shared field a named operation (`setAvailablePlans`, `setConsolePinned`, `setAgentCardOpen`, `setRunSummary`, `markRunRefused`, `clearRunSummary`, `capturePlanTreeBeforeUpdate`, `clearPlanTreeBeforeUpdate`) so every write to shared state is a named act
  - [x] [BE] Make the invariant checkable rather than aspirational: `tools/check_ui_state.py` fails the build if any field gains a second writer
- [x] [FE] Migrate `ui/js` to ES modules and delete the concatenating build
  - [x] [FE] Compute the import graph mechanically from the sources, convert all 21 modules, and verify every shared name resolves
  - [x] [FE] Add `ui/js/main.js` as the single entry point; delete `ui/build/vite-plugin-classic-bundle.mjs` and let Vite bundle the graph
  - [x] [BE] Replace the head watchdog's hand-maintained list of 35 `window` globals with a boot check plus the app's own DOM-cache diagnostic (`window.Aleth.diagnostics()`)
  - [x] [TEST] Un-paralyze static analysis: ESLint with `no-undef` is meaningful now that imports are explicit, and it passes clean over `ui/js`
