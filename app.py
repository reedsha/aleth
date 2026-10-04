import contextlib
import os
import sys
import threading
import traceback
import uuid
import webview

from env_boot import load_environment

# Load environment configuration. ``.env`` deliberately wins over the process
# environment; an exported name that shadowed it is reported rather than silently used.
for _shadowed in load_environment():
    print(f"[Config] .env overrides the exported {_shadowed}")

import bridge_bus
from orchestration.plan_tagging import retag_plan
from registry import registry
from tools.file_tools import (
    get_project_dir,
    set_project_dir,
    list_workspace_files,
    get_active_plan_filename,
    set_active_plan_filename,
    set_plan_dir,
    list_plan_files,
    parse_plan_tree,
    load_plan_state,
    save_plan_state,
    compile_plan_json_to_markdown,
    parse_markdown_to_plan_dict,
    audit_codebase_plan_sync,
    resolve_sync_plan_to_codebase,
    resolve_sync_code_to_plan,
    rollback_task_state,
    revert_plan_revision,
    task_diff,
    read_preview_source,
    read_environment_variables
)
from tools.run_context import get_current_intent, set_current_intent
from tools.settings import read_settings, save_settings as write_settings
from tools.plan_state import append_pending_task, plan_structure_report, write_plan_markdown
from tools.task_tags import UI_TAG
from tools.test_runner import run_task_tests as run_task_test_suite


# --- Frontend delivery -------------------------------------------------------
#
# The frontend is built by Vite into ``dist/`` and served by the API gateway itself, so the
# window is an ordinary browser pointed at ``http://127.0.0.1:<port>/index.html``.
#
# This replaced WebView2's virtual-host mapping. ``file://`` was the historical failure mode --
# WebView2 drops individual subresource fetches from a local document intermittently, and a lost
# module fetch left the window fully rendered but inert. Serving the bundle from the same socket
# that answers ``/api`` removes that class of failure *and* the two problems the mapping could
# never solve: the document's origin is now the API's origin (so no CORS exemption and no
# ``null`` origin), and the page needs no way to be told where the API lives -- it came from it.


