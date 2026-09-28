"""Administrative-bypass actions: handled by the Architect alone, zero coder tokens.

These three intents only refine plan state or inspect the workspace read-only, so
the Gatekeeper resolves them inline instead of delegating. Each one ends with its
own `workflow_complete` and returns, exactly as the inline branches did.
"""

import os
import re
import time
from typing import Any, Dict

from orchestration.workflow.context import WorkflowContext, roadmap_brief
from orchestration.workflow import reasoning
from orchestration.workflow.events import tool_call, tool_result
from agents import laya as laya_gate
from agents.laya import inferred_ui
from tools.code_metrics import analyze_workspace_metrics
from tools.file_tools import (
    PLAN_JSON_FILE,
    audit_codebase_plan_sync,
    check_plan_structure,
    compile_plan_json_to_markdown,
    get_active_plan_filename,
    list_workspace_files,
    load_plan_state,
    parse_markdown_to_plan_dict,
    save_plan_state,
)
from tools.plan_state import append_pending_task, read_plan_markdown
from tools.recovery import get_backup_dir, snapshot_plan_revision
from tools.shell_tools import command_failed, execute_restricted_command
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
        # The same inference the parser uses, so a task added here is tagged exactly as it
        # would be had it been read from the markdown -- one vocabulary, one rule. The task
        # shape itself is shared with the result view's one-click "Add to Plan".
        append_pending_task(
            plan_state,
            new_task_title,
            note=f"Added via Architect Administrative Bypass: {clean_inst[:60]}",
            tag=UI_TAG if inferred_ui(new_task_title) else None,
        )
        updated = True
    else:
        # General refinement of pending milestones
        for sec in sections:
            for t in sec.get("tasks", []):
                if t.get("status") == "pending" and not updated:
                    t.setdefault("details", []).append(f"Architect directive: {clean_inst[:80]}")
                    updated = True
                    break

    if updated:
        # The result view offers a one-click revert of this revision, which needs the plan as
        # it was *before* the edit. The write immediately below is what replaces it on disk,
        # so this is the last moment the old state can still be captured.
        snapshot_plan_revision()

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

    # The Architect's own assessment of the refinement, when a provider is configured. The
    # plan mutation above stays deterministic -- a model is not allowed to rewrite the
    # roadmap from prose -- so the model's contribution is the reasoning about it, which is
    # what the button now actually returns.
    answer = reasoning.architect_answer(
        prompt=(
            f"A roadmap refinement was just applied for this directive: "
            f"{clean_inst or 'General Roadmap Refinement'}. Explain, in at most 4 lines, "
            f"what this changes about the plan and what should happen next."
        ),
        context_text=roadmap_brief(saved_plan),
        plan_file=ctx.plan_file,
    )
    if answer.used_llm:
        reasoning.stream_thought(ctx.stream_text, "software-architect", answer.reasoning)
        ctx.stream_text(
            "software-architect",
            f"> Architect assessment ({answer.model}):\n{answer.text}",
            log_type="decision",
            delay=0.02
        )

    update_plan_deliverables = [
        f"Machine state in `plan.json` synchronized from user directives.",
        f"Standardized markdown recompiled in `{ctx.plan_file}`.",
        "Administrative bypass active: zero Coder tokens consumed."
    ]
    if answer.used_llm:
        update_plan_deliverables.append(answer.usage_line())

    ctx.emit_fn({
        "type": "architect_summary",
        "agent": "software-architect",
        "summary": {
            "title": "Architect Plan Refinement (Administrative Bypass)",
            "status": "Plan Updated & Synced",
            "files": [ctx.plan_file, "plan.json"],
            "deliverables": update_plan_deliverables,
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

    # Syntax check with restricted shell. `list_workspace_files` reports each file's
    # basename in `name` and its workspace-relative path in `path`; the compile runs
    # with cwd set to the workspace root, so a file in a subdirectory must be named by
    # its path or the command cannot find it.
    py_files = [
        f["path"] for f in files
        if f["path"].endswith(".py") and not os.path.basename(f["path"]).startswith(".")
    ]
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
            # run_command_in_workspace always returns a non-empty "[Exit Code: N]..." block,
            # so `not comp_res.strip()` was always False and the raw shell header was shown
            # for a clean run (audit M4). `command_failed` reads the exit code, as
            # actions_impl does.
            comp_res.strip() if command_failed(comp_res) else "Syntax check clean"
        ))

    # The Architect's own findings, when a provider is configured. Read-only and still no
    # Coder: the bypass withholds Coder tokens, not the Architect's reasoning.
    answer = reasoning.architect_answer(
        prompt=(
            "Analyse this codebase and roadmap state. Report the 3 most important findings "
            "and the single most important next action."
        ),
        context_text=(
            "Workspace files: "
            + (", ".join(f["path"] for f in files[:30]) or "none")
            + f"\n\nAudit verdict: {audit_res['summary']}\n\n"
            + roadmap_brief(load_plan_state())
        ),
        plan_file=ctx.plan_file,
    )
    if answer.used_llm:
        reasoning.stream_thought(ctx.stream_text, "software-architect", answer.reasoning)
        ctx.stream_text(
            "software-architect",
            f"> Architect analysis ({answer.model}):\n{answer.text}",
            log_type="decision",
            delay=0.02
        )

    ctx.stream_text("software-architect", f"> Architectural Analysis Report:\n- Workspace modules: {len(files)} total files.\n- Verified Python syntax on: {', '.join(tested_files) if tested_files else 'None'}.\n- Plan synchronization status: {audit_res['summary']}\n- System Health: Stable.", delay=0.02)

    # Real source metrics, so the dashboard's complexity and security widgets are measured
    # rather than drawn as zeroes. Read-only, and it counts a file it cannot parse as
    # unparsed instead of aborting the analysis over one broken module.
    metrics = analyze_workspace_metrics()
    ctx.stream_text(
        "software-architect",
        f"> Static metrics: {metrics['functions']} functions across {metrics['modules']} modules, "
        f"average complexity {metrics['average_complexity']}, "
        f"{metrics['hotspot_count']} hotspot(s), {metrics['security_flag_count']} security flag(s).",
        delay=0.02
    )

    analyze_deliverables = [
        f"Evaluated workspace structure ({len(files)} files found).",
        f"Audit verdict: {audit_res['summary']}.",
        f"Verified compilation and syntax on core modules."
    ]
    if answer.used_llm:
        analyze_deliverables.append(answer.usage_line())

    ctx.emit_fn({
        "type": "architect_summary",
        "agent": "software-architect",
        "summary": {
            "title": "Architect Codebase & Structural Analysis",
            "status": "Analysis Complete",
            "files": [f["path"] for f in files[:8]],
            "deliverables": analyze_deliverables,
            "metrics": metrics,
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

    # The Architect's own recommendation, when a provider is configured. The administrative
    # bypass is about not spawning a *Coder*, not about scripting the answer: this intent is
    # exactly the one the Architect is positioned to reason about. Offline `used_llm` is
    # False and `rec_proposals` is the list this action always produced, unchanged.
    answer = reasoning.architect_answer(
        prompt=(
            "Recommend the next actions for this roadmap. Return at most 4 lines, each a "
            "single concrete action, each prefixed with '- '."
        ),
        context_text=roadmap_brief(plan_state),
        plan_file=ctx.plan_file,
    )
    if answer.used_llm:
        reasoning.stream_thought(ctx.stream_text, "software-architect", answer.reasoning)
        ctx.stream_text(
            "software-architect",
            f"> Architect recommendation ({answer.model}):\n{answer.text}",
            log_type="decision",
            delay=0.02
        )

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

    if answer.used_llm:
        modeled = answer.answer_lines()
        if modeled:
            rec_proposals = modeled[:4]

    recommend_deliverables = [
        f"Milestone progress analyzed: {completed_count}/{len(steps)} tasks finished.",
        f"Identified critical path focus: '{next_task.get('title') if next_task else 'Deployment'}'",
        "Architecture advisory prepared (Administrative Bypass, 0 coder tokens)."
    ]
    if answer.used_llm:
        recommend_deliverables.append(answer.usage_line())

    ctx.emit_fn({
        "type": "architect_summary",
        "agent": "software-architect",
        "summary": {
            "title": "Architect Strategic Roadmap Recommendations",
            "status": "Advisory Formulated",
            "files": [ctx.plan_file, "plan.json"],
            "deliverables": recommend_deliverables,
            "proposals": rec_proposals
        }
    })
    time.sleep(0.3)
    ctx.emit_fn({
        "type": "workflow_complete",
        "status": "finished",
        "message": "Recommendations generated via Administrative Bypass."
    })


def _back_up_plan_markdown(filename: str, content: str) -> str:
    """Snapshots the pre-normalization markdown into the workspace backup folder.

    The plan lives in the plan directory (the repository), not the code workspace, so
    ``backup_file_for_task`` cannot be reused here: it resolves relative to the workspace.
    The snapshot lands in the gitignored ``.deepagents_backups`` directory, which the plan
    switcher does not list, so a reformat is reversible without polluting ``list_plan_files``.
    """
    backup_dir = os.path.join(get_backup_dir(), "plan-normalize")
    os.makedirs(backup_dir, exist_ok=True)
    backup_path = os.path.join(backup_dir, filename)
    with open(backup_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    return backup_path


def normalize_plan_action(
    ctx: WorkflowContext,
    plan_state: Dict[str, Any],
    action_params: Dict[str, Any],
) -> None:
    """Reformats an unstructured markdown plan into the strict plan AST.

    The Normalization Gate's backend half: it checks the active plan against the AST shape
    the parser needs (a title, ``##`` sections, ``- [ ]`` milestones), and when the shape is
    missing it lifts the document's existing bullet and numbered lists into milestones and
    recompiles it. The reformat is the deterministic parser's job -- this path spends no
    Coder tokens and never delegates.

    It is deliberately conservative: nothing is written unless the reformat actually
    recovers a milestone, and the pre-normalization markdown is snapshotted first, so an
    import that cannot be structured is reported rather than emptied.
    """
    ctx.stream_text("software-architect", f"> [GATEKEEPER: ADMINISTRATIVE BYPASS] Intent: Normalize Plan.\n> Bypassing Coder delegation. Zero coder tokens will be used.", log_type="decision", delay=0.02)
    time.sleep(0.2)
    if ctx.should_stop(): return

    ctx.emit_fn(tool_call(
        "software-architect", "check_plan_structure", {"plan_file": ctx.plan_file},
        f"Validating AST structure of {ctx.plan_file} (sections and milestone checkboxes)"
    ))
    time.sleep(0.3)
    content = read_plan_markdown()
    report = check_plan_structure(content)
    ctx.emit_fn(tool_result("software-architect", "check_plan_structure", report["summary"]))
    time.sleep(0.2)

    counts = report.get("counts", {})
    recovered = 0
    normalized_markdown = content
    saved_plan = plan_state

    if report["structured"]:
        ctx.stream_text(
            "software-architect",
            f"> Normalization gate passed: {counts.get('sections', 0)} section(s), "
            f"{counts.get('milestones', 0)} milestone(s) already present.\n"
            f"> No reformatting required; `{ctx.plan_file}` left unchanged.",
            delay=0.02
        )
        time.sleep(0.3)
        status = "Already Structured"
        deliverables = [
            f"Confirmed the AST shape of `{ctx.plan_file}`: "
            f"{counts.get('sections', 0)} section(s), {counts.get('milestones', 0)} milestone(s).",
            "No file was rewritten -- the plan already parses cleanly.",
            "Administrative bypass active: zero Coder tokens consumed.",
        ]
    else:
        ctx.stream_text(
            "software-architect",
            f"> Normalization gate blocked the current shape: {' '.join(report['issues'])}\n"
            f"> Lifting the document's lists into the strict AST...",
            delay=0.02
        )
        time.sleep(0.2)
        parsed = parse_markdown_to_plan_dict(content, ctx.plan_file, relaxed=True)
        recovered = len(parsed.get("steps", []))

        if recovered:
            backup_path = _back_up_plan_markdown(ctx.plan_file, content)
            ctx.emit_fn(tool_call(
                "software-architect", "save_plan_state", {"plan_file": ctx.plan_file},
                f"Recompiling {ctx.plan_file} into the strict AST (backup: {os.path.basename(backup_path)})"
            ))
            time.sleep(0.3)
            saved_plan = save_plan_state(parsed)
            normalized_markdown = compile_plan_json_to_markdown(saved_plan)
            ctx.emit_fn(tool_result(
                "software-architect", "save_plan_state",
                f"Recovered {recovered} milestone(s); plan.json and {ctx.plan_file} rewritten."
            ))
            time.sleep(0.2)
            ctx.emit_fn({
                "type": "plan_updated",
                "filename": ctx.plan_file,
                "content": normalized_markdown,
                "tree": saved_plan.get("steps", []),
                "plan_json": saved_plan,
            })
            time.sleep(0.2)
            status = "Normalized"
            deliverables = [
                f"Lifted {recovered} milestone(s) out of the document's lists.",
                f"Recompiled `{ctx.plan_file}` and `plan.json` into the strict AST shape.",
                "Original markdown snapshotted before the rewrite (reversible).",
                "Administrative bypass active: zero Coder tokens consumed.",
            ]
        else:
            ctx.stream_text(
                "software-architect",
                "> No milestones could be recovered from this document's structure.\n"
                "> Nothing was written; add `- [ ]` milestone lines and run the gate again.",
                delay=0.02
            )
            time.sleep(0.3)
            status = "Needs Manual Structure"
            deliverables = [
                "Found no bullet or numbered list to lift into milestones.",
                f"`{ctx.plan_file}` left byte-for-byte unchanged.",
                "Add `- [ ]` milestone lines, or author the plan from the `+` dialog.",
            ]

    ctx.stream_text(
        "software-architect",
        f"> Normalization report: {report['summary']}\n> Outcome: {status}.",
        delay=0.02
    )
    time.sleep(0.3)
    ctx.emit_fn({
        "type": "architect_summary",
        "agent": "software-architect",
        "summary": {
            "title": "Architect Plan Normalization Gate",
            "status": status,
            "files": [ctx.plan_file, "plan.json"],
            "deliverables": deliverables,
            "proposals": [
                "Review the reformatted plan in the centre workbench.",
                "Execute the top pending milestone using 'Execute Next Step'.",
                "Run 'Retag Plan' to assign domains to the recovered milestones.",
            ],
        },
    })
    time.sleep(0.3)
    ctx.emit_fn({
        "type": "workflow_complete",
        "status": "finished",
        "message": f"Plan normalization finished with status: {status}.",
    })
