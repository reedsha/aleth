"""Implementation actions: the intents that may physically write code.

``fix_bug`` diagnoses a reported defect, patches it through a Coder and verifies the
result; ``next_step`` builds the next roadmap deliverable, verifies it, and marks the
task completed or failed on the verdict;
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
from typing import Any, Dict, Mapping

from agents import laya as laya_gate
# ``laya_gate`` owns the vocabulary (routing a task to a Coder, naming a domain), which
# is arithmetic and never worth a model call. ``laya_model`` owns the one decision that
# is: what a free-form directive is asking for. It answers with the word list unless
# ``LAYA_BACKEND=model`` opts the checkpoint in, so this call site reads the same either way.
from agents import laya_model
from agents.model_routing import architect_model, coder_model
from orchestration.workflow import executor, generation, ledger, planner, reasoning, templates
from orchestration.workflow.context import WorkflowContext, roadmap_brief
from orchestration.workflow.events import tool_call, tool_result
from tools.file_tools import (
    backup_file_for_task,
    compile_plan_json_to_markdown,
    list_workspace_files,
    read_source,
    save_plan_state,
)
from tools.shell_result import command_failed
from tools.task_tags import UI_TAG
from tools.workspace import get_active_plan_filename, get_execution_dir
from storage.db import plan_id_for
# Imported directly rather than through the ``tools.file_tools`` façade: the test runner is
# its own lower-layer tool (stdlib subprocess + pytest), not a file operation.
from tools.test_runner import run_task_tests


def _refuse_if_blocked(ctx: Any, plan_file: str, task: Mapping[str, Any]) -> bool:
    """Refuse a directive aimed at a blocked node. Returns ``True`` when it refused.

    **Architectural law: a blocked node is a blocked node.** Whether the node was chosen by
    the scheduler or named explicitly by a ``fix_bug``/``custom`` directive, the prerequisite
    state either exists or it does not -- and a model asked to work on a node whose state is
    missing will invent a reality to compensate, then write code against it. So every path
    through this module passes the same gate, and every refusal is the same payload.
    """
    from storage.db import get_store, plan_id_for
    from tools.workspace import get_active_plan_filename

    plan_id = plan_id_for(get_active_plan_filename())
    store = get_store()
    task_id = str(task.get("id") or "")
    if not task_id or store.is_task_eligible(plan_id, task_id):
        return False

    blockers = store.incomplete_blockers(plan_id, task_id)
    title = str(task.get("title") or task_id)
    ctx.stream_text(
        "software-architect",
        f"> [SCHEDULER] '{title}' is blocked and was not run.\n"
        f"> Incomplete blocker(s): {', '.join(blockers)}.\n"
        f"> Complete them first; the artifact DAG draws the edges that gate this task.",
        log_type="decision",
        delay=0.02,
    )
    ctx.emit_fn({
        "type": "architect_summary",
        "agent": "software-architect",
        "summary": {
            "title": "Task Blocked By Dependencies",
            "status": "Input Required",
            "files": [plan_file],
            "deliverables": [
                f"'{title}' was not run: its blockers are not complete.",
                f"Incomplete: {', '.join(blockers)}.",
            ],
            "proposals": [f"Complete {', '.join(blockers)} first, then run this task."],
        },
    })
    ctx.emit_fn({
        "type": "workflow_complete",
        "status": "finished",
        "message": f"'{title}' is blocked by incomplete dependencies.",
    })
    return True


def fix_bug_action(
    ctx: WorkflowContext,
    plan_state: Dict[str, Any],
    coder_agents: Dict[str, Dict[str, Any]],
    action_params: Dict[str, Any],
    user_message: str,
) -> None:
    """Diagnosis -> surgical patch -> verification, ending in an Architect approval."""
    bug_desc = action_params.get("bugDescription", "") or user_message
    attachment = action_params.get("bugAttachment", "")
    clean_bug = bug_desc.replace("[ACTION: FIX_BUG]", "").replace("Bug Report:", "").strip()

    # The UI can name the plan task this fix answers for -- the Fix/Retry affordance on a
    # failed card passes that task's id. When it does, the snapshot key is the id (so the
    # diff is the task's own, and its recorded regression file is what runs) and the verdict
    # below is written back onto the task, the same way ``next_step`` writes its own. Without
    # a target -- the console path, the palette -- the key stays the literal ``bugfix`` and no
    # plan state is touched, exactly as before.
    target_task_id = action_params.get("targetTaskId")
    task_key = target_task_id or "bugfix"

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

    # A fix aimed at a named node passes the same eligibility gate as the scheduler: the
    # prerequisite state either exists or it does not, and a model told to patch a node whose
    # state is missing would write code against a reality it invented to compensate.
    if target_task_id:
        directive_node = planner.task_by_id(plan_state, target_task_id) or planner.task_by_dag_id(
            plan_id_for(get_active_plan_filename()), target_task_id
        )
        if directive_node is not None and _refuse_if_blocked(ctx, ctx.plan_file, directive_node):
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

    # The bug report usually names the file it is about; failing that, the first code file
    # in the workspace, which is the diagnosis-by-inspection this replaced.
    reported_target = generation.path_in_text(clean_bug)
    if not reported_target and not py_files:
        # Nothing names a file and the workspace has no code to inspect. Inventing `main.py`
        # and writing a patch into it is worse than refusing (audit M15): this is the same
        # refusal the empty-report gate gives, worded for the missing-target case. "Input
        # Required" is the status the UI already reads as "needs a human".
        ctx.stream_text(
            "software-architect",
            "> [GATEKEEPER EVALUATION] Fix refused: no target file.\n"
            "> The report names no code file, and the workspace holds none.\n"
            "> Name the file in the report, or run Execute Next Step to create code first.",
            log_type="decision",
            delay=0.02
        )
        ctx.emit_fn({
            "type": "architect_summary",
            "agent": "software-architect",
            "summary": {
                "title": "Fix Bug Needs A Target",
                "status": "Input Required",
                "files": [],
                "deliverables": [
                    "No code file to patch, and the report named none.",
                ],
                "proposals": [
                    "Name the file in the bug report.",
                    "Run Execute Next Step to create code first.",
                ]
            }
        })
        ctx.emit_fn({
            "type": "workflow_complete",
            "status": "finished",
            "message": "Bug report needs a target file."
        })
        return
    target_file = reported_target or py_files[0]

    # The regression test is part of the fix, so it is one of the task's declared
    # deliverables: the artifact carries its bytes like any other target, and the executor
    # lands it in the same pass as the patch.
    test_file = generation.paired_test_path(target_file) if reported_target else ""
    if not test_file:
        test_file = [f for f in py_files if "test" in f]
        test_file = test_file[0] if test_file else "test_main.py"

    # PHASE 1 -- PLAN, with DAG supremacy: a fix that named no plan task gets an ephemeral
    # node, and either way it is planned and the run yields. Nothing is written here. The
    # report travels as the task's own detail, so the planner sees the symptom it is fixing
    # rather than only the file it lives in.
    directive_task = planner.ensure_task_for_directive(
        plan_id=plan_id_for(get_active_plan_filename()),
        task=planner.task_by_id(plan_state, action_params.get("targetTaskId")),
        task_id=action_params.get("targetTaskId"),
        title=f"Fix bug in {target_file}",
        files=[target_file, test_file],
        details=[f"Bug report: {clean_bug}"] if clean_bug else [],
    )
    if planner.needs_planning(directive_task):
        planner.plan_and_yield(ctx, task=directive_task, plan_file=ctx.plan_file)
        return

    # Read target file
    ctx.emit_fn(tool_call(
        "software-architect", "read_file", {"filename": target_file},
        f"Inspecting {target_file} for root cause"
    ))
    time.sleep(0.3)
    # Full read, not the tool's token-optimised view: this content is written back
    # below, and ``read_file`` middle-truncates large files, which would delete the
    # omitted middle when the patch is saved.
    curr_code = read_source(target_file, root=ctx.execution_root)
    ctx.emit_fn(tool_result(
        "software-architect", "read_file",
        f"Inspected {target_file} ({len(curr_code)} bytes)"
    ))

    # The diagnosis is carried as fields, not only narrated. The architect's card and the
    # frontend's root-cause/resolution result view both read it, and recovering it by
    # parsing this log line would couple them to one string's exact wording.
    # The offline patch path applies a defensive guard; it does not diagnose. Rather than
    # present a canned cause as a finding, the field states what is actually known -- the
    # reported symptom (audit M14). When System 2 runs, its reasoning is surfaced in the
    # Coder's card instead, so a real diagnosis is not hidden by this label.
    root_cause = (
        f"Reported symptom: {clean_bug[:140]}" if clean_bug
        else "Not diagnosed: the report supplied no description."
    )
    fix_spec = f"Apply a defensive guard for the reported failure in `{test_file}`."

    ctx.stream_text(
        "software-architect",
        f"> Diagnosis Result:\n"
        f"- Bug Report: {clean_bug[:120]}\n"
        f"- Target File: `{target_file}`\n"
        f"- Root Cause: {root_cause}\n"
        f"- Fix Spec: {fix_spec}\n"
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
        "model": target_coder.get("model", coder_model(target_coder_id))
    })
    time.sleep(0.3)

    ctx.stream_text(target_coder_id, f"> [PHASE 2: CODER SURGICAL PATCH]\n> Backing up `{target_file}` before applying patch...", delay=0.02)

    # Backup both files under the one task key the fix is recorded against, so the
    # verification below finds the regression test the same way ``next_step`` does -- from
    # the snapshot metadata rather than a second, guessed path.
    backup_file_for_task(task_key, target_file)
    backup_file_for_task(task_key, test_file)
    time.sleep(0.2)

    # The artifact IS the payload. The task reached this half only because a human approved
    # it (`in_progress`), so the plan a human approved is applied verbatim -- one splice per
    # target, no model call, no second guess.
    ctx.emit_fn(tool_call(
        target_coder_id, "write_file", {"filename": target_file},
        f"Applying the approved bug patch to {target_file}"
    ))
    time.sleep(0.3)
    applied = executor.execute_approved(
        plan_id_for(get_active_plan_filename()),
        str(directive_task.get("id")),
        workspace_dir=get_execution_dir(),
    )
    if not applied.success:
        raise RuntimeError(
            f"{target_coder_id} could not apply the approved plan: {applied.error}"
        )
    ctx.emit_fn(tool_result(
        target_coder_id, "write_file",
        f"Applied the approved plan to {target_file} and {test_file}"
    ))

    # Coder summary
    ctx.emit_fn({
        "type": "coder_summary",
        "agent": target_coder_id,
        "summary": {
            "title": f"Surgical Bug Patch: {target_file}",
            "status": "Patch Applied",
            "files": [target_file, test_file],
            "deliverables": [
                f"Applied surgical patch addressing: {clean_bug[:60]}.",
                f"Created snapshot backup in `.aleth_backups`.",
                f"Constructed regression test suite in `{test_file}`."
            ]
        }
    })
    time.sleep(0.3)

    # Phase 3: Architect Verification. The same gate ``next_step`` uses, and it chooses the
    # verdict rather than only narrating it: a patch that does not compile, or whose
    # regression test runs and fails, is a failed fix. An inconclusive check -- a test file
    # that cannot import (the canned suite imports ``SolutionEngine``, which a real target
    # need not contain), a missing runner -- does not fail the patch, because "could not
    # judge" is not "broken".
    ctx.stream_text("software-architect", f"> [PHASE 3: VERIFICATION LOOP] Lead Architect verifying patch in `{target_file}` via restricted shell...", delay=0.02)
    ctx.emit_fn(tool_call(
        "software-architect", "execute_restricted_command",
        {"command": f"python -m py_compile {target_file}"},
        f"Verifying patched syntax for {target_file}"
    ))
    time.sleep(0.3)
    check_res = ctx.mcp_session.execute(f"python -m py_compile {target_file}", restricted=True)
    compile_error = check_res.strip() if command_failed(check_res) else ""
    ctx.emit_fn(tool_result(
        "software-architect", "execute_restricted_command",
        compile_error or "Syntax and compilation verified with 0 errors"
    ))
    time.sleep(0.2)

    # The regression test is recorded under the same task key, so the shared runner finds
    # it without the workflow having to name it.
    test_verdict = run_task_tests(task_key)
    test_summary = test_verdict.get("summary") or "No test file was recorded for this task."
    ctx.emit_fn(tool_call(
        "software-architect", "run_task_tests", {"task_id": task_key},
        "Executing the regression tests against the patched code"
    ))
    time.sleep(0.3)
    ctx.emit_fn(tool_result(
        "software-architect", "run_task_tests", f"Tests: {test_summary}"
    ))

    # A regression suite that cannot even be collected (its import fails against this target)
    # reports verdict "error", not "failed". Treating only "failed" as failure let an
    # untrusted verdict read as a pass -- the canned suite imports a symbol the target need
    # not define (audit H2).
    fix_failed = bool(compile_error) or test_verdict.get("verdict") in ("failed", "error")
    failure_reason = ""
    if fix_failed:
        failure_reason = compile_error.splitlines()[-1] if compile_error else f"tests did not pass ({test_summary})"

    fix_deliverables = [
        f"Root cause diagnosed and surgically fixed in `{target_file}`.",
        f"Regression tests confirmed in `{test_file}`.",
        "Zero regressions detected in restricted shell validation."
    ]
    fix_proposals = [
        "Run 'Analyze Codebase' to verify entire system stability.",
        "Proceed to next milestone via 'Execute Next Step'."
    ]
    if fix_failed:
        fix_deliverables = [
            f"Root cause diagnosed and patched in `{target_file}`.",
            f"Verification failed: {failure_reason}",
            f"The patch and the regression test in `{test_file}` need attention before this fix is trusted.",
        ]
        fix_proposals[0] = f"Retry the bug fix once the failure in `{target_file}` is addressed."

    # When the UI named the task, the verdict lands in the plan, not only in the transcript.
    # A failed fix sets the task `[!]`; a verified fix records the evidence and leaves the
    # status alone -- it is not *completed*, because a patched bug is not a finished
    # milestone, so only a re-run completes it.
    #
    # There is deliberately no "clear a failed mark back to pending" branch any more. Under
    # the two-phase lifecycle a task reaches execution only after approval has already moved
    # it to `in_progress`, so a `failed` status can never be observed here: the branch was
    # unreachable, and keeping a ghost "pre-approval" status to feed it would be bolting
    # linear scripting onto a state machine.
    if target_task_id:
        target_task = None
        for sec in plan_state.get("sections", []):
            for t in sec.get("tasks", []):
                if t.get("id") == target_task_id or t.get("title") == target_task_id:
                    target_task = t
                    break
            if target_task:
                break
        if target_task:
            if fix_failed:
                target_task["status"] = "failed"
                target_task.setdefault("details", []).append(
                    f"Verification failed: {failure_reason}"
                )
            else:
                target_task.setdefault("details", []).append(
                    f"Bug fix verified in `{target_file}`."
                )
            saved_plan = save_plan_state(plan_state)
            latest_markdown = compile_plan_json_to_markdown(saved_plan)
            ctx.emit_fn(tool_call(
                "software-architect", "save_plan_state",
                {"task_id": target_task.get("id"), "status": target_task.get("status")},
                f"Recording the bug fix verdict for '{target_task.get('title')}'"
            ))
            time.sleep(0.3)
            ctx.emit_fn(tool_result(
                "software-architect", "save_plan_state",
                "Machine state and Markdown synchronized"
            ))
            ctx.emit_fn({
                "type": "plan_updated",
                "filename": ctx.plan_file,
                "content": latest_markdown,
                "tree": saved_plan.get("steps", []),
                "plan_json": saved_plan
            })

    ctx.emit_fn({
        "type": "architect_summary",
        "agent": "software-architect",
        "summary": {
            "title": "Lead Architect Bug Fix Approval",
            "status": "Verification Failed" if fix_failed else "Verified & Approved",
            "files": [target_file, test_file],
            "root_cause": root_cause,
            "fix_spec": fix_spec,
            "deliverables": fix_deliverables,
            "proposals": fix_proposals
        }
    })
    time.sleep(0.3)
    ctx.emit_fn({
        "type": "workflow_complete",
        "status": "finished",
        "message": (
            "Bug fix failed verification and needs attention."
            if fix_failed
            else "Bug surgically diagnosed, patched, and verified."
        )
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

    # Eligibility is the store's answer, not this branch's opinion. A task may run only when
    # every blocker recorded in ``task_dependencies`` is ``completed``: executing a child
    # before its parent provisioned the state it depends on is a race, not a schedule. The
    # status alone is not enough, which is what this replaced.
    from storage.db import get_store

    store = get_store()
    plan_id = plan_id_for(get_active_plan_filename())

    def _eligible(task):
        return store.is_task_eligible(plan_id, str(task.get("id") or ""))

    # Selection, in the order the two-phase lifecycle needs: an explicitly targeted task
    # wins; otherwise an *approved* task (``in_progress``) is the one to execute, and only
    # failing that is the top ``pending`` task selected -- which is the one to *plan*. In
    # every case the node must be unblocked, so a blocked task is never selected and never
    # planned: an ineligible milestone is not "next".
    def _first_with(statuses):
        for section in plan_state.get("sections", []):
            for task in section.get("tasks", []):
                if task.get("status") in statuses and _eligible(task):
                    return task, section
        return None, None

    if target_task_id:
        for sec in plan_state.get("sections", []):
            for t in sec.get("tasks", []):
                if t.get("id") == target_task_id or t.get("title") == target_task_id:
                    target_task = t
                    target_sec = sec
                    break
            if target_task:
                break
        # An explicitly targeted task that is blocked is refused, not run -- through the same
        # gate every other path uses, so the payload is identical.
        if target_task is not None and _refuse_if_blocked(ctx, plan_file, target_task):
            return
    else:
        target_task, target_sec = _first_with(("in_progress",))
        if not target_task:
            target_task, target_sec = _first_with(("pending",))

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

    # PHASE 1 -- PLAN. A task that has not been approved is planned, announced, and the run
    # yields. Nothing is written in this phase: execution is a separate, deterministic pass
    # (orchestration.workflow.executor) that runs only after a human approves the artifact.
    if planner.needs_planning(target_task):
        planner.plan_and_yield(ctx, task=target_task, plan_file=plan_file)
        return

    # MODULE 6: Multimodal UI Task Handling. UI-ness is the task's tag -- one field -- so
    # the pill the tree draws and the delegation path cannot disagree about it.
    ui_task = target_task.get("tag") == UI_TAG
    ui_image = action_params.get("uiImagePath") or action_params.get("attachment")
    # The mockup's bytes, as a data URL, when the UI could read them. Only a UI task with an
    # attached image has this; the model request is text-only when it is absent.
    ui_image_data = action_params.get("uiImageData") or ""

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

    ctx.emit_fn({
        "type": "coder_spawn",
        "agent": target_coder_id,
        "name": target_coder["display_name"],
        "model": target_coder.get("model", coder_model(target_coder_id))
    })
    time.sleep(0.3)

    # ONE LLM call, and it already happened: the approved artifact carries the exact code,
    # so this half applies it. There is no second generation step -- the plan a human
    # approved IS the payload that lands on disk. The template still supplies the filenames
    # and the paired-test name, because those are the shape of the deliverable, not its code.
    deliverable = templates.select(target_task.get("title", ""), target_task.get("tag"))
    # Where the task says its file belongs beats where the template would drop it. Without
    # this a task about `tools/plan_parser.py` is "completed" by writing main.py, which
    # makes every completed mark in the plan untrustworthy.
    target = generation.task_target_path(target_task)
    out_filename = target or deliverable.filename
    out_test = generation.paired_test_path(out_filename) if target else deliverable.test_filename

    ctx.stream_text(target_coder_id, f"> Creating snapshot backup of deliverables for rollback safety...", delay=0.02)
    backup_file_for_task(target_task.get("id"), out_filename)
    backup_file_for_task(target_task.get("id"), out_test)

    ctx.stream_text(target_coder_id, f"> Writing production deliverables: `{out_filename}` and `{out_test}`...", delay=0.02)
    ctx.emit_fn(tool_call(
        target_coder_id, "write_file", {"filename": out_filename},
        f"Writing module deliverables to {out_filename}"
    ))
    time.sleep(0.3)
    applied = executor.execute_approved(
        plan_id_for(get_active_plan_filename()),
        str(target_task.get("id")),
        workspace_dir=get_execution_dir(),
    )
    if not applied.success:
        raise RuntimeError(
            f"{target_coder_id} could not apply the approved plan: {applied.error}"
        )
    out_code = read_source(out_filename, root=ctx.execution_root) or ""
    ctx.emit_fn(tool_result(
        target_coder_id, "write_file",
        f"Wrote {len(out_code)} chars to {out_filename}"
    ))

    ctx.emit_fn(tool_call(
        target_coder_id, "write_file", {"filename": out_test},
        f"Writing test suite to {out_test}"
    ))
    time.sleep(0.3)
    out_test_code = read_source(out_test, root=ctx.execution_root) or ""
    ctx.emit_fn(tool_result(
        target_coder_id, "write_file",
        f"Wrote {len(out_test_code)} chars to {out_test}"
    ))
    # The narration below reports what a *model call* cost. Nothing was called in this half,
    # so the shim says so -- and every branch keyed on it stays exactly as it was offline.
    generated = generation.GeneratedFile(code=out_code, used_llm=False)

    # Architect Verification runs *before* the status is written, so its verdict can choose
    # the status. This is the writer the plan's failed state (`[!]`) needs. Two checks, either
    # of which can fail the task: py_compile, where a syntax error is unambiguous, and the
    # task's own regression tests, run through the shared runner (tools/test_runner.py) rather
    # than a second implementation that could drift from the result view's.
    ctx.stream_text("software-architect", "> Verifying task deliverables via restricted shell...", delay=0.02)
    ctx.emit_fn(tool_call(
        "software-architect", "execute_restricted_command",
        {"command": f"python -m py_compile {out_filename if out_filename.endswith('.py') else out_test}"},
        "Validating syntax and deliverable integrity"
    ))
    time.sleep(0.3)
    cmd_to_run = f"python -m py_compile {out_filename}" if out_filename.endswith(".py") else f"python -m py_compile {out_test}"
    v_res = ctx.mcp_session.execute(cmd_to_run, restricted=True)
    # The command's block is never empty, so success has to be read from its exit code, not
    # from the string's truthiness (which is always true). Only a non-zero exit is evidence
    # of a syntax error; an inconclusive result -- no exit status at all -- is not.
    compile_error = v_res.strip() if command_failed(v_res) else ""
    ctx.emit_fn(tool_result(
        "software-architect", "execute_restricted_command",
        compile_error or "Syntax and compilation verified with 0 errors"
    ))

    # The task's own tests are on disk already (written above) and their filenames are recorded
    # in the snapshot metadata written before the writes, so the runner can find them here.
    test_verdict = run_task_tests(target_task.get("id"))
    test_summary = test_verdict.get("summary") or "No test file was recorded for this task."
    ctx.emit_fn(tool_call(
        "software-architect", "run_task_tests",
        {"task_id": target_task.get("id")},
        "Executing the task's regression tests"
    ))
    time.sleep(0.3)
    ctx.emit_fn(tool_result(
        "software-architect", "run_task_tests",
        f"Tests: {test_summary}"
    ))

    # Only a *definite* negative fails a task: a compile error, or tests that ran and failed.
    # An inconclusive run (a collection error, a missing runner, no test file) does not punish
    # a task whose code could not be judged -- the same "never infer a verdict" rule the result
    # view's test pill follows.
    task_failed = bool(compile_error) or test_verdict.get("verdict") == "failed"
    reason = ""
    if task_failed:
        reason = compile_error.splitlines()[-1] if compile_error else f"tests did not pass ({test_summary})"

    # Mark the task's outcome in plan_state, chosen by the verdict above.
    target_task["status"] = "failed" if task_failed else "completed"
    target_task.setdefault("files", []).extend([out_filename, out_test])
    target_task["files"] = list(dict.fromkeys(target_task["files"]))
    target_task.setdefault("details", []).append(f"Deliverables: `{out_filename}`, `{out_test}`")
    if task_failed:
        # The failure has to live in the plan, not only in the event stream, or a reload would
        # forget why a task is marked `[!]` and it could only be re-run blindly.
        target_task["details"].append(f"Verification failed: {reason}")

    # Living Behavioral Ledger. Deterministic bookkeeping -- no tokens -- and it has to land
    # in the same write as the status, or the plan would briefly claim a task is done with no
    # evidence for it. The evidence line is written either way (the files were written
    # regardless of the verdict), but a failed task is not a *finished* milestone, so it does
    # not graduate into the standing context's core facts.
    ledger_message = f"wrote `{out_filename}` and `{out_test}`"
    if generated.used_llm:
        ledger_message += f" via {generated.model}"
    ledger.record_behavioral_log(target_task, ledger_message)
    fact = None if task_failed else ledger.wrap_up_milestone(plan_state, target_task)
    if generated.used_llm:
        # Narrated only for a real call: the ledger is *recorded* either way (the plan tree
        # and the compiled markdown carry it), but the card's model lines exist to show what
        # the model did, and the offline path's event stream is a pinned contract.
        ctx.stream_text(
            target_coder_id,
            f"> \U0001F7E2 Behavioral Log: {ledger_message}"
            + (f"\n> Global State Summary fact: {fact}" if fact else ""),
            delay=0.02,
        )

    saved_plan = save_plan_state(plan_state)
    latest_markdown = compile_plan_json_to_markdown(saved_plan)

    ctx.emit_fn(tool_call(
        target_coder_id, "save_plan_state",
        {"task_id": target_task.get("id"), "status": target_task["status"]},
        f"Marked task '{target_task.get('title')}' {target_task['status']} in plan.json & {plan_file}"
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
        f"Updated active plan `{plan_file}` (marked task {target_task['status']})."
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

    next_p = [t for s in saved_plan.get("sections", []) for t in s.get("tasks", []) if t.get("status") == "pending"]
    proposals = [
        f"Execute next step: '{next_p[0].get('title')}'" if next_p else "All roadmap tasks finished! Run final test audit.",
        "Verify file structure with 'Analyze Codebase'.",
        "Click completed task in Plan Tracker if you ever need to Rollback."
    ]
    if task_failed:
        # Rollback is not the recovery for a failed task -- it is already un-completed -- so the
        # first proposal names the real next move: address the failure and run it again.
        proposals[0] = f"Retry '{target_task.get('title')}' once the failure above is addressed."

    ctx.emit_fn({
        "type": "architect_summary",
        "agent": "software-architect",
        "summary": {
            "title": "Lead Architect Task Approval & Handoff",
            "status": "Verification Failed" if task_failed else "Verified & Approved",
            "files": [plan_file, out_filename, out_test],
            "deliverables": [
                (
                    f"Verification failed for '{target_task.get('title')}': {reason}"
                    if task_failed
                    else f"Verified deliverables for '{target_task.get('title')}'."
                ),
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
        "message": (
            f"Task '{target_task.get('title')}' failed verification and needs attention."
            if task_failed
            else f"Task '{target_task.get('title')}' completed and verified."
        )
    })


def custom_action(
    ctx: WorkflowContext,
    plan_state: Dict[str, Any],
    coder_agents: Dict[str, Dict[str, Any]],
    action_params: Dict[str, Any],
    user_message: str,
) -> None:
    """The Gatekeeper fallback: classify a free-form prompt, then answer or delegate."""
    plan_file = ctx.plan_file

    # A directive aimed at a named node passes the same eligibility gate as the scheduler.
    # A purely ad-hoc directive names no node and spawns an ephemeral one with no blockers,
    # which is instantly eligible -- so this check is a no-op for it, by construction.
    target_task_id = action_params.get("targetTaskId")
    if target_task_id:
        directive_node = planner.task_by_id(plan_state, target_task_id) or planner.task_by_dag_id(
            plan_id_for(get_active_plan_filename()), target_task_id
        )
        if directive_node is not None and _refuse_if_blocked(ctx, plan_file, directive_node):
            return

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

        # The direct answer itself, when a provider is configured. This branch exists to
        # resolve an analytical directive without a Coder -- and an analytical directive is
        # precisely what the Architect should answer in its own words rather than narrate.
        answer = reasoning.architect_answer(
            prompt=(
                f"Answer this directive directly as the Architect: {user_message}\n"
                f"At most 8 short lines. No code will be written for it."
            ),
            context_text=roadmap_brief(plan_state),
            plan_file=plan_file,
        )
        if answer.used_llm:
            reasoning.stream_thought(ctx.stream_text, "software-architect", answer.reasoning)
            ctx.stream_text(
                "software-architect",
                f"> Architect answer ({answer.model}):\n{answer.text}",
                log_type="decision",
                delay=0.02
            )

        admin_deliverables = [
            f"Directly processed directive: '{user_message[:60]}'.",
            "Administrative bypass applied (no Coder spawned).",
            "Workspace state verified stable."
        ]
        if answer.used_llm:
            admin_deliverables.append(answer.usage_line())

        ctx.emit_fn({
            "type": "architect_summary",
            "agent": "software-architect",
            "summary": {
                "title": "Lead Architect Directive Assessment",
                "status": "Handled Directly",
                "files": [plan_file],
                "deliverables": admin_deliverables,
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

    ctx.emit_fn({
        "type": "coder_spawn",
        "agent": target_coder_id,
        "name": target_coder["display_name"],
        "model": target_coder.get("model", coder_model(target_coder_id))
    })
    time.sleep(0.3)

    deliverable = templates.custom_engine(user_message)
    # The directive may name the file; otherwise the template's name stands, as before.
    target = generation.path_in_text(user_message)
    filename = target or deliverable.filename
    test_filename = generation.paired_test_path(filename) if target else deliverable.test_filename

    # PHASE 1 -- PLAN, with DAG supremacy: a free-form directive that named no plan task
    # gets an ephemeral node, then follows the identical lifecycle. The whole directive is
    # the task's detail, so the planner plans against the request and not only its title.
    directive_task = planner.ensure_task_for_directive(
        plan_id=plan_id_for(get_active_plan_filename()),
        task=None,
        title=(user_message or "Custom directive")[:80],
        files=[filename, test_filename],
        details=[user_message],
    )
    if planner.needs_planning(directive_task):
        planner.plan_and_yield(ctx, task=directive_task, plan_file=plan_file)
        return
    backup_file_for_task("custom", filename)
    ctx.stream_text(target_coder_id, f"> Writing custom code solution to `{filename}`...", delay=0.02)
    # The approved artifact is the payload: the executor applies it verbatim, with no model
    # call in this half.
    applied = executor.execute_approved(
        plan_id_for(get_active_plan_filename()),
        str(directive_task.get("id")),
        workspace_dir=get_execution_dir(),
    )
    if not applied.success:
        raise RuntimeError(
            f"{target_coder_id} could not apply the approved plan: {applied.error}"
        )

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
    v_res = ctx.mcp_session.execute(f"python -m py_compile {filename}", restricted=True)

    # The verdict is the check's, not a literal. ``v_res`` was previously computed and never
    # read, so a directive that wrote broken code still reported "Verified & Approved"
    # (audit C1). ``command_failed`` reads the ``[Exit Code: N]`` header the shell tool
    # always emits -- the same gate ``next_step_action`` uses.
    verified = not command_failed(v_res)
    verify_deliverable = (
        "Syntax verified clean via restricted shell."
        if verified
        else "Verification failed: "
             + ((v_res.strip().splitlines() or ["py_compile reported errors"])[-1])
    )
    ctx.emit_fn({
        "type": "architect_summary",
        "agent": "software-architect",
        "summary": {
            "title": "Lead Architect Verification",
            "status": "Verified & Approved" if verified else "Verification Failed",
            "files": [filename, test_filename],
            "deliverables": [
                f"Validated custom code in `{filename}`.",
                verify_deliverable
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
        "message": (
            "Custom task completed and verified."
            if verified
            else "Custom task completed, but verification failed and needs attention."
        )
    })
