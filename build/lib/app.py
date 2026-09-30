import os
import sys
import threading
import traceback
import html as html_module
import webview

from env_boot import load_environment

# Load environment configuration. ``.env`` deliberately wins over the process
# environment; an exported name that shadowed it is reported rather than silently used.
for _shadowed in load_environment():
    print(f"[Config] .env overrides the exported {_shadowed}")

from bridge_bus import BridgeBus, WebviewTransport
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
from tools.settings import read_settings, save_settings as write_settings
from tools.plan_state import append_pending_task, plan_structure_report, write_plan_markdown
from tools.task_tags import UI_TAG
from tools.test_runner import run_task_tests as run_task_test_suite


# The style of the detached console window. It is a separate document from the main
# window's (ui/index.html, built by Vite into dist/ as one JS asset and one stylesheet) and
# is handed to pywebview as inline html, so it needs no subresource fetch of its own.
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
    "window.__deepAgentsBus = {"
    "  receive: function (raw) {"
    "    var event;"
    "    try { event = JSON.parse(raw); } catch (err) { return; }"
    "    if (!event || event.type !== 'console_line') return;"
    "    var keys = Object.keys(event);"
    "    for (var i = 0; i < keys.length; i++) {"
    "      if (keys[i] !== 'type' && keys[i] !== 'kind' && keys[i] !== 'text') return;"
    "    }"
    "    if (typeof event.kind !== 'string' || typeof event.text !== 'string') return;"
    "    window.appendLine(event.kind, event.text);"
    "  }"
    "};"
)


# --- Frontend delivery -------------------------------------------------------
#
# The frontend is built by Vite into ``dist/`` and handed to the webview through
# WebView2's own virtual-host mapping, not as a ``file://`` document.
#
# ``file://`` is the historical failure mode of this window: WebView2 drops
# individual subresource fetches from a local document intermittently, and a lost
# module fetch left the window fully rendered but inert -- no click handlers at all,
# which is indistinguishable from a working app because the packaged build runs with
# ``debug=False``. The bundle used to be inlined into ``ui/index.html`` to dodge that.
# Mapping a host name onto the build directory removes the failure at the layer that
# has the bug: the document and every asset it names become ordinary https requests
# WebView2 resolves straight out of the folder, with no HTTP server and no inlining.

ASSET_HOST = "aleth.local"


