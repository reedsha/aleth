import os
import sys
import json
import threading
import html as html_module
import webview

from env_boot import load_environment

# Load environment configuration. ``.env`` deliberately wins over the process
# environment; an exported name that shadowed it is reported rather than silently used.
for _shadowed in load_environment():
    print(f"[Config] .env overrides the exported {_shadowed}")

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
    sync_plan_on_disk,
    compile_plan_json_to_markdown,
    parse_markdown_to_plan_dict,
    read_file,
    write_file,
    audit_codebase_plan_sync,
    resolve_sync_plan_to_codebase,
    resolve_sync_code_to_plan,
    rollback_task_state,
    task_diff,
    read_preview_source,
    read_environment_variables
)
from tools.settings import read_settings, save_settings as write_settings
from tools.plan_state import plan_structure_report, write_plan_markdown
from tools.test_runner import run_task_tests as run_task_test_suite


# The style of the detached console window. It is a separate document from ui/index.html
# and is handed to pywebview as inline html, so it needs no subresource fetch and cannot
# be affected by the module-bundle rules that govern the main window.
_CONSOLE_WINDOW_STYLE = """
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #0a0c10; color: #cbd5e1;
         font: 12px/1.6 "JetBrains Mono", Consolas, monospace; }
  header { position: sticky; top: 0; display: flex; align-items: center;
           justify-content: space-between; gap: 10px; padding: 8px 12px;
           background: #15181d; border-bottom: 1px solid #2a2d32;
           font-size: 10.5px; letter-spacing: .08em; text-transform: uppercase;
           color: #94a3b8; }
  #stream { padding: 8px 12px 14px 12px; }
  .ln { display: flex; gap: 10px; white-space: pre-wrap; overflow-wrap: break-word; }
  .cmd .t, .tool .t { color: #7dd3fc; }
  .ok .t { color: #34d399; }
  .error .t { color: #f87171; }
  .agent .t, .decision .t { color: #fbbf24; }
  .state .t { color: #64748b; }
  .delegate .t, .summary .t { color: #e2e8f0; }
"""

_CONSOLE_WINDOW_APPEND_JS = (
    "window.appendLine = function (kind, text) {"
    "  var stream = document.getElementById('stream');"
    "  if (!stream) return;"
    "  var row = document.createElement('div');"
    "  row.className = 'ln ' + String(kind || 'log').replace(/[^a-z0-9_-]/gi, '');"
    "  var span = document.createElement('span');"
    "  span.className = 't';"
    "  span.textContent = String(text);"
    "  row.appendChild(span);"
    "  stream.appendChild(row);"
    "  window.scrollTo(0, document.body.scrollHeight);"
    "};"
)


def _console_kind(kind) -> str:
    """The transcript line's class suffix, restricted to what belongs in a class name."""
    cleaned = "".join(ch for ch in str(kind or "log").lower() if ch.isalnum() or ch in "_-")
    return cleaned or "log"


def _console_window_html(backlog) -> str:
    """The detached console's initial document, seeded with the transcript shown so far."""
    rows = []
    for item in backlog or []:
        if not isinstance(item, dict):
            continue
        kind = _console_kind(item.get("kind"))
        text = html_module.escape(str(item.get("text", "")))
        rows.append(f'<div class="ln {kind}"><span class="t">{text}</span></div>')
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        "<title>DeepAgents \u2022 Console</title>"
        f"<style>{_CONSOLE_WINDOW_STYLE}</style></head>"
        '<body><header><span>DeepAgents \u2022 Console</span>'
        '<span>detached</span></header>'
        f'<div id="stream">{"".join(rows)}</div>'
        f"<script>{_CONSOLE_WINDOW_APPEND_JS}</script></body></html>"
    )


def _eval_console_line(window, kind, text) -> None:
    """Pushes one line into the detached window's transcript.

    The values are JSON-encoded, which is a valid JavaScript string literal, so a log line
    can never break out of the call or corrupt the second window's document.
    """
    window.evaluate_js(
        f"window.appendLine && window.appendLine({json.dumps(str(kind))}, {json.dumps(str(text))});"
    )


