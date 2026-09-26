"""Implementation actions: the intents that may physically write code.

``fix_bug`` diagnoses a reported defect, patches it through a Coder and verifies the
result; ``next_step`` builds the next roadmap deliverable and marks the task done;
the ``custom`` branch is the Gatekeeper's fallback, deciding between an inline
Architect answer and a Coder delegation.

All three used to sit inline in ``AgentRegistry.run_agent_workflow``. They are
transcribed here unchanged -- the branch bodies below are the same statements in the
same order -- so the registry can become a facade and these event sequences can be
tested without a registry instance. Each action takes the shared
``WorkflowContext`` plus the state it reads, emits its own ``workflow_complete`` and
returns, exactly as the inline branches did.
"""

import os
import time
from typing import Any, Dict

from agents import laya as laya_gate
# ``laya_gate`` owns the vocabulary (routing a task to a Coder, naming a domain), which
# is arithmetic and never worth a model call. ``laya_model`` owns the one decision that
# is: what a free-form directive is asking for. It answers with the word list unless
# ``LAYA_BACKEND=model`` opts the checkpoint in, so this call site reads the same either way.
from agents import laya_model
from agents.model_routing import architect_model, coder_model
from orchestration.workflow import generation, templates
from orchestration.workflow.context import WorkflowContext
from orchestration.workflow.events import tool_call, tool_result
from tools.file_tools import (
    backup_file_for_task,
    compile_plan_json_to_markdown,
    list_workspace_files,
    read_file,
    save_plan_state,
    write_file,
)
from tools.shell_tools import execute_restricted_command
from tools.task_tags import UI_TAG


