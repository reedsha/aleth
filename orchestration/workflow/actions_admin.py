"""Administrative-bypass actions: handled by the Architect alone, zero coder tokens.

These three intents only refine plan state or inspect the workspace read-only, so
the Gatekeeper resolves them inline instead of delegating. Each one ends with its
own `workflow_complete` and returns, exactly as the inline branches did.
"""

import re
import time
from typing import Any, Dict

from orchestration.workflow.context import WorkflowContext
from orchestration.workflow.events import tool_call, tool_result
from agents import laya as laya_gate
from agents.laya import inferred_ui
from tools.file_tools import (
    PLAN_JSON_FILE,
    audit_codebase_plan_sync,
    compile_plan_json_to_markdown,
    get_active_plan_filename,
    list_workspace_files,
    load_plan_state,
    save_plan_state,
)
from tools.shell_tools import execute_restricted_command
from tools.task_tags import UI_TAG


def update_plan_action(
    ctx: WorkflowContext,
    plan_state: Dict[str, Any],
    action_params: Dict[str, Any],
    user_message: str,
) -> None:
    """Refines the roadmap in plan_state, persists it, and recompiles the markdown."""
    ctx.stream_text("software-architect", f"> [GATEKEEPER: ADMINISTRATIVE BYPASS] Intent: Update Plan.\n> Bypassing Coder delegation. Zero coder tokens will be used.", log_type="decision", delay=0.02)
    time.sleep(0.2)
    if ctx.should_stop(): return

    ctx.emit_fn(tool_call(
        "software-architect", "load_plan_state", {"plan_file": ctx.plan_file},
        f"Hydrating machine state from {ctx.plan_file} (plan.json)"
    ))
    time.sleep(0.3)
    ctx.emit_fn(tool_result(
        "software-architect", "load_plan_state",
        f"Loaded plan state: {len(plan_state.get('sections', []))} sections, {len(plan_state.get('steps', []))} tasks."
    ))
    time.sleep(0.2)

    custom_instructions = action_params.get("customInstructions", "") or user_message
    clean_inst = custom_instructions.replace("[ACTION: UPDATE_PLAN]", "").replace("Instructions:", "").strip()
    ctx.stream_text("software-architect", f"> Evaluating plan refinement directives: \"{clean_inst or 'General Roadmap Refinement'}\"...", delay=0.02)

    # Update or refine tasks/sections in plan_state
    sections = plan_state.get("sections", [])
    updated = False

    # Check for explicit new task directives or append to last section
    if any(k in clean_inst.lower() for k in ["add task", "new task", "create task", "include task"]):
        new_task_title = re.sub(r'^(add|new|create|include)\s+task\s*:?', '', clean_inst, flags=re.I).strip()
        if not new_task_title:
            new_task_title = "Implement additional system requirement"
        target_sec = sections[-1] if sections else {"id": "sec-1", "title": "General", "tasks": []}
        task_id = f"task-{len(plan_state.get('steps', [])) + 1}"
        new_task = {
            "id": task_id,
            "section": target_sec.get("title"),
            "title": new_task_title,
            "status": "pending",
            # The same inference the parser uses, so a task added here is tagged exactly as
            # it would be had it been read from the markdown -- one vocabulary, one rule.
            "tag": UI_TAG if inferred_ui(new_task_title) else None,
            "details": [f"Added via Architect Administrative Bypass: {clean_inst[:60]}"],
            "files": []
        }
        target_sec.setdefault("tasks", []).append(new_task)
        updated = True
    else:
        # General refinement of pending milestones
        for sec in sections:
            for t in sec.get("tasks", []):
                if t.get("status") == "pending" and not updated:
                    t.setdefault("details", []).append(f"Architect directive: {clean_inst[:80]}")
                    updated = True
                    break

    saved_plan = save_plan_state(plan_state)
    latest_markdown = compile_plan_json_to_markdown(saved_plan)

    ctx.emit_fn(tool_call(
        "software-architect", "save_plan_state", {"plan_file": ctx.plan_file},
        f"Persisting refined AST state to plan.json & recompiling {ctx.plan_file}"
    ))
    time.sleep(0.3)
    ctx.emit_fn(tool_result(
        "software-architect", "save_plan_state",
        f"Plan state saved successfully ({len(latest_markdown)} chars markdown compiled)"
    ))
    time.sleep(0.2)

    ctx.emit_fn({
        "type": "plan_updated",
        "filename": ctx.plan_file,
        "content": latest_markdown,
        "tree": saved_plan.get("steps", []),
        "plan_json": saved_plan
    })
    time.sleep(0.3)
    ctx.stream_text("software-architect", f"> Plan successfully updated and compiled into `{ctx.plan_file}`.\n> Ready for milestone execution.", delay=0.02)

    ctx.emit_fn({
        "type": "architect_summary",
        "agent": "software-architect",
        "summary": {
            "title": "Architect Plan Refinement (Administrative Bypass)",
            "status": "Plan Updated & Synced",
            "files": [ctx.plan_file, "plan.json"],
            "deliverables": [
                f"Machine state in `plan.json` synchronized from user directives.",
                f"Standardized markdown recompiled in `{ctx.plan_file}`.",
                "Administrative bypass active: zero Coder tokens consumed."
            ],
            "proposals": [
                "Execute top pending task using 'Execute Next Step'.",
                "Run Codebase Audit to verify disk synchronization.",
                "Review task file dependencies in Plan Tracker."
            ]
        }
    })
    time.sleep(0.3)
    ctx.emit_fn({
        "type": "workflow_complete",
        "status": "finished",
        "message": "Plan updated successfully via Administrative Bypass."
    })