def frontend_root() -> str:
    """The directory holding the frontend to serve: the Vite build output."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist")


def _output_tail(result, limit: int = 400) -> str:
    """The end of a failed command's output, for a refusal message. Never raises.

    The tail rather than the head: a compiler and a test runner both put the verdict at the end,
    and the head of an install log is progress bars.
    """
    try:
        combined = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
    except Exception:  # a result that is not an IsolatedResult (a test double)
        return str(result)
    if len(combined) <= limit:
        return combined
    return "..." + combined[-limit:]


class EngineService:
    """The engine-facing operations, reached over HTTP and never bound into the page.

    This used to be ``BridgeAPI``: a pywebview ``js_api`` object, which made every method here
    callable from JavaScript -- including the ones that execute. The API gateway
    (``api.operations``) is now its only caller, and pywebview never sees it: the window is a
    browser, and the page's only door to the engine is the loopback socket.
    """
    def __init__(self):
        self._window = None
        self._execution_thread = None
        self._tagging_thread = None
        # The server-authoritative run lock. The check ("is a run live?") and the claim (launch
        # one) must be one atomic act: two request threads passing the check together used to
        # both launch, and two workflows then wrote the same plan and files concurrently. Held
        # only across the decision and the thread spawn -- never across a run.
        self._run_lock = threading.Lock()
        # The local API gateway. Built on demand by ``start_api`` -- the test suite constructs
        # this service constantly and must not bind a socket doing it.
        self._api = None
        # The durable intent ledger, attached by ``start_api``. Kept here so the acknowledge
        # endpoint and the run loop reach the same record.
        self._ledger = None
        # The gateway's intent queue, injected by ``api.server.start_gateway`` when it mints the
        # queue (``attach_intent_queue``). The service owns the intent *operations*;
        # the queue lives with the gateway, so the two are joined by injection, never by reaching.
        self._intent_queue = None
        # The swarm, built on first use -- never in ``__init__``. Constructing the bridge must not
        # spawn a process pool or dispatch a node: the test suite builds one constantly, and a cold
        # tick there would fire the swarm at fixtures that have no planner.
        self._swarm = None
        # The detached-console window (a real second pywebview window) and the lines that
        # arrived before it finished loading. Touched from the workflow thread, the spawn
        # thread and pywebview's loaded/closed callbacks, so every access takes the lock.
        self._console_window = None
        self._console_backlog = []
        self._console_lock = threading.Lock()

    def set_window(self, window):
        """Bind the webview window, for the native dialogs and the detached console."""
        self._window = window

    def emit_event(self, event_data: dict):
        """Announces a structured event on the bus, which streams it to every client.

        The bus validates ``event_data`` against the strict event union before it is
        serialised, so a drifted payload is reported here rather than delivered. The
        failure is printed with its stack and swallowed: a dropped *terminal* event would
        leave the UI stuck in its running state, so this must never raise into the
        workflow thread (audit M8).
        """
        try:
            bridge_bus.emit(event_data)
        except Exception as e:
            traceback.print_exc()
            print(f"[EngineService] Error emitting event: {e}", file=sys.stderr)

    # -------------------------------------------------------------
    # Dynamic Agent Registry Methods
    # -------------------------------------------------------------
    def get_agents(self):
        """Fetch all categorized Main Agents and Coder Agents."""
        try:
            return registry.get_agent_summary()
        except Exception as e:
            return {"success": False, "error": str(e), "main_agents": [], "coder_agents": [],
                    "workspace_dir": get_project_dir()}

    def save_system_prompt(self, agent_id: str, new_prompt: str, is_custom_only: bool = False):
        """Persists updated system prompt or custom instructions to disk and reloads registry without restart."""
        try:
            result = registry.save_system_prompt(agent_id, new_prompt, is_custom_only=is_custom_only)
            self.emit_event({
                "type": "agents_updated",
                "agents": registry.get_agent_summary()
            })
            return result
        except Exception as e:
            return {"success": False, "error": str(e)}

    # -------------------------------------------------------------
    # Workspace Selection & Bug-Free Folder Dialog
    # -------------------------------------------------------------
    def select_workspace(self):
        """
        Opens native OS folder selection dialog to dynamically switch PROJECT_DIR.
        FIX: Guaranteed to close cleanly on first click of 'Cancel' or 'X'
        without reopening a secondary dialog.
        """
        selected_path = None
        dialog_performed = False

        try:
            if self._window and hasattr(self._window, "create_file_dialog"):
                dialog_performed = True
                result = self._window.create_file_dialog(
                    webview.FileDialog.FOLDER,
                    directory=get_project_dir()
                )
                if result and len(result) > 0:
                    selected_path = result[0]
                else:
                    # User explicitly cancelled the pywebview dialog -> exit cleanly on first click!
                    return {
                        "success": False,
                        "cancelled": True,
                        "workspace_dir": get_project_dir(),
                        "files": list_workspace_files(),
                        "plans": list_plan_files()
                    }
        except Exception as e:
            # Fallback only if pywebview dialog raised an unhandled exception
            dialog_performed = False

        if not dialog_performed:
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                path = filedialog.askdirectory(
                    title="Select Project Workspace Directory",
                    initialdir=get_project_dir()
                )
                root.destroy()
                if path:
                    selected_path = path
                else:
                    return {
                        "success": False,
                        "cancelled": True,
                        "workspace_dir": get_project_dir(),
                        "files": list_workspace_files(),
                        "plans": list_plan_files()
                    }
            except Exception as e:
                print(f"[EngineService] Dialog error: {e}", file=sys.stderr)

        if selected_path:
            abs_path = set_project_dir(selected_path)
            # The plan (PLAN.md + its plan.json twin) belongs to the project folder, so the
            # active plan -- and the plan switcher's list -- has to follow the workspace.
            # Without this the tree and the raw markdown kept rendering the previously
            # active folder's plan while the file explorer showed the new workspace, which
            # is exactly the split the workspace switcher must not create.
            set_plan_dir(abs_path)
            files = list_workspace_files()
            plans = list_plan_files()
            self.emit_event({
                "type": "workspace_changed",
                "workspace_dir": abs_path,
                "files": files,
                "plans": plans
            })
            return {
                "success": True,
                "workspace_dir": abs_path,
                "files": files,
                "plans": plans
            }

        return {
            "success": False,
            "cancelled": True,
            "workspace_dir": get_project_dir(),
            "files": list_workspace_files(),
            "plans": list_plan_files()
        }

    def get_workspace_info(self):
        """Return current workspace path, file tree, and available plan files."""
        return {
            "workspace_dir": get_project_dir(),
            "files": list_workspace_files(),
            "plans": list_plan_files(),
            "active_plan": get_active_plan_filename()
        }

    # -------------------------------------------------------------
    # Dynamic Plan Management API
    # -------------------------------------------------------------
    def get_plan_files(self):
        """Lists all .md plan files available in the workspace."""
        return list_plan_files()

    def get_active_plan(self):
        """Fetches the active plan state including plan.json machine state."""
        return registry.get_current_plan_data()

    def add_plan_task(self, title: str):
        """Adds a recommendation to the plan as a real pending task and persists it.

        The one-click half of the result view's "Add to Plan": the card's own click is
        the confirmation, so it does not route through the Update Plan drawer or a
        second, thinner plan endpoint. The write is the ordinary one -- append +
        ``save_plan_state`` + compile, then a ``plan_updated`` event -- so the tree, the
        workbench and the progress meter all re-render from it exactly as they do for any
        other save.
        """
        # Called at the call site: agents.laya reaches back into the workflow package, so
        # importing it at module scope here would close an import cycle. One vocabulary.
        from agents.laya import inferred_ui

        clean = (title or "").strip()
        if not clean:
            return {"success": False, "error": "A task needs a title."}

        plan_state = load_plan_state()
        task = append_pending_task(
            plan_state,
            clean,
            note="Added from an Architect recommendation.",
            tag=UI_TAG if inferred_ui(clean) else None,
        )
        saved = save_plan_state(plan_state)
        filename = get_active_plan_filename()
        content = compile_plan_json_to_markdown(saved)
        data = {
            "success": True,
            "filename": filename,
            "task_id": task.get("id"),
            "content": content,
            "tree": saved.get("steps", []),
            "plan_json": saved,
            "plans": list_plan_files()
        }
        self.emit_event({
            "type": "plan_updated",
            "filename": filename,
            "content": content,
            "tree": saved.get("steps", []),
            "plan_json": saved
        })
        return data

    def revert_plan_update(self):
        """Restores the roadmap to the state captured before the last plan revision.

        The honest counterpart to the auto-commit: an Update Plan run writes the plan as it
        finishes, so the result view cannot offer an "Approve" that gates the write. What it
        can offer is a one-click undo of that write, and this endpoint is it -- the plan
        files are restored from the snapshot taken immediately before the revision, then
        announced with the usual ``plan_updated`` so the tree and progress re-derive.
        """
        res = revert_plan_revision()
        if not res.get("success"):
            return res
        filename = get_active_plan_filename()
        data = {
            "success": True,
            "filename": filename,
            "content": res.get("content", ""),
            "tree": res.get("tree", []),
            "plan_json": res.get("plan_json", {}),
            "plans": list_plan_files()
        }
        self.emit_event({
            "type": "plan_updated",
            "filename": filename,
            "content": data["content"],
            "tree": data["tree"],
            "plan_json": data["plan_json"]
        })
        return data

    def retag_plan_with_laya(self):
        """
        Starts the offline [UI] re-tagging pass in a background thread.

        Deliberately off the render path: System 1 answers a title in one call, which
        costs zero tokens but is not instant, so the tags are decided here and persisted
        into plan.json and PLAN.md rather than re-decided on every draw. An explicit [UI]
        the author wrote is left alone.

        The pass takes on the order of a second per title, so it reports instead of
        blocking: progress and completion arrive on the same event channel every other
        long action uses, and the UI shows a panel while it runs. Returns at once.
        """
        if self._tagging_thread and self._tagging_thread.is_alive():
            return {"status": "already_running"}
        self._tagging_thread = threading.Thread(target=self._run_plan_tagging, daemon=True)
        self._tagging_thread.start()
        return {"status": "started"}

    def _run_plan_tagging(self):
        """The tagging pass's worker: emit progress, then the result and a new plan tree."""
        self.emit_event({"type": "laya_tagging_started"})
        try:
            result = retag_plan(
                on_progress=lambda done, total, title: self.emit_event(
                    {
                        "type": "laya_tagging_progress",
                        "done": done,
                        "total": total,
                        "title": title,
                    }
                )
            )
        except Exception as e:
            self.emit_event({"type": "laya_tagging_done", "success": False, "error": str(e)})
            return

        # Only the fields the UI reads cross the wire: the tagging result also carries totals,
        # dry-run flags and a before/after count map that no consumer uses (audit L2).
        self.emit_event({
            "type": "laya_tagging_done",
            "success": True,
            "changed": result.get("changed", []),
            "engine": result.get("engine", ""),
        })

        # The tags changed, so the tree, the pills and the workbench all need the new
        # payload. This is the same funnel every other plan write uses.
        if result.get("written"):
            data = registry.get_current_plan_data()
            self.emit_event({
                "type": "plan_updated",
                "filename": data["filename"],
                "content": data["content"],
                "tree": data["tree"],
                "plan_json": data.get("plan_json", {})
            })

    def set_active_plan(self, filename: str):
        """Switches the active plan file, re-hydrates state, and notifies the UI."""
        if self._execution_thread and self._execution_thread.is_alive():
            # A live run captured the old plan and writes it back at *save* time
            # (save_plan_state resolves the path then), so switching now would let the run
            # serialize the old plan over the newly-opened one (audit H5).
            return {"success": False, "error": "A run is in progress; switch plans after it finishes."}
        clean_name = set_active_plan_filename(filename)
        load_plan_state(force_sync=True)
        data = registry.get_current_plan_data()
        # NOTE: No emit_event here — the JS chip handler applies data directly from
        # the return value. A redundant plan_updated emit races and can revert the switch.
        return data

    def validate_plan_structure(self):
        """Read-only AST structure verdict for the active plan (Normalization Gate).

        The plan switcher calls this after a switch so an imported `.md` without `##`
        sections or `- [ ]` milestones is caught before it renders as a blank tree. It
        writes nothing, and a failure degrades to "structured" so a broken check can
        never block a plan the user actually wants to open.
        """
        try:
            report = plan_structure_report()
            # A verdict that was actually produced is marked as checked; the degrade path below
            # marks the opposite, so the two cannot be confused (audit M5).
            report.setdefault("checked", True)
            return report
        except Exception as e:
            return {"success": False, "structured": True, "checked": False, "issues": [],
                    "counts": {}, "summary": f"Structure check unavailable: {e}",
                    "error": str(e)}

    def normalize_plan(self):
        """Runs the Normalization Gate's reformat as an archived administrative workflow.

        Fire-and-forget, mirroring the action buttons: the workflow streams its progress
        as normal agent events and emits `plan_updated` when it rewrites the plan, so the
        UI needs no return value beyond the acknowledgement.
        """
        return self.start_execution(
            "Normalize the active plan into the strict AST structure",
            "normalize",
            {},
        )


    def extract_plan_steps(self, filename: str = None):
        """Forces extraction of plan steps from the active or specified plan file."""
        with self._run_lock:
            if self._execution_thread and self._execution_thread.is_alive():
                # The same guard as ``set_active_plan`` (audit H5): the active-plan pointer
                # resolves at *save* time, so moving it under a live run makes the run's next
                # save serialize the old plan's state over the new plan's document.
                return {
                    "success": False,
                    "reason": "busy",
                    "error": "A run is in progress; switch plans after it finishes.",
                }
            target_file = filename or get_active_plan_filename()
            clean_name = set_active_plan_filename(target_file)
        load_plan_state(force_sync=True)
        data = registry.get_current_plan_data()
        self.emit_event({
            "type": "plan_updated",
            "filename": clean_name,
            "content": data["content"],
            "tree": data["tree"],
            "plan_json": data.get("plan_json", {})
        })
        return {"success": True, "filename": clean_name, "steps_count": len(data.get("tree", []))}

    def create_plan_file(self, filename: str, project_idea: str):
        """Generates a structured plan file from user's idea and sets it active."""
        with self._run_lock:
            if self._execution_thread and self._execution_thread.is_alive():
                # Same guard as ``set_active_plan``: creating a plan switches the active-plan
                # pointer, and a live run saves against the pointer it resolves at save time.
                return {
                    "success": False,
                    "reason": "busy",
                    "error": "A run is in progress; switch plans after it finishes.",
                }
            res = registry.create_new_plan_file(filename, project_idea)
        self.emit_event({
            "type": "plan_updated",
            "filename": res["filename"],
            "content": res["content"],
            "tree": res["tree"],
            "plan_json": res.get("plan_json", {})
        })
        return res

    # -------------------------------------------------------------
    # Execution Lifecycle & Card Orchestration
    # -------------------------------------------------------------
    def _launch_run(self, user_message: str, action_type: str, action_params: dict, intent_id: str = "") -> None:
        """Start a workflow run on a daemon thread. The caller holds ``_run_lock``.

        Factored out of ``start_execution`` because approval launches a run too: an
        approved artifact is applied by re-issuing the request that proposed it, and that
        must obey the same lock and the same threading discipline as a user launch.

        ``intent_id`` is the execution identity the run's shadow is keyed to (Phase 21). A
        queued run passes its own; an approval passes the id of the run whose artifact it is
        releasing, so its writes land in that run's shadow rather than a new one.

        A caller with **no** id (a desktop launch) used to run with every intent-ledger gate
        inert -- no token budget, no cost ceiling, no liveness record. The id is now minted and
        written to the ledger (recorded and claimed, exactly as the queue does) so those gates
        bind on every run; when the bookkeeping itself cannot be written the run still proceeds
        ungated, because a lost record is an observer's loss, never the run's.
        """
        owned_intent = None
        if not str(intent_id or "").strip():
            intent_id, owned_intent = self._mint_run_intent(
                user_message, action_type, action_params
            )
        def target_runner():
            # Every run executes against a shadow of the workspace (Phase 20). A run launched
            # inside the autonomous loop finds the loop's shadow already active and reuses it; a
            # run launched on its own -- a manual approval -- reuses the shadow of the intent it
            # names, or gets one of its own. The `finally` tick runs while that shadow is still
            # the execution root, so a dispatch it triggers also lands in the shadow.
            try:
                with self._staged_execution(intent_id):
                    try:
                        registry.run_agent_workflow(
                            (user_message or "").strip(),
                            self.emit_event,
                            action_type=action_type or "custom",
                            action_params=action_params or {},
                            # The correlation id, threaded all the way to the agent loop (Phase 27).
                            intent_id=str(intent_id or ""),
                        )
                    finally:
                        # The run has ended, so whatever it completed has unblocked its children. The
                        # tick is deliberately *outside* the run: the artifact was written to disk by
                        # the pass, and no SQLite transaction is held across that I/O or this dispatch.
                        self._tick_swarm()
            finally:
                if owned_intent is not None:
                    self._settle_run_intent(owned_intent)

        self._execution_thread = threading.Thread(target=target_runner, daemon=True)
        self._execution_thread.start()

    def _mint_run_intent(self, user_message: str, action_type: str, action_params: dict):
        """A fresh intent row for a run no queue submitted. Returns ``(intent_id, intent_or_None)``.

        The queue writes a ``running`` record before dispatching and settles it at the end; a
        desktop launch had neither, which is why the ledger's budget, cost and liveness gates all
        no-oped for it. When the record cannot be written, the run proceeds with a blank id --
        exactly the ungated behaviour it had before -- rather than being taken down by its own
        bookkeeping.
        """
        import time
        import uuid

        from api.intents import Intent

        intent = Intent(
            id=uuid.uuid4().hex,
            action_type=str(action_type or "custom"),
            message=str(user_message or ""),
            action_params=dict(action_params or {}),
            enqueued_at=time.time(),
        )
        try:
            from storage.db import get_store
            from storage.intents import IntentLedger

            ledger = IntentLedger(get_store().path)
            ledger.record(intent)
            ledger.claim(intent)
        except Exception as error:
            print(
                f"[app] the run ledger could not record {intent.id}: "
                f"{type(error).__name__}: {error}",
                file=sys.stderr,
            )
            return "", None
        return intent.id, intent

    def _settle_run_intent(self, intent) -> None:
        """Close the run's own ledger row. Never raises."""
        try:
            from storage.db import get_store
            from storage.intents import IntentLedger

            IntentLedger(get_store().path).settle(intent)
        except Exception as error:
            print(
                f"[app] the run ledger could not settle {getattr(intent, 'id', '?')}: "
                f"{type(error).__name__}: {error}",
                file=sys.stderr,
            )

    def start_execution(self, user_message: str, action_type: str = "custom", action_params: dict = None,
                        intent_id: str = ""):
        """
        Starts the multi-agent task execution in an asynchronous background thread.
        Supports intent-driven Gatekeeper actions and administrative bypass.
        """
        if not user_message or not user_message.strip():
            return {"success": False, "error": "Prompt cannot be empty"}

        with self._run_lock:
            if self._execution_thread and self._execution_thread.is_alive():
                # Server-authoritative run lock (audit H6/H10). The client lock can be defeated by
                # a reload, a retry, or the normalize path; refusing here is what actually stops
                # two runs from interleaving their plan writes. Previously the old run was asked
                # to stop and joined for one second -- best-effort, and a second thread ran anyway.
                #
                # ``reason`` is machine-readable on purpose (Phase 25): the intent loop has to tell
                # "the engine is busy, come back later" from a real refusal, because only the first
                # is a reason to keep an intent queued rather than fail it.
                return {"success": False, "reason": "busy", "error": "A run is already in progress."}

            self._launch_run(user_message, action_type, action_params or {}, intent_id)

        return {"success": True, "status": "started", "message": user_message, "action_type": action_type}

    def get_run_state(self):
        """Whether a workflow is live, so a reloaded UI can re-arm its lock and Stop.

        ``state.isExecuting`` is client-only and resets on reload while this thread keeps
        running, leaving Stop hidden and a second run launchable (audit H6). This is the
        server-side home for that flag.
        """
        return {"running": bool(self._execution_thread and self._execution_thread.is_alive())}

    # -------------------------------------------------------------
    # Intents: the queue's surface, owned by the service (Phase 18)
    # -------------------------------------------------------------
    def attach_intent_queue(self, queue) -> None:
        """Bind the gateway's queue. Called by ``api.server.start_gateway`` where the queue is made.

        The service answers for the intent *operations* (``api.operations``), and the queue belongs
        to the gateway, so the queue is injected rather than reached for. This is also what keeps the
        test suite socket-free: a service built without a gateway simply has no queue, and an intent
        operation against it refuses instead of raising.
        """
        self._intent_queue = queue

    def get_intent_status(self):
        """The queue, the durable ledger it wrote through, and the failure gate.

        One implementation lives in ``api.intents``, so this operation and the test stubs that
        exercise the same wire shape cannot drift apart.
        """
        if self._intent_queue is None:
            return {"success": False, "error": "no intent queue is attached"}
        from api.intents import intent_status

        return intent_status(self._intent_queue, self._ledger).model_dump()

    def submit_intent(self, action_type: str = "custom", message: str = "", action_params=None):
        """Validate-and-append one intent. The caller can only *ask*; the loop is what runs it."""
        if self._intent_queue is None:
            return {"success": False, "error": "no intent queue is attached"}
        from api.intents import submit_intent as _submit

        return _submit(self._intent_queue, action_type=action_type, message=message,
                       action_params=action_params)

    def _run_blocking(self, user_message: str, action_type: str, action_params: dict,
                      intent_id: str = "") -> dict:
        """Run one workflow pass on this thread and return the run's own terminal event.

        The outcome is read from the run's terminal ``workflow_complete`` -- a thread that merely
        finished is not a run that succeeded, and treating it as one is how a failed plan becomes a
        green UI. A *refused* run raises: a busy engine raises :class:`RunDeferred` (the intent keeps
        its place in the queue), and any other refusal raises, which the caller turns into a failed
        intent.
        """
        outcome = {"status": "", "message": ""}
        original = self.emit_event

        def capture(event):
            try:
                if isinstance(event, dict) and event.get("type") == "workflow_complete":
                    outcome["status"] = str(event.get("status") or "")
                    outcome["message"] = str(event.get("message") or "")
            finally:
                original(event)

        self.emit_event = capture
        try:
            result = self.start_execution(user_message, action_type, dict(action_params or {}),
                                          intent_id=intent_id)
            if not result.get("success"):
                if result.get("reason") == "busy":
                    from api.intents import RunDeferred

                    raise RunDeferred(str(result.get("error") or "a run is already in progress"))
                raise RuntimeError(str(result.get("error") or "the run was refused"))
            thread = self._execution_thread
            if thread is not None:
                thread.join()
        finally:
            self.emit_event = original
        return outcome

    def _settle_from_outcome(self, intent, outcome: dict) -> None:
        """Turn a run's terminal status into the intent's fate. Raises on an error, never retries."""
        if outcome["status"] == "error":
            failure = outcome["message"] or "the run failed"
            # Announced before it is raised: the queue settles the intent as failed and the ledger
            # records it, but the *UI* needs to be told now rather than on its next poll.
            self.emit_event({
                "type": "intent_failed",
                "intent_id": intent.id,
                "action_type": intent.action_type,
                "error": failure,
            })
            raise RuntimeError(failure)
        if outcome["status"] == "stopped":
            # Terminal, but the user's own doing: not a failure, and not something to acknowledge.
            intent.status = "stopped"

    def _run_intent(self, intent):
        """Run one queued intent and report how it actually ended.

        Two shapes of work reach here. ``execute_plan`` is the autonomous loop (Phase 18): the
        engine drives the DAG itself, so the loop owns the whole outcome. Every other intent is one
        workflow pass, whose terminal event is its answer.

        Either shape runs against a shadow copy of the workspace (Phase 20): the same intent wraps
        every pass the loop makes, so an earlier pass's writes are visible to a later one and none
        of them touch the user's live tree.

        A busy engine is checked **before** the shadow is made, so a deferred intent leaves no copy
        of the workspace behind (Phase 25). ``_run_blocking`` re-checks it as a backstop: a direct
        dispatch can win the race between this test and the run actually starting.
        """
        if self.get_run_state()["running"]:
            from api.intents import RunDeferred

            raise RunDeferred("a run is already in progress")
        # The process's correlation id, for anything that has no argument to thread -- an
        # unhandled exception's report being the one that matters (Phase 27). Cleared in the
        # ``finally`` so it cannot outlive the run that set it.
        set_current_intent(str(intent.id))
        try:
            with self._staged_execution(str(intent.id)) as shadow:
                # Setup first, and the model is not involved (Phase 29): a project whose
                # dependencies cannot be installed cannot be verified either, so the run is refused
                # here rather than after a model has written code nothing can run.
                self._run_setup_phase()
                if str(intent.action_type) == "execute_plan":
                    self._drive_plan(intent)
                else:
                    outcome = self._run_blocking(
                        intent.message, intent.action_type, dict(intent.action_params), str(intent.id)
                    )
                    self._settle_from_outcome(intent, outcome)
                # Reached only when the pass did not raise: a run that already failed is its own
                # answer, and verifying it would only bury the reason.
                self._verify_shadow(shadow)
        finally:
            set_current_intent(None)

    def _run_setup_phase(self) -> None:
        """Install the project's dependencies, **with egress**, before the model is involved.

        Deterministic and LLM-free: the command comes from the project's own manifests (or a human's
        declaration in ``.aleth_phases.json``), and it runs in a container the model never gets. The
        egress lives for that container's life and dies with its network namespace -- there is no
        long-lived container to disconnect, because this perimeter is one container per command.

        A failed install **refuses the run**: the LLM loop only begins once setup has succeeded, so
        a missing dependency is reported as itself rather than as a model that wrote code nothing
        could run.

        The execution is **bounded and short by default** (Phase 30): this is the only phase with
        egress, so a poisoned manifest or a circular dependency must not be able to hold the intent
        queue's only worker. A timeout kills the container, which takes its network with it.
        """
        from tools import project_phases

        root = get_project_dir()
        try:
            result = project_phases.run_setup(root)
        except Exception as error:
            raise RuntimeError(
                f"the project's dependencies could not be installed: "
                f"{type(error).__name__}: {error}"
            ) from error
        if result is None:
            return
        if result.returncode != 0:
            raise RuntimeError(
                "the project's setup command failed, so nothing that follows could be verified: "
                + _output_tail(result)
            )
        print(
            f"[Setup] {project_phases.detect_setup_command(root)} -> exit 0 (egress severed)",
            flush=True,
        )

    def _verify_shadow(self, shadow) -> None:
        """The automated merge gate: the project's own suite, or its syntax check, decides.

        Airgapped, over the **shadow** -- the execution root is still the shadow here -- and before
        the intent can reach a human. A failure fails the intent *and* marks the shadow unmergeable.

        **There is no free pass (Phase 30).** A project with no test suite gets the runtime's
        structural check (``compileall``, ``node --check``), and a project whose runtime cannot be
        determined at all is marked failed rather than left unverified: an unchecked diff must never
        reach the review surface. The escape hatch is a declared ``verify`` command.
        """
        from tools import project_phases, staging
        from tools.workspace import get_execution_dir

        if shadow is None:
            return
        root = get_execution_dir()
        try:
            result = project_phases.run_verification(root)
        except Exception as error:
            detail = f"the gate could not run: {type(error).__name__}: {error}"
            staging.record_verification(shadow, False, detail)
            raise RuntimeError(detail) from error
        if result is None:
            detail = (
                "no verification command could be determined for this project; declare one as "
                f"\"verify\" in {project_phases.DECLARATION_FILE}"
            )
            staging.record_verification(shadow, False, detail)
            raise RuntimeError(detail)
        if result.returncode == 0:
            staging.record_verification(shadow, True, "")
            print(f"[Verify] {project_phases.detect_verify_command(root)} -> exit 0", flush=True)
            return
        detail = _output_tail(result)
        staging.record_verification(shadow, False, detail)
        self.emit_event({
            "type": "intent_failed",
            "intent_id": shadow.intent_id,
            "action_type": "verify",
            "error": f"the staged changes do not pass the project's own check: {detail}",
        })
        raise RuntimeError(
            "the staged changes do not pass the project's own check, so the shadow is "
            f"unmergeable: {detail}"
        )

    # -------------------------------------------------------------
    # The autonomous loop: drive the DAG to the human gate (Phase 18)
    # -------------------------------------------------------------
    def _plan_progress(self):
        """The DAG's shape right now, in one query (``orchestration.autonomy``)."""
        from orchestration import autonomy
        from storage.db import get_store, plan_id_for
        from tools.workspace import get_active_plan_filename

        return autonomy.plan_progress(
            get_store().path, plan_id_for(get_active_plan_filename())
        )

    def _execute_next_approved(self) -> bool:
        """Apply the approved (``in_progress``) node, if there is one, and say whether it landed."""
        from orchestration import autonomy
        from storage.db import get_store, plan_id_for
        from tools.workspace import get_active_plan_filename

        plan_id = plan_id_for(get_active_plan_filename())
        task_id = autonomy.next_approved_task(get_store().path, plan_id)
        if not task_id:
            return False
        outcome = self._run_blocking(
            "", "execute_artifact", {"taskId": task_id, "planId": plan_id}
        )
        if outcome["status"] == "error":
            # Reported here because the loop's caller only sees the boolean; the run's own message
            # is the fact the user needs, not a generic "the loop failed".
            self.emit_event({
                "type": "intent_failed",
                "intent_id": "",
                "action_type": "execute_plan",
                "error": outcome["message"] or "the execution pass failed",
            })
            return False
        return True

    def _plan_next_runnable(self) -> bool:
        """Dispatch the runnable nodes to the swarm and wait for the plans to land."""
        from orchestration import autonomy

        swarm = self._ensure_swarm()
        dispatched = swarm.tick()
        if not dispatched:
            return False
        # Bounded: a planner that hangs past the budget is a fault to report, not a reason to keep
        # the loop alive. The breaker's wall-clock check is the outer bound; this is the inner one.
        swarm.drain(timeout=autonomy.PLAN_DRAIN_SECONDS)
        return True

    def _drive_plan(self, intent) -> None:
        """Drive the plan forward until it is done, awaiting approval, or the breaker trips.

        The loop stops for a person at exactly one place -- an artifact awaiting approval -- and it
        stops hard when the breaker trips. Every other stop is a failure the user is told about, and
        the ledger's failure gate holds new work until they acknowledge it.
        """
        from orchestration import autonomy

        def announce(step: int, move) -> None:
            self.emit_event({
                "type": "log",
                "agent": "software-architect",
                "log_type": "decision",
                "text": f"[AUTONOMY] transition {step}: {move.kind} ({move.reason})",
            })

        report = autonomy.PlanDriver(
            progress=self._plan_progress,
            execute_next=self._execute_next_approved,
            plan_next=self._plan_next_runnable,
            on_transition=announce,
        ).drive()

        if report.outcome == autonomy.AWAITING_REVIEW:
            # A clean stop at the gate, and the pass that produced the artifact already announced
            # it (``workflow_complete: planned``), so the UI is already showing something to approve.
            return
        if report.outcome == autonomy.DONE:
            return

        failure = report.reason or "the autonomous run stopped without finishing the plan"
        # The failure gate is the ledger's: with the intent settled failed, ``pending_failure`` is
        # what stops the UI accepting new work until the user has seen this and acknowledged it.
        self.emit_event({
            "type": "intent_failed",
            "intent_id": intent.id,
            "action_type": intent.action_type,
            "error": failure,
        })
        raise RuntimeError(failure)

    def acknowledge_intent(self, intent_id=None):
        """Clears the failure gate: the user has seen that their run failed.

        The ledger is the record and the gate is the UI's, so this writes the acknowledgement
        where the next read will find it -- a UI-only dismissal would come back on the next
        reload, and the user would be told about a failure they already dealt with.
        """
        if self._ledger is None:
            return {"success": False, "error": "no intent ledger is attached"}
        try:
            return {"success": True, "acknowledged": int(self._ledger.acknowledge(intent_id))}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def interrupt_intent(self, intent_id=None, note=""):
        """Hold a *running* intent so the user can steer it (Phase 33).

        The agent loop's gate reads this state before every model call and every tool execution,
        so the hold lands at the next boundary rather than cutting a call in flight. Nothing is
        failed -- the run pauses and waits -- which is why the status is its own state and not
        ``failed``.

        The pause *event* is the loop's to announce: it is the thing that actually pauses, and it
        emits ``intent_paused`` the moment it reaches the gate. Emitting a second one here would
        be the same transition reported twice.
        """
        if self._ledger is None:
            return {"success": False, "error": "no intent ledger is attached"}
        try:
            held = self._ledger.interrupt(intent_id, note)
        except Exception as e:
            return {"success": False, "error": str(e)}
        if not held:
            return {"success": False, "error": "the intent is not running, so it cannot be held"}
        from storage.intents import PAUSED

        return {"success": True, "intent_id": str(intent_id or ""), "status": PAUSED}

    def resume_intent(self, intent_id=None, correction=""):
        """Release a paused intent, carrying the user's correction into its context (Phase 33).

        The correction is written to the **ledger**, not handed to the loop: the API thread and the
        run thread are different threads of the same run, and the ledger is the only thing they
        share. The loop drains it when it wakes and injects it as a ``[user]`` message, so a
        correction cannot be lost between the request and the resume.
        """
        if self._ledger is None:
            return {"success": False, "error": "no intent ledger is attached"}
        try:
            released = self._ledger.resume(intent_id, correction)
        except Exception as e:
            return {"success": False, "error": str(e)}
        if not released:
            return {"success": False, "error": "the intent is not paused, so it cannot be resumed"}
        return {"success": True, "intent_id": str(intent_id or ""), "status": "running"}

    def get_intent_steps(self, intent_id=None):
        """The steps a held run can be rewound to (Phase 35).

        Read from the shadow's own snapshot repository, not from the ledger: the snapshots *are* the
        rewind points, and a step whose files never changed has no snapshot of its own to return to
        (it resolves to the newest one at or before it).
        """
        from tools import snapshots, staging

        resolved = str(intent_id or "").strip()
        if not resolved:
            return {"success": False, "error": "a rollback needs the intent it belongs to"}
        shadow = staging.find_staging(intent_id=resolved)
        if shadow is None:
            return {"success": True, "intent_id": resolved, "steps": []}
        return {
            "success": True,
            "intent_id": resolved,
            "staging_id": shadow.staging_id,
            "steps": snapshots.steps(shadow.path),
        }

    def rollback_intent(self, intent_id=None, step=0):
        """Rewind a *held* run's workspace to ``step`` (Phase 35).

        Only while paused: a rewind is an operator decision made at a hold, and the run must not be
        writing files while its tree is restored underneath it. The atomic ledger write is the gate
        -- a conditional ``UPDATE`` that only takes on ``paused_awaiting_input`` -- so the files are
        never restored under a run that resumed in the meantime.

        The filesystem restore happens here; the context window and the token ledger are rewound by
        the agent loop when it resumes, from the target this writes. Those are the two halves of one
        rewind, in the two processes that own them.
        """
        from tools import snapshots, staging

        if self._ledger is None:
            return {"success": False, "error": "no intent ledger is attached"}
        resolved = str(intent_id or "").strip()
        if not resolved:
            return {"success": False, "error": "a rollback needs the intent it belongs to"}
        shadow = staging.find_staging(intent_id=resolved)
        if shadow is None:
            return {"success": False, "error": "there is no staged workspace to rewind"}
        target = snapshots.latest_step_at_or_before(shadow.path, int(step))
        if target is None:
            return {
                "success": False,
                "error": f"there is no snapshot at or before step {int(step)}",
            }
        if not self._ledger.request_rollback(resolved, target):
            return {"success": False, "error": "the run is not paused, so it cannot be rewound"}
        try:
            outcome = snapshots.revert_to_step(shadow.path, target)
        except snapshots.SnapshotError as error:
            # The target did not take: clear it, so a later resume does not rewind the context to a
            # step whose files were never restored.
            self._ledger.request_rollback(resolved, 0)
            return {"success": False, "error": str(error)}
        self.emit_event({
            "type": "log", "agent": "software-architect", "log_type": "decision",
            "text": f"[ROLLBACK] workspace restored to step {outcome['step']}",
        })
        return {
            "success": True,
            "intent_id": resolved,
            "staging_id": shadow.staging_id,
            "step": outcome["step"],
            "requested_step": outcome["requested_step"],
            "steps": snapshots.steps(shadow.path),
        }

    def start_api(self):
        """Bind the local gateway, serving the built frontend and the API from one origin.

        The window is then pointed at this socket, which is what makes the page an ordinary
        browser: its origin is the API's origin, so no request needs a CORS exemption and the
        page never has to be told where the API lives.
        """
        if self._api is not None:
            return {"success": True, "base_url": self._api.base_url, "already_running": True}
        try:
            from api.server import start_gateway
            from storage.db import get_store
            from storage.intents import IntentLedger

            self._ledger = IntentLedger(get_store().path)
            self._api = start_gateway(
                service=self,
                ledger=self._ledger,
                on_intent=self._run_intent,
                static_root=frontend_root(),
            )
        except Exception as error:
            print(
                f"[api] the gateway could not start: {type(error).__name__}: {error}",
                file=sys.stderr,
            )
            return {"success": False, "error": str(error)}
        print(
            f"[api] gateway bound to {self._api.base_url} (loopback only, serving the frontend)",
            flush=True,
        )
        return {"success": True, "base_url": self._api.base_url}

    def get_api_base(self):
        """Where the gateway is bound, so the frontend reaches it without guessing a port."""
        if self._api is None:
            return {"running": False, "base_url": ""}
        return {"running": True, "base_url": self._api.base_url}

    def stop_api(self):
        """Release the socket, the subscribers and the intent worker. Idempotent."""
        api, self._api = self._api, None
        if api is not None:
            try:
                api.stop()
            except Exception as error:
                print(f"[api] shutdown failed: {type(error).__name__}: {error}", file=sys.stderr)
        return {"success": True}

    def audit_codebase_sync(self):
        """Audits discrepancies between workspace files and plan.json."""
        try:
            return audit_codebase_plan_sync()
        except Exception as e:
            return {"success": False, "error": str(e), "in_sync": False,
                    "summary": f"Audit error: {str(e)}"}

    def resolve_sync(self, resolution_type: str):
        """
        Applies sync resolution:
        - 'plan_to_code': sync plan to match existing deliverables
        - 'code_to_plan': reset missing tasks to pending for rebuilding
        """
        try:
            if resolution_type == "plan_to_code":
                res = resolve_sync_plan_to_codebase()
            else:
                res = resolve_sync_code_to_plan()
            self.emit_event({
                "type": "plan_updated",
                "filename": get_active_plan_filename(),
                "content": res.get("content", ""),
                "tree": res.get("tree", []),
                "plan_json": res.get("plan_json", {})
            })
            return res
        except Exception as e:
            return {"success": False, "error": str(e)}

    def rollback_task(self, task_id: str):
        """
        Rolls back a completed task to pending status and restores previous file versions.
        """
        try:
            res = rollback_task_state(task_id)
            if res.get("success"):
                self.emit_event({
                    "type": "plan_updated",
                    "filename": get_active_plan_filename(),
                    "content": res.get("content", ""),
                    "tree": res.get("tree", []),
                    "plan_json": res.get("plan_json", {})
                })
            return res
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_task_diff(self, task_id: str):
        """Read-only diff of the file edits a task recorded in .aleth_backups."""
        try:
            return task_diff(task_id)
        except Exception as e:
            return {"success": False, "error": str(e), "task_id": task_id, "files": []}

    def run_task_tests(self, task_id: str):
        """Executes the regression tests a task wrote and returns the real verdict.

        Called by the result view when it opens (on demand), and by the workflow's own
        verification gate -- so the workflow's event stream is the only thing that runs tests
        during a run. Read-only with respect to the workspace -- see tools/test_runner.py.
        """
        try:
            return run_task_test_suite(task_id)
        except Exception as e:
            return {"success": False, "error": str(e), "task_id": task_id,
                    "found": False, "ran": False, "verdict": "none", "tests": [],
                    "totals": {"passed": 0, "failed": 0, "errors": 0, "skipped": 0},
                    "summary": ""}

    def get_preview_source(self, filename: str = None):
        """Reads the workspace interface file for the live preview. Read-only.

        The preview renders this text through the bridge instead of pointing an iframe at a
        ``file://`` URL, because WebView2 does not reliably finish a local document load and
        the frame is then left blank.
        """
        try:
            return read_preview_source(filename) if filename else read_preview_source()
        except Exception as e:
            return {"success": False, "found": False, "filename": filename or "",
                    "content": "", "truncated": False, "error": str(e)}

    def get_environment_variables(self):
        """Names of the environment variables the app loaded, for the sidebar env panel.

        Read-only, and masked on this side: only a name, a set flag and a character count
        cross the bridge, so no value ever reaches the webview.
        """
        try:
            return read_environment_variables()
        except Exception as e:
            return {"success": False, "found": False, "filename": "",
                    "variables": [], "error": str(e)}

    def get_settings(self):
        """The settings panel's payload: the editable names, with the key masked.

        The endpoint and the three model routes are returned as text because they are not
        secrets. ``OPENAI_API_KEY`` is returned as a set flag and a length only -- the
        value is never put in a payload the webview can read.
        """
        try:
            return read_settings()
        except Exception as e:
            return {"success": False, "found": False, "filename": ".env",
                    "fields": [], "error": str(e)}

    def save_settings(self, values: dict):
        """Persists the panel's values to ``.env`` and this process, then reports the state.

        Takes effect without a restart: the route and endpoint readers consult the
        environment on every call. Names outside the allowlist are reported as ignored
        rather than written.
        """
        try:
            return write_settings(values or {})
        except Exception as e:
            return {"success": False, "found": False, "filename": ".env", "fields": [],
                    "saved": [], "ignored": [], "error": str(e)}

    def stop_execution(self):
        """Signals the active workflow to stop.

        The terminal event is left to the *run*: the runner emits ``workflow_complete``
        (status ``stopped``) when the branch it dispatched actually unwinds, so the UI no
        longer declares the run halted while the backend is still writing. Only when no
        run is live does this emit the terminal event itself, so a stray Stop click still
        returns the UI to a resting state.

        The stop is also written to the **ledger** (Phase 27), not only to the in-process flag: the
        workflow layer's liveness gate reads that row, so a run that is deep inside an agent loop
        finds out that it has been aborted and drops the operation instead of finishing work whose
        result nobody will accept. A flag can only be seen by the thread that holds it.

        Which intent to abort comes from the **queue**, not from the run context (Phase 28): this
        runs on the API thread, and the run's context variable is deliberately invisible across
        threads. The queue is the authority on what it has handed out and not settled.
        """
        registry.stop_workflow()
        running = None
        if self._intent_queue is not None:
            running = self._intent_queue.current
        if self._ledger is not None and running is not None:
            try:
                self._ledger.abort(running.id, "stopped by the user")
            except Exception as error:  # the flag above is what actually stops the run
                print(
                    f"[engine] could not record the abort of intent {running.id}: "
                    f"{type(error).__name__}: {error}",
                    file=sys.stderr,
                )
        if not (self._execution_thread and self._execution_thread.is_alive()):
            self.emit_event({
                "type": "workflow_stopped",
                "status": "stopped",
                "message": "Task halted by user."
            })
        return {"status": "stopped"}

    def retry_execution(self, agent_id: str = None, user_message: str = None):
        """Retries the execution from the failed agent or last message."""
        prompt = user_message or "Retry current architectural step"
        return self.start_execution(prompt)

    # -------------------------------------------------------------
    # Artifact Gate: approval is the only way past a PLANNED task
    # -------------------------------------------------------------
    def approve_artifact(self, task_id: str, plan_id: str = None, intent_id: str = None):
        """Approves a planned artifact: PLANNED -> IN_PROGRESS, and releases execution.

        The state transition is validated against the store first, so an approval for a
        task that is not ``planned`` (a double-click, a stale UI) is refused rather than
        re-running finished work. The events are emitted only after the write commits.

        Approval is what dispatches execution, and the dispatch is a dedicated pass
        (``orchestration.workflow.execution``) that applies the stored artifact directly.
        It deliberately does **not** re-issue the request that produced the plan: re-entering
        an action branch would re-run System 1 (the Gatekeeper router) and System 2 (the
        planner), and a decision re-run is not deterministic -- the directive could be
        classified differently the second time and the approved artifact would be stranded.
        The task is IN_PROGRESS and the artifact is locked, so the pass needs only the task
        and its plan. The returned ``dispatched`` flag states whether it started; the UI must
        not assume the artifact landed.

        ``intent_id`` is the run whose artifact is being released, supplied by the UI that
        tracked it. The dispatch joins *that* run's shadow (Phase 21), so the applied artifact
        is part of the same reviewable delta rather than a shadow nobody can reach.
        """
        from tools.execution_gate import approve_artifact as approve
        from tools.workspace import get_active_plan_filename
        from storage.db import plan_id_for

        resolved_plan = plan_id or plan_id_for(get_active_plan_filename())
        result = approve(str(task_id), resolved_plan)
        if not result.get("success"):
            return result

        with self._run_lock:
            if self._execution_thread and self._execution_thread.is_alive():
                result["dispatched"] = False
                result["note"] = "Approved; a run is already in progress."
                return result

            self._launch_run(
                "",
                "execute_artifact",
                {"taskId": str(task_id), "planId": resolved_plan},
                intent_id=str(intent_id or ""),
            )
        result["dispatched"] = True
        return result

    # -------------------------------------------------------------
    # Phase 20: the shadow workspace and the merge boundary
    # -------------------------------------------------------------
    def _active_plan_id(self) -> str:
        """The plan the engine is working on right now, in one call."""
        from storage.db import plan_id_for
        from tools.workspace import get_active_plan_filename

        return plan_id_for(get_active_plan_filename())

    @contextlib.contextmanager
    def _staged_execution(self, intent_id: str):
        """Run the block against a shadow copy of the workspace, not the live tree.

        The shadow is what the container mounts and what every execution write targets, so a
        hallucinating Coder can damage a scratch copy and nothing else. The block is the run: the
        module-level execution root is set on entry and cleared on exit, on every path.

        A nested call reuses the active shadow rather than staging again. The autonomous loop
        drives several workflow passes under one ``execute_plan`` intent, and each pass has to see
        what the previous one wrote -- a second copy would silently discard it. A *sequential*
        call for the same intent (an approval releasing a run's artifact) finds that run's shadow
        on disk and reuses it too, so its writes join the same delta rather than replacing it.

        Staging is a hard requirement, not a best effort: if the shadow cannot be made, the run
        is refused rather than allowed to fall back onto the user's files. A blank id is not a
        shadow nobody can reach -- it is minted, so every shadow maps to an execution id (Phase
        21: staging is keyed 1:1 to an intent, never to a plan).
        """
        from tools import staging
        from tools.workspace import get_execution_dir, get_project_dir, set_execution_dir

        if get_execution_dir() != get_project_dir():
            # An outer run already staged this workspace; every pass lands in its shadow.
            yield None
            return
        resolved = str(intent_id or "").strip() or uuid.uuid4().hex
        try:
            shadow = staging.find_staging(intent_id=resolved)
            if shadow is None:
                shadow = staging.create_staging(
                    get_project_dir(), intent_id=resolved, plan_id=self._active_plan_id()
                )
        except staging.StagingError as error:
            # Announced so the UI settles instead of hanging on a run that never started.
            self.emit_event({
                "type": "workflow_complete", "status": "error",
                "message": f"The workspace could not be staged, so nothing was executed: {error}",
            })
            raise
        set_execution_dir(shadow.path)
        try:
            yield shadow
        finally:
            # Clear only what this context set. The execution root is process-wide (workers read
            # the published root), so an unconditional ``None`` here would clear the root out from
            # under a *later* staged run that happens to be active -- its writes would then resolve
            # to the live project tree instead of its shadow, and the containment boundary leaks.
            # The run lock serialises runs, so this is a belt-and-braces guard on the one global
            # that carries the sandbox scope.
            from tools.workspace import get_execution_dir as _current_root

            if _current_root() == os.path.abspath(shadow.path):
                set_execution_dir(None)

    def workspace_diff(self, intent_id=None):
        """The staged delta for review: what a merge would change on the host, and nothing else.

        ``intent_id`` is required and is the *only* key. A shadow belongs to one execution; a
        request without the run's id has no shadow to describe, so it is refused rather than
        answered with whichever delta happens to be newest -- guessing is how the wrong changes
        reach the user's tree.
        """
        from tools import staging

        resolved = str(intent_id or "").strip()
        if not resolved:
            return {
                "success": False,
                "error": "A workspace diff needs the intent id that produced the staged changes.",
            }
        shadow = staging.find_staging(intent_id=resolved)
        if shadow is None:
            return {
                "success": True, "staged": False, "clean": True,
                "added": [], "modified": [], "deleted": [],
                "counts": {"added": 0, "modified": 0, "deleted": 0}, "patch": "",
            }
        return {
            "success": True,
            "staged": True,
            # The gate's verdict travels with the delta (Phase 38): the UI must not offer an "Apply to
            # Project" action for work the project's own check did not pass, and it must not have to
            # guess the verdict from a second call.
            "verified": shadow.verified,
            "mergeable": shadow.verified is True,
            **staging.compute_diff(shadow),
        }

    def workspace_merge(self, intent_id=None, approve=True):
        """Discard the staged delta, or apply it. The merge gate.

        A rejection discards the shadow and leaves the host untouched, under the project's write
        lock so a rejection cannot interleave with an apply.

        An **approve is the egress** (Phase 38): it delegates to :meth:`egress_intent`, so the older
        name is not a second, ungated way onto the user's tree. There is one apply path, and it is
        collision-gated.

        ``intent_id`` is required, for the same reason the diff requires it.
        """
        from tools import staging

        resolved = str(intent_id or "").strip()
        if not resolved:
            return {
                "success": False,
                "error": "A merge needs the intent id that produced the staged changes.",
            }
        if approve:
            return self.egress_intent(resolved)

        with staging.merge_lock():
            shadow = staging.find_staging(intent_id=resolved)
            if shadow is None:
                return {"success": False, "error": "There are no staged changes to merge."}

            purged = staging.purge_staging(shadow.staging_id)
            self.emit_event({
                "type": "log", "agent": "software-architect", "log_type": "decision",
                "text": f"[STAGING] rejected and purged {shadow.staging_id}",
            })
            return {
                "success": True, "rejected": True, "purged": purged,
                "staging_id": shadow.staging_id, "intent_id": shadow.intent_id,
            }

    def egress_intent(self, intent_id=None):
        """Apply a verified run's staged work to the user's host tree (Phase 38).

        The extraction gate, and the only way staged work reaches the host. It refuses unless the
        shadow is **verified** -- the project's own suite, or its syntax check, actually passed over
        it -- because applying unchecked work to a person's tree is the one thing the staging
        boundary exists to prevent.

        The apply is **collision-gated**: the host is compared against the baseline recorded when
        the run started, and a path the *agent* changed that the *human* also changed refuses the
        whole egress rather than overwriting their work. It runs under the project's write lock, and
        the collision check is re-run inside it -- between the review a person read and this call, a
        file may have been saved.
        """
        from orchestration import autonomy
        from storage.db import get_store
        from tools import staging

        resolved = str(intent_id or "").strip()
        if not resolved:
            return {"success": False, "error": "An egress needs the intent id that produced the work."}
        try:
            with staging.merge_lock():
                shadow = staging.find_staging(intent_id=resolved)
                if shadow is None:
                    return {"success": False, "error": "There are no staged changes to apply."}

                # The automated gate (Phase 29), sealed in Phase 30: only a shadow the project's own
                # suite (or its syntax check) actually *passed* may reach the host. ``None`` means
                # the gate could not run, which is not a pass.
                if shadow.verified is not True:
                    reason = shadow.verify_error or (
                        "the verification gate did not run for this shadow"
                        if shadow.verified is None
                        else "the gate reported a failure"
                    )
                    return {
                        "success": False,
                        "staging_id": shadow.staging_id,
                        "intent_id": shadow.intent_id,
                        "verified": shadow.verified,
                        "error": f"the staged changes are not verified; refusing to apply: {reason}",
                    }

                plan_id = shadow.plan_id or self._active_plan_id()
                progress = autonomy.plan_progress(get_store().path, plan_id)
                if not progress.finished:
                    return {
                        "success": False, "staging_id": shadow.staging_id,
                        "error": "The plan is not complete; refusing to apply staged changes.",
                        "progress": {
                            "total": progress.total, "completed": progress.completed,
                            "failed": progress.failed, "planned": progress.planned,
                            "in_progress": progress.in_progress, "pending": progress.pending,
                        },
                    }

                plan = staging.egress_plan(shadow)
                if plan["conflict"]:
                    # Refused before anything is copied: the engine never writes over a person's
                    # work, and the shadow is kept so the agent's work is not lost -- it needs
                    # reconciling, which is a person's decision.
                    return {
                        "success": False,
                        "conflict": True,
                        "staging_id": shadow.staging_id,
                        "intent_id": shadow.intent_id,
                        "collisions": plan["collisions"],
                        "error": (
                            "the host changed while the agent worked; nothing was applied: "
                            + ", ".join(plan["collisions"][:8])
                        ),
                    }

                result = staging.apply_egress(shadow)
                staging.purge_staging(shadow.staging_id)
                self.emit_event({
                    "type": "log", "agent": "software-architect", "log_type": "decision",
                    "text": (
                        f"[EGRESS] applied {result['applied']['modified']} change(s) and "
                        f"{result['applied']['added']} addition(s) to {shadow.host_root}"
                    ),
                })
                return {"success": True, **result}
        except staging.EgressConflict as error:
            # The re-check inside the lock found a collision the pre-check did not (a save landed
            # between the two). Same refusal shape, so the caller reads one kind of answer.
            return {"success": False, "conflict": True, "error": str(error)}
        except staging.StagingLocked:
            # A conflict, not a fault: the gateway maps it to a 409 and the client's retry story
            # understands it. Re-raised rather than reported as a refusal.
            raise
        except staging.StagingError as error:
            return {"success": False, "error": str(error)}

    # -------------------------------------------------------------
    # The swarm: reactive dispatch, driven by events
    # -------------------------------------------------------------
    def _ensure_swarm(self):
        """The swarm, built on first use. It owns the pool and the in-flight set."""
        from tools.workspace import get_active_plan_filename, get_execution_dir

        if self._swarm is None:
            from orchestration.swarm import Swarm
            from storage.db import get_store, plan_id_for

            self._swarm = Swarm(
                db_path=get_store().path,
                plan_id=plan_id_for(get_active_plan_filename()),
                workspace_dir=get_execution_dir(),
            )
        else:
            # Each run stages its own shadow, so the root a node's descriptor should carry moves
            # between runs. The cached swarm must follow it, or a node would be dispatched into
            # the previous run's stale copy.
            self._swarm.set_workspace(get_execution_dir())
        return self._swarm

    def _tick_swarm(self) -> list:
        """Dispatch whatever is runnable right now. Never raises.

        A dispatch failure is reported rather than propagated: the caller is either the tail of a
        finished run or an approval that has already committed its state, and neither should be
        undone by the pool being unavailable.
        """
        try:
            return self._ensure_swarm().tick(intent_id=self._running_intent_id())
        except Exception as error:
            print(f"[Swarm] tick skipped: {type(error).__name__}: {error}", file=sys.stderr)
            return []

    def _running_intent_id(self) -> str:
        """The intent a tick belongs to: the run context, else whatever the queue has in flight.

        The context variable is right when the tick is on the run's own thread (the run's trailing
        tick), and empty when it is not -- an API-thread tick during a live run. The queue is the
        authority in both cases, so it is the fallback rather than the primary (Phase 28).
        """
        resolved = get_current_intent()
        if resolved:
            return resolved
        if self._intent_queue is None:
            return ""
        current = self._intent_queue.current
        return str(current.id) if current is not None else ""

    def start_swarm(self):
        """Cold start: bring the pool up and take one tick.

        Called by the frontend's ready signal, not by ``__init__``. This is what awakens nodes left
        ``pending`` by a crash -- and it is a single tick, not a loop: the callbacks drive from here.
        """
        return {"success": True, "dispatched": self._tick_swarm()}

    def shutdown_swarm(self):
        """Drain the swarm, close its pool, and forget it. Idempotent, and never raises.

        The swarm's worker callbacks run on the pool's own thread and reach the database, so a
        caller that is about to remove that database -- an app exit, or a test's temporary
        directory -- must shut the swarm down first rather than race it. ``_swarm`` is dropped so a
        later tick builds a fresh swarm rather than dispatching into a shut-down pool.
        """
        swarm, self._swarm = self._swarm, None
        if swarm is None:
            return {"success": True, "drained": True}
        try:
            drained = swarm.shutdown()
        except Exception as error:
            print(f"[Swarm] shutdown failed: {type(error).__name__}: {error}", file=sys.stderr)
            return {"success": False, "drained": False, "error": str(error)}
        return {"success": True, "drained": drained}

    def ui_ready(self):
        """The frontend is loaded and its inbound bus is bound: start the swarm.

        The cold-start tick is gated on *this* rather than on a boot step in the entrypoint, because
        only the frontend knows when its listeners exist. A tick before then would broadcast
        ``task_state_updated`` into nothing and the DAG would render stale until the next plan load.
        """
        return self.start_swarm()

    def reject_artifact(self, task_id: str, feedback: str = "", plan_id: str = None):
        """Reject a planned artifact: persist the critique and re-dispatch the node.

        The critique is **required**. A blind rejection would re-queue the node with no new
        information, so the worker would regenerate the same artifact -- exactly the loop the retry
        budget exists to prevent. The retry is a tick: the node is ``pending`` again and the
        dispatch query decides whether its budget allows another attempt.
        """
        from storage.db import get_store, plan_id_for
        from tools.workspace import get_active_plan_filename

        note = str(feedback or "").strip()
        if not note:
            return {
                "success": False,
                "error": "A rejection needs feedback explaining what to change.",
            }

        resolved = plan_id or plan_id_for(get_active_plan_filename())
        if not get_store().record_rejection(resolved, str(task_id), note):
            return {
                "success": False,
                "error": f"Unknown task {task_id!r} in plan {resolved!r}.",
            }

        dispatched = self._tick_swarm()
        # Announced after the write commits, so the UI's demotion is the backend's, not a guess the
        # frontend made locally. ``record_rejection`` is a store write and emits nothing itself.
        self.emit_event({
            "type": "task_state_updated",
            "plan_id": resolved,
            "task_id": str(task_id),
            "status": "pending",
        })
        return {
            "success": True,
            "task_id": str(task_id),
            "plan_id": resolved,
            "dispatched": dispatched,
        }

    def add_task_dependency(self, parent_id: str, child_id: str, plan_id: str = None):
        """Adds one blocker edge to the plan DAG: ``child`` now depends on ``parent``.

        Topology is mutated as structure, never inferred from the document: markdown is a
        read-only projection, so a relationship is written where relationships live -- the
        store's node -- and the ``task_dependencies`` table follows from it. The new graph is
        announced on the same ``plan_updated`` event the tree and the DAG already render, so
        both repaint from the committed state.
        """
        from tools.workspace import get_active_plan_filename
        from storage.db import plan_id_for, get_store

        resolved_plan = plan_id or plan_id_for(get_active_plan_filename())
        if not get_store().add_task_dependency(resolved_plan, str(child_id), str(parent_id)):
            return {
                "success": False,
                "error": f"Could not add {parent_id!r} -> {child_id!r} in plan {resolved_plan!r}.",
            }

        saved = load_plan_state()
        try:
            markdown = compile_plan_json_to_markdown(saved)
        except Exception:
            markdown = ""
        self.emit_event({
            "type": "plan_updated",
            "filename": saved.get("plan_file") or get_active_plan_filename(),
            "content": markdown,
            "tree": saved.get("steps", []),
            "plan_json": saved,
        })
        return {
            "success": True,
            "plan_id": resolved_plan,
            "task_id": str(child_id),
            "depends_on": str(parent_id),
        }

    def update_artifact_target(
        self, task_id: str, target_index: int, new_content: str, plan_id: str = None
    ):
        """Amends one AST target's proposed content on a PLANNED artifact.

        The review surface is editable because a plan is a proposal: a hallucinated
        character should cost one edit, not a rejected run, wasted tokens and a re-plan.
        The whole artifact is re-validated before it is stored, so an amendment cannot leave
        a payload the executor would refuse or misread, and the amendment is announced on the
        same ``artifact_planned`` event the review surface already renders.

        Refused unless the task is still ``planned``: amending an artifact that is already
        ``in_progress`` would race the execution pass that is reading it.
        """
        from tools.payloads import ImplementationPlanArtifact
        from tools.workspace import get_active_plan_filename
        from storage.db import plan_id_for, get_store

        resolved_plan = plan_id or plan_id_for(get_active_plan_filename())
        store = get_store()
        dag = store.get_dag(resolved_plan)
        if dag is None:
            return {"success": False, "error": f"No plan {resolved_plan!r} in the store."}
        node = dag.nodes.get(str(task_id))
        if node is None:
            return {"success": False, "error": f"No task {task_id!r} in plan {resolved_plan!r}."}
        if node.status != "planned":
            return {
                "success": False,
                "error": f"Task {task_id!r} is {node.status!r}; only a 'planned' artifact can be amended.",
            }

        payload = store.get_artifact(resolved_plan, str(task_id))
        if payload is None:
            return {"success": False, "error": f"No artifact is stored for {task_id!r}."}
        try:
            artifact = ImplementationPlanArtifact.model_validate(payload)
        except Exception as e:
            return {"success": False, "error": f"The stored artifact is not readable: {e}"}

        try:
            index = int(target_index)
        except (TypeError, ValueError):
            return {"success": False, "error": f"target_index {target_index!r} is not an integer."}
        if index < 0 or index >= len(artifact.ast_targets):
            return {"success": False, "error": f"target_index {index} is out of range."}

        artifact.ast_targets[index].content = str(new_content or "")
        store.save_artifact(resolved_plan, str(task_id), artifact.model_dump())
        # The same events a fresh plan emits, so the review surface repaints from the stored
        # payload rather than from this call's return value.
        self.emit_event({"type": "artifact_planned", "artifact": artifact.model_dump()})
        self.emit_event({
            "type": "task_state_updated",
            "plan_id": resolved_plan,
            "task_id": str(task_id),
            "status": "planned",
        })
        return {"success": True, "artifact": artifact.model_dump()}

    def get_source_span(self, file_path: str, start: int = 0, end: int = 0):
        """The current workspace bytes in one artifact target's span. Read-only.

        The diff surface renders one AST target's proposed content against the bytes it
        would replace, so it reads that span and nothing else: a whole-file read across the
        bridge would make a whole-file diff possible, which is exactly what the artifact
        surface exists to avoid. The span is clamped to the file rather than refused, so a
        plan recorded against a file that has since shrunk still renders.

        Containment is decided by resolving both paths -- ``Path.resolve()`` on the workspace
        root and on the requested path -- and comparing them, not by string manipulation. A
        string guard is a zero-day waiting to happen: ``..\\..\\``, a leading ``/``, a drive
        letter and a symlink are all caught by construction here, and none of them can be
        missed by a missed edge case in a hand-written pattern.
        """
        from pathlib import Path

        try:
            root = Path(get_project_dir()).resolve()
            target = (root / str(file_path or "")).resolve()
            if not target.is_relative_to(root):
                return {"success": False, "found": False, "file_path": str(file_path or ""),
                        "start": 0, "end": 0, "length": 0, "text": "",
                        "error": "Path traversal denied."}
            with open(target, "rb") as handle:
                raw = handle.read()
            length = len(raw)
            lo = max(0, min(int(start), length))
            hi = max(lo, min(int(end), length))
            return {
                "success": True, "found": True, "file_path": str(file_path or ""),
                "start": lo, "end": hi, "length": length,
                "text": raw[lo:hi].decode("utf-8", errors="replace"),
            }
        except FileNotFoundError:
            return {"success": True, "found": False, "file_path": str(file_path or ""),
                    "start": 0, "end": 0, "length": 0, "text": ""}
        except Exception as e:
            return {"success": False, "found": False, "file_path": str(file_path or ""),
                    "start": 0, "end": 0, "length": 0, "text": "", "error": str(e)}

    # -------------------------------------------------------------
    # Detached console (hybrid console: inline drawer + second window)
    # -------------------------------------------------------------
    def open_console_window(self, backlog=None):
        """Opens the transcript in a real second native window, pointed at ``/console.html``.

        The window is a **browser on the gateway**, not an inline ``html=`` string. That is what
        gives it an origin: an inline document is opaque, so it can neither fetch the backlog nor
        open an ``EventSource``, and its lines had to be injected with ``evaluate_js``. Served by
        the gateway, it shares the app's origin and reads the same stream every other client
        reads.

        Spawning is handed to its own daemon thread so window creation can never hold the caller;
        success or failure arrives as a ``console_detached`` / ``console_detach_failed`` event
        rather than a return value.
        """
        seeded = [
            {"kind": str(item.get("kind", "log")), "text": str(item.get("text", ""))}
            for item in (backlog or [])
            if isinstance(item, dict)
        ]

        def _spawn():
            try:
                window = webview.create_window(
                    title="Aleth \u2022 Console",
                    url=f"{self._console_url()}",
                    width=760,
                    height=440,
                    background_color="#0a0c10",
                )
            except Exception as e:
                print(f"[EngineService] Detached console failed: {e}", file=sys.stderr)
                self.emit_event({"type": "console_detach_failed", "error": str(e)})
                return
            if not window:
                self.emit_event({"type": "console_detach_failed", "error": "window was not created"})
                return
            with self._console_lock:
                self._console_window = window
                self._console_backlog = seeded
            window.events.closed += self._forget_console_window
            self.emit_event({"type": "console_detached"})

        threading.Thread(target=_spawn, daemon=True).start()
        return {"success": True, "pending": True}

    def _console_url(self) -> str:
        """The console page on the gateway, or a file path when the gateway is not running.

        The fallback keeps the call honest rather than opening a blank window: without a gateway
        there is no origin to serve from, and the page would have nothing to read.
        """
        if self._api is not None:
            return f"{self._api.base_url}/console.html"
        return os.path.join(frontend_root(), "console.html")

    def get_console_backlog(self):
        """The transcript the dock had when it detached.

        The second window cannot read the first window's memory, so the history travels through
        the backend. This is the one thing the console page fetches before it starts reading the
        stream.
        """
        with self._console_lock:
            return {"lines": list(self._console_backlog)}

    def _forget_console_window(self):
        """Drops the reference once the user closes the detached window."""
        with self._console_lock:
            self._console_window = None
            self._console_backlog = []

    def push_console_line(self, kind, text):
        """Mirrors one transcript line onto the bus, for the detached window to render.

        The line travels as a ``console_line`` event, so it reaches the console page the same way
        every other event does -- through the stream. The dock ignores this event type, so
        echoing it back does not duplicate the line it came from.

        A refusal when no window is open is deliberate and is what the dock reads: it stops
        mirroring rather than emitting into nothing for the rest of the session.
        """
        with self._console_lock:
            if self._console_window is None:
                return {"success": False, "error": "console window is not open"}
        self.emit_event({"type": "console_line", "kind": str(kind), "text": str(text)})
        return {"success": True}