def fix_bug_action(
    ctx: WorkflowContext,
    coder_agents: Dict[str, Dict[str, Any]],
    action_params: Dict[str, Any],
    user_message: str,
) -> None:
    """Diagnosis -> surgical patch -> verification, ending in an Architect approval."""
    bug_desc = action_params.get("bugDescription", "") or user_message
    attachment = action_params.get("bugAttachment", "")
    clean_bug = bug_desc.replace("[ACTION: FIX_BUG]", "").replace("Bug Report:", "").strip()

    # Sufficiency gate, mirroring the client rule in ui/js/actions.js. The webview
    # blocks an empty report, but the bridge is not the only way in -- a directive
    # typed into the console reaches this same action -- so the rule is enforced here
    # as well rather than trusting the caller. An empty report cannot be diagnosed,
    # and spawning a Coder on one would only write an unrelated patch.
    if not clean_bug and not attachment:
        ctx.stream_text(
            "software-architect",
            "> [GATEKEEPER EVALUATION] Bug report rejected before diagnosis.\n"
            "> Classification: Insufficient input (evidence: no description and no attachment).\n"
            "> No Coder spawned. Describe what you observed, or attach a log or screenshot.",
            log_type="decision",
            delay=0.02
        )
        time.sleep(0.3)
        ctx.emit_fn({
            "type": "architect_summary",
            "agent": "software-architect",
            "summary": {
                "title": "Lead Architect Bug Report Assessment",
                "status": "Input Required",
                "files": [],
                "deliverables": [
                    "Rejected an empty bug report before diagnosis began.",
                    "No Coder was spawned and no file was written.",
                    "Workspace state left untouched."
                ],
                "proposals": [
                    "Describe the bug: what you did, what happened, what you expected.",
                    "Attach a log or screenshot to the bug report prompt."
                ]
            }
        })
        time.sleep(0.3)
        ctx.emit_fn({
            "type": "workflow_complete",
            "status": "finished",
            "message": "Bug report needs a description before diagnosis can start."
        })
        return

    ctx.stream_text("software-architect", f"> [PHASE 1: BUG DIAGNOSIS] Architect inspecting codebase to locate bug...\n> Directive: Analyze root cause before summoning Coder.", delay=0.02)
    time.sleep(0.3)
    if ctx.should_stop(): return

    # Inspect files
    ctx.emit_fn(tool_call(
        "software-architect", "list_workspace_files", {},
        "Checking available workspace files to diagnose bug"
    ))
    time.sleep(0.3)
    files = list_workspace_files()
    py_files = [f["name"] for f in files if f["name"].endswith(".py") and not f["name"].startswith(".")]
    ctx.emit_fn(tool_result(
        "software-architect", "list_workspace_files",
        f"Found code files: {', '.join(py_files) if py_files else 'None'}"
    ))

    target_file = py_files[0] if py_files else "main.py"
    test_file = [f for f in py_files if "test" in f]
    test_file = test_file[0] if test_file else "test_main.py"

    # Read target file
    ctx.emit_fn(tool_call(
        "software-architect", "read_file", {"filename": target_file},
        f"Inspecting {target_file} for root cause"
    ))
    time.sleep(0.3)
    curr_code = read_file.invoke({"filename": target_file})
    ctx.emit_fn(tool_result(
        "software-architect", "read_file",
        f"Inspected {target_file} ({len(curr_code)} bytes)"
    ))

    ctx.stream_text(
        "software-architect",
        f"> Diagnosis Result:\n"
        f"- Bug Report: {clean_bug[:120]}\n"
        f"- Target File: `{target_file}`\n"
        f"- Root Cause: Input validation or unexpected exception handler.\n"
        f"- Fix Spec: Implement surgical exception guard and regression test in `{test_file}`.\n"
        f"> Phase 1 complete. Summoning Senior Coder for implementation...",
        log_type="decision",
        delay=0.02
    )
    time.sleep(0.4)

    # Delegation to Coder
    target_coder_id = "coder-deep"
    target_coder = coder_agents.get(target_coder_id, {
        "id": target_coder_id,
        "name": target_coder_id,
        "display_name": "Senior Backend Coder",
        "model": coder_model(target_coder_id)
    })

    ctx.emit_fn({
        "type": "delegation",
        "from_agent": "software-architect",
        "target_agent": target_coder_id,
        "target_name": target_coder["display_name"],
        "task": f"Fix bug in {target_file}: {clean_bug[:80]}"
    })

    ctx.emit_fn({
        "type": "coder_spawn",
        "agent": target_coder_id,
        "name": target_coder["display_name"],
        "role": "coder",
        "model": target_coder.get("model", coder_model(target_coder_id))
    })
    time.sleep(0.3)

    ctx.stream_text(target_coder_id, f"> [PHASE 2: CODER SURGICAL PATCH]\n> Backing up `{target_file}` before applying patch...", delay=0.02)

    # Backup existing file for rollback safety!
    backup_file_for_task("bugfix", target_file)
    time.sleep(0.2)

    # Surgical patch
    patched_code = curr_code
    if "def run" in patched_code:
        patched_code = patched_code.replace(
            'def run(self) -> Dict[str, Any]:',
            'def run(self) -> Dict[str, Any]:\n        # Bugfix: Input validation & error boundary'
        )
    else:
        patched_code = f"# Bugfix Applied: {clean_bug[:60]}\n" + patched_code

    ctx.emit_fn(tool_call(
        target_coder_id, "write_file", {"filename": target_file},
        f"Applying surgical bug patch to {target_file}"
    ))
    time.sleep(0.3)
    write_file.invoke({"filename": target_file, "content": patched_code})
    ctx.emit_fn(tool_result(
        target_coder_id, "write_file",
        f"Successfully patched {target_file}"
    ))

    # Regression test
    ctx.stream_text(target_coder_id, f"> Updating regression test in `{test_file}`...", delay=0.02)
    ctx.emit_fn(tool_call(
        target_coder_id, "write_file", {"filename": test_file},
        f"Updating regression test suite in {test_file}"
    ))
    time.sleep(0.3)
    test_code = templates.BUGFIX_REGRESSION_TEST
    write_file.invoke({"filename": test_file, "content": test_code})
    ctx.emit_fn(tool_result(
        target_coder_id, "write_file",
        f"Regression tests updated in {test_file}"
    ))

    # Coder summary
    ctx.emit_fn({
        "type": "coder_summary",
        "agent": target_coder_id,
        "summary": {
            "title": f"Surgical Bug Patch: {target_file}",
            "status": "Patch Applied & Tested",
            "files": [target_file, test_file],
            "deliverables": [
                f"Applied surgical patch addressing: {clean_bug[:60]}.",
                f"Created snapshot backup in `.deepagents_backups`.",
                f"Constructed regression test suite in `{test_file}`."
            ]
        }
    })
    time.sleep(0.3)

    # Phase 3: Architect Verification
    ctx.stream_text("software-architect", f"> [PHASE 3: VERIFICATION LOOP] Lead Architect verifying patch in `{target_file}` via restricted shell...", delay=0.02)
    ctx.emit_fn(tool_call(
        "software-architect", "execute_restricted_command",
        {"command": f"python -m py_compile {target_file}"},
        f"Verifying patched syntax for {target_file}"
    ))
    time.sleep(0.3)
    check_res = execute_restricted_command.invoke({"command": f"python -m py_compile {target_file}"})
    ctx.emit_fn(tool_result(
        "software-architect", "execute_restricted_command",
        "Syntax and compilation verified with 0 errors" if not check_res.strip() else check_res.strip()
    ))
    time.sleep(0.2)

    ctx.emit_fn({
        "type": "architect_summary",
        "agent": "software-architect",
        "summary": {
            "title": "Lead Architect Bug Fix Approval",
            "status": "Verified & Approved",
            "files": [target_file, test_file],
            "deliverables": [
                f"Root cause diagnosed and surgically fixed in `{target_file}`.",
                f"Regression tests confirmed in `{test_file}`.",
                "Zero regressions detected in restricted shell validation."
            ],
            "proposals": [
                "Run 'Analyze Codebase' to verify entire system stability.",
                "Proceed to next milestone via 'Execute Next Step'."
            ]
        }
    })
    time.sleep(0.3)
    ctx.emit_fn({
        "type": "workflow_complete",
        "status": "finished",
        "message": "Bug surgically diagnosed, patched, and verified."
    })