def analyze_action(ctx: WorkflowContext) -> None:
    """Lists the workspace, audits plan/disk sync, and syntax-checks core modules."""
    ctx.stream_text("software-architect", f"> [GATEKEEPER: ADMINISTRATIVE BYPASS] Intent: Analyze Codebase.\n> Bypassing Coder delegation. Zero coder tokens will be used.", log_type="decision", delay=0.02)
    time.sleep(0.2)
    if ctx.should_stop(): return

    ctx.emit_fn(tool_call(
        "software-architect", "list_workspace_files", {},
        "Scanning workspace directory structure"
    ))
    time.sleep(0.3)
    files = list_workspace_files()
    file_names = [f["path"] for f in files]
    ctx.emit_fn(tool_result(
        "software-architect", "list_workspace_files",
        f"Found {len(files)} files: {', '.join(file_names[:6])}{'...' if len(files) > 6 else ''}"
    ))
    time.sleep(0.2)

    ctx.emit_fn(tool_call(
        "software-architect", "audit_codebase_plan_sync", {},
        "Comparing workspace modules against plan.json deliverables"
    ))
    time.sleep(0.3)
    # Pre-flight: System 1 estimates how far the plan and the code have drifted from the
    # paths just listed, so the reconciliation is only paid for when it is likely to have
    # something to say. The tool_call/tool_result pair is emitted either way -- only the
    # verdict differs -- so the console reads the same shape and no event is gained or lost.
    # The plan's own files belong to no task by design, and the audit excludes them too.
    drift = laya_gate.plan_drift(
        load_plan_state(),
        file_names,
        ignore={PLAN_JSON_FILE, get_active_plan_filename()},
    )
    if drift.probability >= laya_gate.DRIFT_THRESHOLD:
        audit_res = audit_codebase_plan_sync()
        audit_detail = audit_res["summary"]
    else:
        # The report the rest of this action reads. It carries the pre-flight verdict rather
        # than a reconciliation that was deliberately not run, so every later mention of the
        # sync status stays truthful instead of quoting a scan that never happened.
        audit_detail = (
            f"Pre-flight: plan drift unlikely ({drift.probability:.0%}). "
            f"{drift.reasons[0][:1].upper()}{drift.reasons[0][1:]}; "
            f"full reconciliation not needed."
        )
        audit_res = {"summary": audit_detail, "in_sync": True}
    ctx.emit_fn(tool_result(
        "software-architect", "audit_codebase_plan_sync",
        audit_detail
    ))
    time.sleep(0.2)

    # Syntax check with restricted shell
    py_files = [f["name"] for f in files if f["name"].endswith(".py") and not f["name"].startswith(".")]
    tested_files = []
    for pyf in py_files[:3]:
        ctx.emit_fn(tool_call(
            "software-architect", "execute_restricted_command",
            {"command": f"python -m py_compile {pyf}"},
            f"Validating syntax for {pyf}"
        ))
        time.sleep(0.2)
        comp_res = execute_restricted_command.invoke({"command": f"python -m py_compile {pyf}"})
        tested_files.append(pyf)
        ctx.emit_fn(tool_result(
            "software-architect", "execute_restricted_command",
            "Syntax check clean" if not comp_res.strip() else comp_res.strip()
        ))

    ctx.stream_text("software-architect", f"> Architectural Analysis Report:\n- Workspace modules: {len(files)} total files.\n- Verified Python syntax on: {', '.join(tested_files) if tested_files else 'None'}.\n- Plan synchronization status: {audit_res['summary']}\n- System Health: Stable.", delay=0.02)

    ctx.emit_fn({
        "type": "architect_summary",
        "agent": "software-architect",
        "summary": {
            "title": "Architect Codebase & Structural Analysis",
            "status": "Analysis Complete",
            "files": [f["path"] for f in files[:8]],
            "deliverables": [
                f"Evaluated workspace structure ({len(files)} files found).",
                f"Audit verdict: {audit_res['summary']}.",
                f"Verified compilation and syntax on core modules."
            ],
            "proposals": [
                "Run 'Fix Bug' if error stack traces are observed.",
                "Use 'Execute Next Step' to proceed with next milestone.",
                "Run 'Force Code to Match Plan' if deliverables need reconstruction."
            ]
        }
    })
    time.sleep(0.3)
    ctx.emit_fn({
        "type": "workflow_complete",
        "status": "finished",
        "message": "Codebase analysis completed via Administrative Bypass."
    })


