import os
import sys
import json
import threading
import webview

from env_boot import load_environment

# Load environment configuration. ``.env`` deliberately wins over the process
# environment; an exported name that shadowed it is reported rather than silently used.
for _shadowed in load_environment():
    print(f"[Config] .env overrides the exported {_shadowed}")

from registry import registry
from tools.file_tools import (
    get_project_dir,
    set_project_dir,
    list_workspace_files,
    get_active_plan_filename,
    set_active_plan_filename,
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

class BridgeAPI:
    """
    Two-way asynchronous bridge connecting PyWebView UI to Python DeepAgents backend.
    """
    def __init__(self):
        self._window = None
        self._execution_thread = None

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

    def set_active_plan(self, filename: str):
        """Switches the active plan file, re-hydrates state, and notifies the UI."""
        clean_name = set_active_plan_filename(filename)
        load_plan_state(force_sync=True)
        data = registry.get_current_plan_data()
        # NOTE: No emit_event here — the JS chip handler applies data directly from
        # the return value. A redundant plan_updated emit races and can revert the switch.
        return data


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
        write_file.invoke({"filename": clean_name, "content": content})
        parsed = parse_markdown_to_plan_dict(content, clean_name)
        saved = save_plan_state(parsed)
        data = {
            "success": True,
            "filename": clean_name,
            "content": content,
            "tree": saved.get("steps", []),
            "plan_json": saved,
            "plans": list_plan_files()
        }
        self.emit_event({
            "type": "plan_updated",
            "filename": clean_name,
            "content": content,
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

    def stop_execution(self):
        """Signals active workflow to stop and emit completion."""
        registry.stop_workflow()
        self.emit_event({
            "type": "workflow_stopped",
            "message": "Task halted by user."
        })
        return {"status": "stopped"}

    def retry_execution(self, agent_id: str = None, user_message: str = None):
        """Retries the execution from the failed agent or last message."""
        prompt = user_message or "Retry current architectural step"
        return self.start_execution(prompt)


def main(argv=None):
    """Launch the desktop UI.

    ``--debug`` opens the WebView2 DevTools. The window normally runs with
    ``debug=False``, which makes an uncaught error or a failed resource invisible:
    the window renders but nothing responds. DevTools exposes the console and the
    network log for exactly that case.
    """
    argv = sys.argv[1:] if argv is None else argv
    debug = "--debug" in argv

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