class BridgeAPI:
    """
    Two-way asynchronous bridge connecting PyWebView UI to Python DeepAgents backend.
    """
    def __init__(self):
        self._window = None
        self._execution_thread = None
        self._tagging_thread = None
        # The detached-console window (a real second pywebview window) and the lines that
        # arrived before it finished loading. Touched from the workflow thread, the spawn
        # thread and pywebview's loaded/closed callbacks, so every access takes the lock.
        self._console_window = None
        self._console_buffer = []
        self._console_lock = threading.Lock()

    def set_window(self, window):
        """Bind the webview window for pushing asynchronous events to JavaScript."""
        self._window = window

    def emit_event(self, event_data: dict):
        """Pushes structured real-time events to the frontend UI."""
        if not self._window:
            return
        try:
            escaped_json = json.dumps(event_data)
            self._window.evaluate_js(f"window.onAgentEvent && window.onAgentEvent({escaped_json});")
        except Exception as e:
            print(f"[BridgeAPI] Error pushing event to UI: {e}", file=sys.stderr)

    # -------------------------------------------------------------
    # Dynamic Agent Registry Methods
    # -------------------------------------------------------------
    def get_agents(self):
        """Fetch all categorized Main Agents and Coder Agents."""
        try:
            return registry.get_agent_summary()
        except Exception as e:
            return {"error": str(e), "main_agents": [], "coder_agents": [], "workspace_dir": get_project_dir()}

    def get_agent(self, agent_id: str):
        """Fetch metadata and system prompt for a single agent."""
        return registry.get_agent(agent_id) or {"error": f"Agent {agent_id} not found"}

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
                print(f"[BridgeAPI] Dialog error: {e}", file=sys.stderr)

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

    def get_plan_json(self):
        """Directly fetches the plan.json machine state dictionary."""
        return load_plan_state()

    def save_plan_json(self, plan_data: dict):
        """Persists plan.json directly and compiles back to PLAN.md."""
        saved = save_plan_state(plan_data)
        filename = get_active_plan_filename()
        content = compile_plan_json_to_markdown(saved)
        data = {
            "success": True,
            "filename": filename,
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

    def sync_plan(self):
        """Checks disk mtimes and synchronizes plan.json and PLAN.md."""
        synced = sync_plan_on_disk()
        data = registry.get_current_plan_data()
        self.emit_event({
            "type": "plan_updated",
            "filename": data["filename"],
            "content": data["content"],
            "tree": data["tree"],
            "plan_json": data.get("plan_json", {})
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

        self.emit_event({"type": "laya_tagging_done", "success": True, **result})

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
            return plan_structure_report()
        except Exception as e:
            return {"structured": True, "issues": [], "counts": {},
                    "summary": f"Structure check unavailable: {e}", "error": str(e)}

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
        res = registry.create_new_plan_file(filename, project_idea)
        self.emit_event({
            "type": "plan_updated",
            "filename": res["filename"],
            "content": res["content"],
            "tree": res["tree"],
            "plan_json": res.get("plan_json", {})
        })
        return res

    def save_plan_content(self, filename: str, content: str):
        """Saves edited markdown directly back to the active plan file and syncs plan.json."""
        clean_name = set_active_plan_filename(filename)
        # The plan lives in the plan directory, not the code workspace, so it is written
        # with the plan-aware helper rather than the workspace file tool.
        write_plan_markdown(content)
        parsed = parse_markdown_to_plan_dict(content, clean_name)
        saved = save_plan_state(parsed)
        # save_plan_state persists the *recompiled* markdown, so that is what the UI must be
        # handed back. Returning the editor's raw text let the workbench show (and mark
        # clean) a document that differed from what is on disk and from plan.json.
        try:
            canonical = compile_plan_json_to_markdown(saved)
        except Exception:
            canonical = content
        data = {
            "success": True,
            "filename": clean_name,
            "content": canonical,
            "tree": saved.get("steps", []),
            "plan_json": saved,
            "plans": list_plan_files()
        }
        self.emit_event({
            "type": "plan_updated",
            "filename": clean_name,
            "content": canonical,
            "tree": saved.get("steps", []),
            "plan_json": saved
        })
        return data

    # -------------------------------------------------------------
    # Execution Lifecycle & Card Orchestration
    # -------------------------------------------------------------
    def start_execution(self, user_message: str, action_type: str = "custom", action_params: dict = None):
        """
        Starts the multi-agent task execution in an asynchronous background thread.
        Supports intent-driven Gatekeeper actions and administrative bypass.
        """
        if not user_message or not user_message.strip():
            return {"error": "Prompt cannot be empty"}

        if self._execution_thread and self._execution_thread.is_alive():
            registry.stop_workflow()
            self._execution_thread.join(timeout=1.0)

        def target_runner():
            registry.run_agent_workflow(
                user_message.strip(),
                self.emit_event,
                action_type=action_type or "custom",
                action_params=action_params or {}
            )

        self._execution_thread = threading.Thread(target=target_runner, daemon=True)
        self._execution_thread.start()

        return {"status": "started", "message": user_message, "action_type": action_type}

    def audit_codebase_sync(self):
        """Audits discrepancies between workspace files and plan.json."""
        try:
            return audit_codebase_plan_sync()
        except Exception as e:
            return {"error": str(e), "in_sync": False, "summary": f"Audit error: {str(e)}"}

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
        """Read-only diff of the file edits a task recorded in .deepagents_backups."""
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
        """
        registry.stop_workflow()
        if not (self._execution_thread and self._execution_thread.is_alive()):
            self.emit_event({
                "type": "workflow_stopped",
                "message": "Task halted by user."
            })
        return {"status": "stopped"}

    def retry_execution(self, agent_id: str = None, user_message: str = None):
        """Retries the execution from the failed agent or last message."""
        prompt = user_message or "Retry current architectural step"
        return self.start_execution(prompt)

    # -------------------------------------------------------------
    # Detached console (hybrid console: inline drawer + second window)
    # -------------------------------------------------------------
    def open_console_window(self, backlog=None):
        """Opens the transcript in a real second native window (`webview.create_window`).

        This runs on the webview's JS-API thread, which is precisely the thread pywebview
        expects for a window created after `start` -- it creates such a window immediately
        rather than deferring it to the main loop. Spawning is still handed to its own
        daemon thread and the bridge returns at once, so window creation can never hold the
        caller; success or failure arrives as a `console_detached` / `console_detach_failed`
        event instead of a return value.
        """
        def _spawn():
            try:
                window = webview.create_window(
                    title="DeepAgents \u2022 Console",
                    html=_console_window_html(backlog),
                    width=760,
                    height=440,
                    background_color="#0a0c10",
                )
            except Exception as e:
                print(f"[BridgeAPI] Detached console failed: {e}", file=sys.stderr)
                self.emit_event({"type": "console_detach_failed", "error": str(e)})
                return
            if not window:
                self.emit_event({"type": "console_detach_failed", "error": "window was not created"})
                return
            with self._console_lock:
                self._console_window = window
                self._console_buffer = []
            # Lines pushed before the document finishes loading are buffered; this flushes
            # them once it has, so the second window catches up instead of missing them.
            window.events.loaded += self._flush_console_buffer
            window.events.closed += self._forget_console_window
            self.emit_event({"type": "console_detached"})

        threading.Thread(target=_spawn, daemon=True).start()
        return {"success": True, "pending": True}

    def _forget_console_window(self):
        """Drops the reference once the user closes the detached window."""
        with self._console_lock:
            self._console_window = None
            self._console_buffer = []

    def _flush_console_buffer(self):
        """Replays transcript lines that arrived before the detached window loaded."""
        with self._console_lock:
            window = self._console_window
            if not window:
                return
            pending, self._console_buffer = self._console_buffer, []
        for kind, text in pending:
            try:
                _eval_console_line(window, kind, text)
            except Exception as e:
                print(f"[BridgeAPI] Detached console push failed: {e}", file=sys.stderr)
                with self._console_lock:
                    self._console_window = None
                return

    def push_console_line(self, kind, text):
        """Mirrors one transcript line into the detached window, if one is open."""
        with self._console_lock:
            window = self._console_window
            if not window:
                return {"success": False, "error": "console window is not open"}
            try:
                if not window.events.loaded.is_set():
                    self._console_buffer.append((kind, text))
                    return {"success": True, "buffered": True}
                if self._console_buffer:
                    pending, self._console_buffer = self._console_buffer, []
                else:
                    pending = []
            except Exception as e:
                # A closed window raises here; dropping the reference stops the mirror rather
                # than retrying against a window that no longer exists.
                self._console_window = None
                return {"success": False, "error": str(e)}
        # Pushes happen outside the lock so a slow webview cannot stall the workflow
        # thread that is emitting into it.
        for pending_kind, pending_text in pending:
            try:
                _eval_console_line(window, pending_kind, pending_text)
            except Exception as e:
                self._console_window = None
                return {"success": False, "error": str(e)}
        try:
            _eval_console_line(window, kind, text)
            return {"success": True}
        except Exception as e:
            self._console_window = None
            return {"success": False, "error": str(e)}


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

    api = BridgeAPI()
    
    ui_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", "index.html")

    window = webview.create_window(
        title="DeepAgents • Plan Orchestrator Studio",
        url=ui_path,
        js_api=api,
        width=1340,
        height=860,
        min_size=(1040, 700),
        background_color="#0a0c10"
    )

    api.set_window(window)
    webview.start(debug=debug)


if __name__ == "__main__":
    main()