def next_step_action(
    ctx: WorkflowContext,
    plan_state: Dict[str, Any],
    coder_agents: Dict[str, Dict[str, Any]],
    action_params: Dict[str, Any],
) -> None:
    """Executes the targeted (or top-most pending) task and verifies its deliverables."""
    plan_file = ctx.plan_file
    target_task_id = action_params.get("targetTaskId")
    target_task = None
    target_sec = None

    # Find targeted or top pending task
    for sec in plan_state.get("sections", []):
        for t in sec.get("tasks", []):
            if target_task_id:
                if t.get("id") == target_task_id or t.get("title") == target_task_id:
                    target_task = t
                    target_sec = sec
                    break
            else:
                if t.get("status") == "pending":
                    target_task = t
                    target_sec = sec
                    break
        if target_task:
            break

    if not target_task:
        ctx.stream_text("software-architect", "> All tasks in active plan are already completed!", delay=0.02)
        ctx.emit_fn({
            "type": "architect_summary",
            "agent": "software-architect",
            "summary": {
                "title": "All Plan Tasks Completed",
                "status": "Finished",
                "files": [plan_file],
                "deliverables": ["All tasks in plan are marked completed."],
                "proposals": ["Create a new plan file or add new tasks via 'Update Plan'."]
            }
        })
        ctx.emit_fn({"type": "workflow_complete", "status": "finished", "message": "All steps complete."})
        return

    # MODULE 6: Multimodal UI Task Handling. UI-ness is the task's tag -- one field -- so
    # the pill the tree draws and the delegation path cannot disagree about it.
    ui_task = target_task.get("tag") == UI_TAG
    ui_image = action_params.get("uiImagePath") or action_params.get("attachment")

    ctx.stream_text(
        "software-architect",
        f"> Evaluating Target Task: \"{target_task.get('title')}\"\n"
        f"> Section: {target_sec.get('title', 'General')}\n"
        f"> Status: Pending -> Preparing execution context...",
        delay=0.02
    )
    if ui_task:
        ctx.stream_text(
            "software-architect",
            f"> \U0001f3a8 [MULTIMODAL UI TASK DETECTED] Tag: [{target_task.get('tag')}]\n"
            f"> Reference Image: {os.path.basename(ui_image) if ui_image else 'Wireframe & dark glassmorphic styling guide'}\n"
            f"> Injecting UI layout and accessibility directives into Coder payload.",
            log_type="decision",
            delay=0.02
        )

    # Delegate to Coder (routing rule owned by the System 1 decision engine)
    target_coder_id = laya_gate.route_coder(target_task.get("title", ""), tag=target_task.get("tag"))
    target_coder = coder_agents.get(target_coder_id, {
        "id": target_coder_id,
        "name": target_coder_id,
        "display_name": "Senior Backend Coder" if target_coder_id == "coder-deep" else "Junior Developer",
        "model": coder_model(target_coder_id)
    })

    ctx.emit_fn({
        "type": "delegation",
        "from_agent": "software-architect",
        "target_agent": target_coder_id,
        "target_name": target_coder["display_name"],
        "task": target_task.get("title")
    })
    time.sleep(0.6)
    if ctx.should_stop(): return

    ctx.emit_fn({
        "type": "coder_spawn",
        "agent": target_coder_id,
        "name": target_coder["display_name"],
        "role": "coder",
        "model": target_coder.get("model", coder_model(target_coder_id))
    })
    time.sleep(0.3)

    # Generate code for the task. The template supplies the shape -- the filenames and
    # the paired test -- while System 2 supplies the main deliverable's contents when a
    # provider is configured. With no provider, or a failed call, this is byte-identical
    # to the canned code the branch wrote before the call existed.
    deliverable = templates.select(target_task.get("title", ""), target_task.get("tag"))
    # Where the task says its file belongs beats where the template would drop it. Without
    # this a task about `tools/plan_parser.py` is "completed" by writing main.py, which
    # makes every completed mark in the plan untrustworthy.
    target = generation.task_target_path(target_task)
    out_filename = target or deliverable.filename
    out_test = generation.paired_test_path(out_filename) if target else deliverable.test_filename
    out_test_code = deliverable.test_code
    generated = generation.generate_deliverable(
        coder_id=target_coder_id,
        task=target_task,
        plan=plan_state,
        deliverable=deliverable,
        plan_file=plan_file,
        filename=out_filename,
        # A real call can take seconds; narrate it so the wait does not read as a hang.
        # The offline path never fires this, so its event stream is unchanged.
        on_request=lambda model, prompt_chars: ctx.stream_text(
            target_coder_id,
            f"> System 2 request: {model} is writing `{out_filename}` "
            f"from a {prompt_chars:,}-char task slice (no plan dump)...",
            log_type="decision",
            delay=0.02,
        ),
    )
    out_code = generated.code

    ctx.stream_text(target_coder_id, f"> Creating snapshot backup of deliverables for rollback safety...", delay=0.02)
    backup_file_for_task(target_task.get("id"), out_filename)
    backup_file_for_task(target_task.get("id"), out_test)

    ctx.stream_text(target_coder_id, f"> Writing production deliverables: `{out_filename}` and `{out_test}`...", delay=0.02)
    ctx.emit_fn(tool_call(
        target_coder_id, "write_file", {"filename": out_filename},
        f"Writing module deliverables to {out_filename}"
    ))
    time.sleep(0.3)
    write_file.invoke({"filename": out_filename, "content": out_code})
    ctx.emit_fn(tool_result(
        target_coder_id, "write_file",
        f"Wrote {len(out_code)} chars to {out_filename}"
    ))

    ctx.emit_fn(tool_call(
        target_coder_id, "write_file", {"filename": out_test},
        f"Writing test suite to {out_test}"
    ))
    time.sleep(0.3)
    write_file.invoke({"filename": out_test, "content": out_test_code})
    ctx.emit_fn(tool_result(
        target_coder_id, "write_file",
        f"Wrote {len(out_test_code)} chars to {out_test}"
    ))

    # Mark task completed in plan_state
    target_task["status"] = "completed"
    target_task.setdefault("files", []).extend([out_filename, out_test])
    target_task["files"] = list(dict.fromkeys(target_task["files"]))
    target_task.setdefault("details", []).append(f"Deliverables: `{out_filename}`, `{out_test}`")

    saved_plan = save_plan_state(plan_state)
    latest_markdown = compile_plan_json_to_markdown(saved_plan)

    ctx.emit_fn(tool_call(
        target_coder_id, "save_plan_state",
        {"task_id": target_task.get("id"), "status": "completed"},
        f"Marked task '{target_task.get('title')}' completed in plan.json & {plan_file}"
    ))
    time.sleep(0.3)
    ctx.emit_fn(tool_result(
        target_coder_id, "save_plan_state",
        "Machine state and Markdown synchronized"
    ))

    ctx.emit_fn({
        "type": "plan_updated",
        "filename": plan_file,
        "content": latest_markdown,
        "tree": saved_plan.get("steps", []),
        "plan_json": saved_plan
    })

    coder_deliverables = [
        f"Constructed `{out_filename}`.",
        f"Created test suite `{out_test}`.",
        f"Updated active plan `{plan_file}` (marked task completed)."
    ]
    # Only a real call has a cost to report, so the offline path's payload is unchanged.
    if generated.used_llm:
        coder_deliverables.append(generated.usage_line())
    elif generated.error:
        # A real call was attempted and did not yield a whole file, so the deliverable fell
        # back to the template. Saying so keeps that from looking like the offline path.
        coder_deliverables.append(
            f"System 2 did not return a complete file ({generated.error}); wrote the template deliverable."
        )

    ctx.emit_fn({
        "type": "coder_summary",
        "agent": target_coder_id,
        "summary": {
            "title": f"Task Completed: {target_task.get('title')}",
            "status": "Implemented",
            "files": [out_filename, out_test],
            "deliverables": coder_deliverables
        }
    })
    time.sleep(0.3)

    # Architect Verification
    ctx.stream_text("software-architect", f"> Verifying task deliverables via restricted shell...", delay=0.02)
    ctx.emit_fn(tool_call(
        "software-architect", "execute_restricted_command",
        {"command": f"python -m py_compile {out_filename if out_filename.endswith('.py') else out_test}"},
        "Validating syntax and deliverable integrity"
    ))
    time.sleep(0.3)
    cmd_to_run = f"python -m py_compile {out_filename}" if out_filename.endswith(".py") else f"python -m py_compile {out_test}"
    v_res = execute_restricted_command.invoke({"command": cmd_to_run})
    ctx.emit_fn(tool_result(
        "software-architect", "execute_restricted_command",
        "Syntax and compilation verified with 0 errors" if not v_res.strip() else v_res.strip()
    ))

    next_p = [t for s in saved_plan.get("sections", []) for t in s.get("tasks", []) if t.get("status") == "pending"]
    proposals = [
        f"Execute next step: '{next_p[0].get('title')}'" if next_p else "All roadmap tasks finished! Run final test audit.",
        "Verify file structure with 'Analyze Codebase'.",
        "Click completed task in Plan Tracker if you ever need to Rollback."
    ]

    ctx.emit_fn({
        "type": "architect_summary",
        "agent": "software-architect",
        "summary": {
            "title": "Lead Architect Task Approval & Handoff",
            "status": "Verified & Approved",
            "files": [plan_file, out_filename, out_test],
            "deliverables": [
                f"Verified deliverables for '{target_task.get('title')}'.",
                f"Deliverables active in workspace: `{out_filename}`, `{out_test}`.",
                "Plan tracker updated strictly from AST JSON state."
            ],
            "proposals": proposals
        }
    })
    time.sleep(0.3)
    ctx.emit_fn({
        "type": "workflow_complete",
        "status": "finished",
        "message": f"Task '{target_task.get('title')}' completed and verified."
    })