def frontend_root() -> str:
    """The directory holding the frontend to serve: the Vite build output."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist")


def install_asset_host(folder: str, host: str, fallback_url: str) -> bool:
    """Serves ``folder`` under ``https://<host>/`` inside the WebView2 window.

    Returns True when the mapping is available -- Windows, with pywebview driving the
    EdgeChromium backend -- so the caller can point the window at the mapped URL
    rather than at a file path.

    pywebview 6 exposes no public API for WebView2's
    ``SetVirtualHostNameToFolderMapping``, so the one seam it does expose is used: the
    handler it runs when CoreWebView2 finishes initializing. Wrapping that handler
    installs the mapping *before* pywebview performs the window's first navigation,
    which is the ordering WebView2 requires -- a navigation to a host that is not yet
    mapped fails, and there is no second chance at it.

    ``Allow`` (rather than ``DenyCors``) is deliberate: the preview pane renders
    generated markup in a ``srcdoc`` frame, whose opaque origin would be refused
    access to these assets under the stricter mode.
    """
    if sys.platform != "win32":
        return False
    try:
        # The enum is not re-exported by pywebview, so it is imported from the assembly
        # pywebview has already loaded -- which is also what makes this the right place
        # to fail: an unavailable mapping is reported before the window is created, and
        # the caller falls back to loading the document as a file.
        from webview.platforms import edgechromium
        from Microsoft.Web.WebView2.Core import CoreWebView2HostResourceAccessKind
    except Exception as e:
        print(f"[UI] WebView2 virtual host mapping is unavailable: {e}")
        return False

    original = edgechromium.EdgeChrome.on_webview_ready

    def on_webview_ready(self, sender, args):
        mapped = False
        if args.IsSuccess:
            try:
                sender.CoreWebView2.SetVirtualHostNameToFolderMapping(
                    host, folder, CoreWebView2HostResourceAccessKind.Allow
                )
                mapped = True
            except Exception as e:
                print(f"[UI] Could not map {host} to {folder}: {e}")
        original(self, sender, args)
        if args.IsSuccess and not mapped:
            # The window was pointed at the mapped URL, which cannot resolve without
            # the mapping. Recover by loading the document the way it was loaded
            # before the mapping existed.
            self.load_url(fallback_url)

    edgechromium.EdgeChrome.on_webview_ready = on_webview_ready
    return True


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
        "<title>Aleth \u2022 Console</title>"
        f"<style>{_CONSOLE_WINDOW_STYLE}</style></head>"
        '<body><header><span>Aleth \u2022 Console</span>'
        '<span>detached</span></header>'
        f'<div id="stream">{"".join(rows)}</div>'
        f"<script>{_CONSOLE_WINDOW_APPEND_JS}</script></body></html>"
    )


def _push_console_line_to(window, kind, text) -> None:
    """Pushes one transcript line into the detached window through the bus sink.

    The line travels as a ``console_line`` event validated against the same strict
    contract as every other bus payload, so the second window cannot be handed a shape
    the contract does not describe.
    """
    BridgeBus(WebviewTransport(lambda: window)).dispatch(
        {"type": "console_line", "kind": str(kind), "text": str(text)}
    )


class BridgeAPI:
    """
    Two-way asynchronous bridge connecting PyWebView UI to Python Aleth backend.
    """
    def __init__(self):
        self._window = None
        # The one typed channel to the UI. Built here so it exists before the window is
        # bound; ``WebviewTransport`` no-ops until ``set_window`` supplies one.
        self._bus = BridgeBus(WebviewTransport(lambda: self._window))
        self._execution_thread = None
        self._tagging_thread = None
        # The swarm, built on first use -- never in ``__init__``. Constructing the bridge must not
        # spawn a process pool or dispatch a node: the test suite builds one constantly, and a cold
        # tick there would fire the swarm at fixtures that have no planner.
        self._swarm = None
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
        """Pushes a structured real-time event to the frontend UI through the bus.

        The bus validates ``event_data`` against the strict event union before it is
        serialised, so a drifted payload is reported here rather than delivered. The
        failure is printed with its stack and swallowed: a dropped *terminal* event would
        leave the UI stuck in its running state, so this must never raise into the
        workflow thread (audit M8).
        """
        try:
            self._bus.dispatch(event_data)
        except Exception as e:
            traceback.print_exc()
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
            return {"structured": True, "checked": False, "issues": [], "counts": {},
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

    # -------------------------------------------------------------
    # Execution Lifecycle & Card Orchestration
    # -------------------------------------------------------------
    def _launch_run(self, user_message: str, action_type: str, action_params: dict) -> None:
        """Start a workflow run on a daemon thread. The caller owns the run lock.

        Factored out of ``start_execution`` because approval launches a run too: an
        approved artifact is applied by re-issuing the request that proposed it, and that
        must obey the same lock and the same threading discipline as a user launch.
        """
        def target_runner():
            try:
                registry.run_agent_workflow(
                    (user_message or "").strip(),
                    self.emit_event,
                    action_type=action_type or "custom",
                    action_params=action_params or {}
                )
            finally:
                # The run has ended, so whatever it completed has unblocked its children. The tick
                # is deliberately *outside* the run: the artifact was written to disk by the pass,
                # and no SQLite transaction is held across that I/O or across this dispatch.
                self._tick_swarm()

        self._execution_thread = threading.Thread(target=target_runner, daemon=True)
        self._execution_thread.start()

    def start_execution(self, user_message: str, action_type: str = "custom", action_params: dict = None):
        """
        Starts the multi-agent task execution in an asynchronous background thread.
        Supports intent-driven Gatekeeper actions and administrative bypass.
        """
        if not user_message or not user_message.strip():
            return {"success": False, "error": "Prompt cannot be empty"}

        if self._execution_thread and self._execution_thread.is_alive():
            # Server-authoritative run lock (audit H6/H10). The client lock can be defeated by
            # a reload, a retry, or the normalize path; refusing here is what actually stops
            # two runs from interleaving their plan writes. Previously the old run was asked
            # to stop and joined for one second -- best-effort, and a second thread ran anyway.
            return {"success": False, "error": "A run is already in progress."}

        self._launch_run(user_message, action_type, action_params or {})

        return {"success": True, "status": "started", "message": user_message, "action_type": action_type}

    def get_run_state(self):
        """Whether a workflow is live, so a reloaded UI can re-arm its lock and Stop.

        ``state.isExecuting`` is client-only and resets on reload while this thread keeps
        running, leaving Stop hidden and a second run launchable (audit H6). This is the
        server-side home for that flag.
        """
        return {"running": bool(self._execution_thread and self._execution_thread.is_alive())}

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
        """
        registry.stop_workflow()
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
    def approve_artifact(self, task_id: str, plan_id: str = None):
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
        """
        from tools.execution_gate import approve_artifact as approve
        from tools.workspace import get_active_plan_filename
        from storage.db import plan_id_for

        resolved_plan = plan_id or plan_id_for(get_active_plan_filename())
        result = approve(str(task_id), resolved_plan)
        if not result.get("success"):
            return result

        if self._execution_thread and self._execution_thread.is_alive():
            result["dispatched"] = False
            result["note"] = "Approved; a run is already in progress."
            return result

        self._launch_run(
            "",
            "execute_artifact",
            {"taskId": str(task_id), "planId": resolved_plan},
        )
        result["dispatched"] = True
        return result

    # -------------------------------------------------------------
    # The swarm: reactive dispatch, driven by events
    # -------------------------------------------------------------
    def _ensure_swarm(self):
        """The swarm, built on first use. It owns the pool and the in-flight set."""
        if self._swarm is None:
            from orchestration.swarm import Swarm
            from storage.db import get_store, plan_id_for
            from tools.workspace import get_active_plan_filename, get_project_dir

            self._swarm = Swarm(
                db_path=get_store().path,
                plan_id=plan_id_for(get_active_plan_filename()),
                workspace_dir=get_project_dir(),
            )
        return self._swarm

    def _tick_swarm(self) -> list:
        """Dispatch whatever is runnable right now. Never raises.

        A dispatch failure is reported rather than propagated: the caller is either the tail of a
        finished run or an approval that has already committed its state, and neither should be
        undone by the pool being unavailable.
        """
        try:
            return self._ensure_swarm().tick()
        except Exception as error:
            print(f"[Swarm] tick skipped: {type(error).__name__}: {error}", file=sys.stderr)
            return []

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
                    title="Aleth \u2022 Console",
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
                _push_console_line_to(window, kind, text)
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
                _push_console_line_to(window, pending_kind, pending_text)
            except Exception as e:
                self._console_window = None
                return {"success": False, "error": str(e)}
        try:
            _push_console_line_to(window, kind, text)
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

    # The window is loaded from the Vite build, not from the hand-authored document in
    # ui/. A build that has not been made yet is reported as the build step it is,
    # rather than as a blank window.
    dist = frontend_root()
    index_path = os.path.join(dist, "index.html")
    if not os.path.isfile(index_path):
        print("The frontend has not been built yet: dist/index.html is missing.")
        print("Build it once with:  npm install && npm run build")
        raise SystemExit(1)

    # Mapped delivery where the platform supports it, the file document otherwise, so
    # the app still opens (without the mapped origin's guarantees) on a backend that
    # has no virtual-host mapping.
    if install_asset_host(dist, ASSET_HOST, index_path):
        ui_url = f"https://{ASSET_HOST}/index.html"
    else:
        ui_url = index_path

    window = webview.create_window(
        title="Aleth • Plan Orchestrator Studio",
        url=ui_url,
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