def main(argv=None):
    """Launch the desktop UI.

    ``--debug`` opens the WebView2 DevTools. The window normally runs with
    ``debug=False``, which makes an uncaught error or a failed resource invisible:
    the window renders but nothing responds. DevTools exposes the console and the
    network log for exactly that case.
    """
    argv = sys.argv[1:] if argv is None else argv
    debug = "--debug" in argv

    # Load the Laya checkpoint in the background, so its one-off read overlaps the window
    # opening instead of being paid on the user's first click. A no-op unless LAYA_BACKEND
    # selects the checkpoint, and it never holds the process open.
    from agents import laya_model
    laya_model.warm_up_async()

    api = EngineService()

    # The window is loaded from the Vite build, not from the hand-authored document in
    # ui/. A build that has not been made yet is reported as the build step it is,
    # rather than as a blank window.
    dist = frontend_root()
    index_path = os.path.join(dist, "index.html")
    if not os.path.isfile(index_path):
        print("The frontend has not been built yet: dist/index.html is missing.")
        print("Build it once with:  npm install && npm run build")
        raise SystemExit(1)

    # The gateway serves the bundle *and* the API from one origin, so the window loads a URL
    # rather than a document path. It must be listening before the window is created, or the
    # first navigation has nothing to resolve against.
    started = api.start_api()
    if not started.get("success"):
        print("The local API gateway could not start; the UI would have no backend.")
        raise SystemExit(1)
    ui_url = f"{started['base_url']}/index.html"

    window = webview.create_window(
        title="Aleth • Plan Orchestrator Studio",
        url=ui_url,
        # No ``js_api``: the window is a browser. Everything it needs is a typed HTTP call to the
        # loopback gateway, and nothing in the page can reach a backend method that executes.
        width=1340,
        height=860,
        min_size=(1040, 700),
        background_color="#0a0c10"
    )

    api.set_window(window)
    try:
        webview.start(debug=debug)
    finally:
        api.stop_api()


if __name__ == "__main__":
    main()