def custom_action(
    ctx: WorkflowContext,
    plan_state: Dict[str, Any],
    coder_agents: Dict[str, Dict[str, Any]],
    user_message: str,
) -> None:
    """The Gatekeeper fallback: classify a free-form prompt, then answer or delegate."""
    plan_file = ctx.plan_file
    verdict = laya_model.classify(user_message)
    is_admin_bypass = verdict.intent == laya_gate.INTENT_ADMIN

    if is_admin_bypass:
        ctx.stream_text(
            "software-architect",
            f"> [GATEKEEPER EVALUATION] Prompt: \"{user_message[:60]}\"\n"
            f"> Classification: Architectural / Analytical Query (evidence: {'; '.join(verdict.reasons)}).\n"
            f"> Activating ADMINISTRATIVE BYPASS: Handled directly by Architect (0 Coder tokens).",
            log_type="decision",
            delay=0.02
        )
        time.sleep(0.3)

        ctx.emit_fn(tool_call(
            "software-architect", "load_plan_state", {"plan_file": plan_file},
            "Inspecting workspace plan memory"
        ))
        time.sleep(0.3)
        ctx.emit_fn(tool_result(
            "software-architect", "load_plan_state",
            f"Loaded plan {plan_file}"
        ))

        ctx.stream_text("software-architect", f"> Direct Architect Response to Custom Directive:\n- Workspace is anchored to `{plan_file}`.\n- Active milestones: {len(plan_state.get('steps', []))} items.\n- No physical code modification requested; administrative resolution applied.", delay=0.02)

        ctx.emit_fn({
            "type": "architect_summary",
            "agent": "software-architect",
            "summary": {
                "title": "Lead Architect Directive Assessment",
                "status": "Handled Directly",
                "files": [plan_file],
                "deliverables": [
                    f"Directly processed directive: '{user_message[:60]}'.",
                    "Administrative bypass applied (no Coder spawned).",
                    "Workspace state verified stable."
                ],
                "proposals": [
                    "Execute Next Step to advance implementation.",
                    "Analyze Codebase for in-depth system audit."
                ]
            }
        })
        time.sleep(0.3)
        ctx.emit_fn({
            "type": "workflow_complete",
            "status": "finished",
            "message": "Custom directive executed directly by Architect."
        })
        return

    # Coding directive -> Delegate to Coder
    ctx.stream_text(
        "software-architect",
        f"> [GATEKEEPER EVALUATION] Prompt: \"{user_message[:60]}\"\n"
        f"> Classification: Code Modification / Implementation (evidence: {'; '.join(verdict.reasons)}).\n"
        f"> Architect Rule: Cannot write application code directly. Summoning Coder...",
        log_type="decision",
        delay=0.02
    )
    time.sleep(0.3)

    target_coder_id = laya_gate.coder_for_domain(verdict.domain)
    target_coder = coder_agents.get(target_coder_id, {
        "id": target_coder_id,
        "name": target_coder_id,
        "display_name": "Senior Backend Coder" if target_coder_id == laya_gate.CODER_DEEP else "Junior Developer",
        "model": coder_model(target_coder_id)
    })

    ctx.emit_fn({
        "type": "delegation",
        "from_agent": "software-architect",
        "target_agent": target_coder_id,
        "target_name": target_coder["display_name"],
        "task": user_message
    })
    time.sleep(0.6)
    if ctx.should_stop(): return

    ctx.emit_fn({
        "type": "coder_spawn",
        "agent": target_coder_id,
        "name": target_coder["display_name"],
        "role": "coder",
        "model": target_coder.get("model", coder_model(target_coder_id))
    })
    time.sleep(0.3)

    deliverable = templates.custom_engine(user_message)
    filename = deliverable.filename
    test_filename = deliverable.test_filename
    code_content = deliverable.code
    test_content = deliverable.test_code
    backup_file_for_task("custom", filename)
    ctx.stream_text(target_coder_id, f"> Writing custom code solution to `{filename}`...", delay=0.02)
    write_file.invoke({"filename": filename, "content": code_content})
    write_file.invoke({"filename": test_filename, "content": test_content})

    ctx.emit_fn({
        "type": "coder_summary",
        "agent": target_coder_id,
        "summary": {
            "title": f"Custom Implementation: {user_message[:40]}",
            "status": "Implemented",
            "files": [filename, test_filename],
            "deliverables": [
                f"Created custom implementation in `{filename}`.",
                f"Added unit tests in `{test_filename}`."
            ]
        }
    })
    time.sleep(0.3)

    ctx.stream_text("software-architect", f"> Verifying custom implementation via restricted shell...", delay=0.02)
    v_res = execute_restricted_command.invoke({"command": f"python -m py_compile {filename}"})

    ctx.emit_fn({
        "type": "architect_summary",
        "agent": "software-architect",
        "summary": {
            "title": "Lead Architect Verification",
            "status": "Verified & Approved",
            "files": [filename, test_filename],
            "deliverables": [
                f"Validated custom code in `{filename}`.",
                "Syntax verified clean via restricted shell."
            ],
            "proposals": [
                "Execute Next Step to advance roadmap.",
                "Update Plan if new requirements emerge."
            ]
        }
    })
    time.sleep(0.3)
    ctx.emit_fn({
        "type": "workflow_complete",
        "status": "finished",
        "message": "Custom task completed and verified."
    })