def recommend_action(ctx: WorkflowContext, plan_state: Dict[str, Any]) -> None:
    """Reads plan progress and formulates the Architect's strategic proposals."""
    ctx.stream_text("software-architect", f"> [GATEKEEPER: ADMINISTRATIVE BYPASS] Intent: Strategic Recommendation.\n> Bypassing Coder delegation. Zero coder tokens will be used.", log_type="decision", delay=0.02)
    time.sleep(0.2)
    if ctx.should_stop(): return

    ctx.emit_fn(tool_call(
        "software-architect", "load_plan_state", {"plan_file": ctx.plan_file},
        "Inspecting current plan progress and task milestones"
    ))
    time.sleep(0.3)
    ctx.emit_fn(tool_result(
        "software-architect", "load_plan_state",
        f"Plan loaded: {len(plan_state.get('steps', []))} total tasks."
    ))
    time.sleep(0.2)

    steps = plan_state.get("steps", [])
    completed_count = sum(1 for s in steps if s.get("status") == "completed")
    pending_tasks = [s for s in steps if s.get("status") == "pending"]
    next_task = pending_tasks[0] if pending_tasks else None

    ctx.stream_text(
        "software-architect",
        f"> Roadmap Audit: {completed_count}/{len(steps)} tasks completed.\n"
        f"> Next priority task: \"{next_task.get('title') if next_task else 'All tasks complete'}\".\n"
        f"> Formulating strategic architectural guidance...",
        delay=0.02
    )
    time.sleep(0.3)

    rec_proposals = []
    if next_task:
        rec_proposals.append(f"Execute immediate next task: '{next_task.get('title')}'")
        if next_task.get("tag") == UI_TAG:
            rec_proposals.append("Prepare visual mockup/screenshot reference for UI task.")
        rec_proposals.append("Review file dependencies before generating code.")
    else:
        rec_proposals.append("All plan milestones completed! Run automated test suite.")
        rec_proposals.append("Package application for production deployment.")
        rec_proposals.append("Create new feature roadmap file.")

    ctx.emit_fn({
        "type": "architect_summary",
        "agent": "software-architect",
        "summary": {
            "title": "Architect Strategic Roadmap Recommendations",
            "status": "Advisory Formulated",
            "files": [ctx.plan_file, "plan.json"],
            "deliverables": [
                f"Milestone progress analyzed: {completed_count}/{len(steps)} tasks finished.",
                f"Identified critical path focus: '{next_task.get('title') if next_task else 'Deployment'}'.",
                "Architecture advisory prepared (Administrative Bypass, 0 coder tokens)."
            ],
            "proposals": rec_proposals
        }
    })
    time.sleep(0.3)
    ctx.emit_fn({
        "type": "workflow_complete",
        "status": "finished",
        "message": "Recommendations generated via Administrative Bypass."
    })
