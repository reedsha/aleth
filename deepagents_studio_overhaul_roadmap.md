# Project Plan: System 1 & Context Architecture Overhaul

## 🌍 Global State Summary
- **Stack:** Python 3.12 (`pywebview`), HTML5/JS/CSS3 dark IDE UI, `markdown-it-py` AST parser.
- **Architecture:** Multi-agent orchestrator (`software-architect` coordinator + Coder sub-agents) with stateless session execution.
- **Current Milestone:** Implementing Laya System 1 routing, Line-Anchored Context slicing with Global State Summaries, and the `.md` Normalization Gate.
- **Active Context:** Preparing implementation tasks based on `HANDOFF.md` state.

---

## 1. System 1 Cognitive Brain Integration (Laya)
- [ ] [API] Implement Laya inference gateway and classification helper
  - Files: `** `orchestration/laya_gateway.py` (New)`, `app.py`
- [ ] [BE] Integrate sub-50ms intent router in workflow execution pipeline
  - Files: `** `orchestration/workflow/runner.py`, `orchestration/workflow/actions_admin.py`
- [ ] [BE] Implement zero-token `[UI]` task classifier during AST hydration
  - Files: `** `tools/plan_parser.py`, `tools/plan_state.py`
- [ ] [FE] Add input sufficiency validation gate for `Fix Bug` action
  - Files: `** `orchestration/workflow/actions_impl.py`, `ui/js/actions.js`

## 2. Line-Anchored Context Slicing & Living Behavioral Ledger
- [ ] [BE] Update `PLAN.md` AST parser & generator for `## 🌍 Global State Summary`
  - Files: `** `tools/plan_parser.py`, `tools/plan_state.py`, `orchestration/workflow/templates.py`
- [ ] [BE] Implement two-part context slicing builder
  - Files: `** `orchestration/workflow/context.py`
- [ ] [FE] Update Architect system prompt for dynamic summary compression & inline logs
  - Files: `** `agents/architect.py`, `agents/coders.py`

## 3. Strict `.md` Normalization Gate & Workspace Integrity
- [ ] [BE] Add AST structure validation scanner for custom plan files
  - Files: `** `tools/plan_parser.py`, `tools/plan_state.py`
- [ ] [API] Build Normalization Gate modal and Bridge API handler
  - Files: `** `app.py`, `ui/js/plan-modals.js`, `ui/css/modals.css`

## 4. Verification & Regression Testing
- [ ] [TEST] Run backend characterization and frontend startup contract checks
  - Files: `** `tests/test_characterization.py`, `tests/ui_startup_contract.js`
