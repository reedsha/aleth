"""Behavioral equivalence harness.

Every assertion here was captured from the pre-refactor implementation and must
keep passing afterwards. Tests exercise the public surface only (the names other
modules import), so they survive internal module reorganization.

    .\\venv\\Scripts\\python.exe -m unittest discover -s tests -t . -v
"""

import os
import shutil
import tempfile
import time
import unittest

from tools import file_tools as ft

PLAN_MD = """# Project Plan: Demo

## 1. Setup
- [x] Scaffolding
  - Files Created/Modified: `setup.py`, `config.py`
- [ ] Pending item
- [-] Working item
- [!] not a real checkbox
- [ ] [UI] Dashboard view

## 2. Build
- [ ] Second section task
"""

RELAXED_MD = """# Notes

1. First numbered item
2. Second numbered item
- Plain bullet item
"""


class PlanParserTests(unittest.TestCase):
    """parse_markdown_to_plan_dict / compile_plan_json_to_markdown / parse_plan_tree"""

    def test_titles_sections_and_statuses(self):
        parsed = ft.parse_markdown_to_plan_dict(PLAN_MD, "PLAN.md")

        self.assertEqual(parsed["version"], "1.0")
        self.assertEqual(parsed["plan_file"], "PLAN.md")
        self.assertEqual(parsed["title"], "Demo")
        self.assertEqual([s["title"] for s in parsed["sections"]], ["1. Setup", "2. Build"])

        steps = parsed["steps"]
        self.assertEqual(
            [s["title"] for s in steps],
            ["Scaffolding", "Pending item", "Working item", "Dashboard view", "Second section task"],
        )
        self.assertEqual(
            [s["status"] for s in steps],
            ["completed", "pending", "in_progress", "pending", "pending"],
        )
        self.assertEqual([s["id"] for s in steps], [f"task-{i}" for i in range(1, 6)])
        self.assertEqual([s["section"] for s in steps][:4], ["1. Setup"] * 4)
        self.assertEqual(steps[4]["section"], "2. Build")

    def test_ui_tag_is_detected_and_stripped(self):
        steps = ft.parse_markdown_to_plan_dict(PLAN_MD)["steps"]
        ui_task = steps[3]
        self.assertTrue(ui_task["is_ui"])
        self.assertEqual(ui_task["title"], "Dashboard view")
        self.assertFalse(steps[0]["is_ui"])

    def test_deliverables_are_kept_as_raw_detail_text(self):
        # NOTE (pre-existing behavior, deliberately preserved): the parser's file
        # extraction regex only matches forms like "Files: a.py" / "File Modified: a.py".
        # The "Files Created/Modified: `a.py`" shape written by the Coder prompt is
        # retained verbatim as a detail but yields NO parsed files. This is captured
        # here so the refactor cannot silently change it.
        steps = ft.parse_markdown_to_plan_dict(PLAN_MD)["steps"]
        self.assertEqual(steps[0]["files"], [])
        self.assertEqual(
            steps[0]["details"],
            ["Files Created/Modified: `setup.py`, `config.py`"],
        )

    def test_file_extraction_for_supported_formats(self):
        md = "# P\n\n## 1. S\n- [x] Done\n  - Files: `a.py`, `b.py`\n"
        steps = ft.parse_markdown_to_plan_dict(md, "P.md")["steps"]
        self.assertEqual(steps[0]["files"], ["a.py", "b.py"])

    def test_non_checkbox_bullet_appends_to_previous_task_details(self):
        steps = ft.parse_markdown_to_plan_dict(PLAN_MD)["steps"]
        # The raw item text (including its leading "[!] ") is preserved verbatim.
        self.assertEqual(steps[2]["details"], ["[!] not a real checkbox"])
        # ...and it does not create a task of its own.
        self.assertNotIn("[!] not a real checkbox", [s["title"] for s in steps])

    def test_metrics(self):
        parsed = ft.parse_markdown_to_plan_dict(PLAN_MD)
        self.assertEqual(
            parsed["metrics"],
            {
                "total_tasks": 5,
                "completed_tasks": 1,
                "in_progress_tasks": 1,
                "pending_tasks": 3,
                "progress_percent": 20,
            },
        )

    def test_relaxed_fallback_for_uncheckboxed_lists(self):
        parsed = ft.parse_markdown_to_plan_dict(RELAXED_MD, "NOTES.md")
        self.assertEqual(parsed["plan_file"], "NOTES.md")
        titles = [s["title"] for s in parsed["steps"]]
        self.assertIn("First numbered item", titles)
        self.assertIn("Second numbered item", titles)
        self.assertIn("Plain bullet item", titles)
        self.assertTrue(all(s["status"] == "pending" for s in parsed["steps"]))

    def test_compile_then_reparse_round_trip(self):
        original = ft.parse_markdown_to_plan_dict(PLAN_MD, "PLAN.md")
        compiled = ft.compile_plan_json_to_markdown(original)

        self.assertTrue(compiled.startswith("# Project Plan: Demo"))
        self.assertIn("- [x] Scaffolding", compiled)
        self.assertIn("- [-] Working item", compiled)
        self.assertIn("- [ ] [UI] Dashboard view", compiled)
        self.assertTrue(compiled.endswith("\n"))

        reparsed = ft.parse_markdown_to_plan_dict(compiled, "PLAN.md")
        self.assertEqual(
            [(s["title"], s["status"], s["is_ui"]) for s in reparsed["steps"]],
            [(s["title"], s["status"], s["is_ui"]) for s in original["steps"]],
        )
        self.assertEqual([len(s["details"]) for s in reparsed["steps"]],
                         [len(s["details"]) for s in original["steps"]])

    def test_parse_plan_tree_returns_steps_only(self):
        self.assertEqual(ft.parse_plan_tree(PLAN_MD), ft.parse_markdown_to_plan_dict(PLAN_MD)["steps"])


class WorkspaceTestCase(unittest.TestCase):
    """Base class: isolates every test in a throwaway workspace.

    The real workspace files are snapshotted and restored so the developer's
    ``my_project_workspace`` is never mutated by the suite.
    """

    _SNAPSHOT_FILES = ("PLAN.md", "plan.json")

    def setUp(self):
        self._orig_dir = ft.get_project_dir()
        self._orig_plan = ft.get_active_plan_filename()
        self._snapshot = {}
        for name in self._SNAPSHOT_FILES:
            path = os.path.join(self._orig_dir, name)
            if os.path.isfile(path):
                with open(path, "rb") as fh:
                    self._snapshot[name] = fh.read()

        self.tmp = tempfile.mkdtemp(prefix="deepagents_chartest_")
        ft.set_project_dir(self.tmp)
        ft.set_active_plan_filename("PLAN.md")

    def tearDown(self):
        ft.set_project_dir(self._orig_dir)
        ft.set_active_plan_filename(self._orig_plan)
        for name in self._SNAPSHOT_FILES:
            path = os.path.join(self._orig_dir, name)
            if name in self._snapshot:
                with open(path, "wb") as fh:
                    fh.write(self._snapshot[name])
            elif os.path.isfile(path):
                os.remove(path)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, filename, content):
        with open(os.path.join(self.tmp, filename), "w", encoding="utf-8") as fh:
            fh.write(content)
        return os.path.join(self.tmp, filename)

    def read(self, filename):
        with open(os.path.join(self.tmp, filename), "r", encoding="utf-8") as fh:
            return fh.read()

    # --- plan-state fixtures ---------------------------------------------------
    #
    # load_plan_state() rehydrates plan.json from the markdown plan whenever the
    # markdown mtime is strictly greater. save_plan_state() writes plan.json and
    # then PLAN.md microseconds apart, and the system clock tick (~15.6ms on
    # Windows) makes that comparison a coin flip -- identical more often than not,
    # but occasionally plan.json lands one tick earlier.
    #
    # That matters because the markdown compiler never emits a Files line, so any
    # rehydration silently discards every task's `files` (and re-parses details as
    # loose bullets). Tests that seed machine state must therefore pin plan.json as
    # the authoritative side, otherwise audit/rollback assertions flake.

    def pin_plan_json_authoritative(self):
        """Make plan.json strictly newer than the markdown plan."""
        json_path = ft.get_plan_json_path()
        md_path = os.path.join(self.tmp, ft.get_active_plan_filename())
        base = os.path.getmtime(md_path) if os.path.exists(md_path) else time.time()
        os.utime(json_path, (base + 1, base + 1))

    def save_state(self, plan_dict):
        """save_plan_state, with plan.json pinned as authoritative afterwards."""
        state = ft.save_plan_state(plan_dict)
        self.pin_plan_json_authoritative()
        return state

    def read_state(self):
        """load_plan_state, with plan.json pinned as authoritative first."""
        self.pin_plan_json_authoritative()
        return ft.load_plan_state()


class FileOpTests(WorkspaceTestCase):
    """read_file / write_file / append_to_file / list_workspace_files."""

    def test_missing_file_message(self):
        self.assertEqual(ft.read_file.invoke({"filename": "nope.py"}), "File nope.py does not exist yet.")

    def test_write_then_read_round_trip(self):
        result = ft.write_file.invoke({"filename": "pkg/mod.py", "content": "x = 1\n"})
        self.assertEqual(result, "Successfully wrote 6 characters to pkg/mod.py.")
        self.assertEqual(ft.read_file.invoke({"filename": "pkg/mod.py"}), "x = 1\n")

    def test_append(self):
        ft.write_file.invoke({"filename": "a.txt", "content": "one"})
        ft.append_to_file.invoke({"filename": "a.txt", "content": "two"})
        self.assertEqual(ft.read_file.invoke({"filename": "a.txt"}), "one\ntwo\n")

    def test_large_file_middle_truncation(self):
        ft.write_file.invoke({"filename": "big.py", "content": "A" * 7000 + "B" * 7000})
        content = ft.read_file.invoke({"filename": "big.py"})
        self.assertIn("Omitted 8000 characters", content)
        self.assertTrue(content.startswith("A" * 3000))
        self.assertTrue(content.endswith("B" * 3000))

    def test_plan_files_are_never_truncated(self):
        ft.write_file.invoke({"filename": "BIG.md", "content": "Z" * 9000})
        self.assertEqual(ft.read_file.invoke({"filename": "BIG.md"}), "Z" * 9000)

    def test_list_workspace_files_prunes_internal_dirs(self):
        ft.write_file.invoke({"filename": "sub/a.py", "content": ""})
        ft.write_file.invoke({"filename": "__pycache__/b.pyc", "content": ""})
        ft.write_file.invoke({"filename": ".deepagents_backups/task-1/c.py", "content": ""})
        ft.write_file.invoke({"filename": "node_modules/d.js", "content": ""})

        paths = [f["path"] for f in ft.list_workspace_files()]
        self.assertIn("sub/a.py", paths)
        self.assertNotIn("__pycache__/b.pyc", paths)
        self.assertNotIn(".deepagents_backups/task-1/c.py", paths)
        self.assertNotIn("node_modules/d.js", paths)

    def test_coder_tool_bundle_contents(self):
        names = [t.name for t in ft.all_file_tools]
        self.assertEqual(names, ["read_file", "write_file", "append_to_file"])


class PlanStateTests(WorkspaceTestCase):
    """load_plan_state / save_plan_state / update_plan_task_status / sync_plan_on_disk."""

    def _save_demo(self):
        return self.save_state(ft.parse_markdown_to_plan_dict(PLAN_MD, "PLAN.md"))

    def test_save_writes_both_representations_with_metrics(self):
        saved = self._save_demo()

        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "plan.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "PLAN.md")))
        self.assertEqual(saved["metrics"]["total_tasks"], 5)
        self.assertEqual(saved["metrics"]["progress_percent"], 20)
        self.assertEqual(len(saved["steps"]), 5)
        self.assertEqual(saved["plan_file"], "PLAN.md")
        self.assertEqual(saved["steps"][0]["section"], "1. Setup")

    def test_load_returns_empty_skeleton_when_nothing_exists(self):
        state = ft.load_plan_state()
        self.assertEqual(state["sections"], [])
        self.assertEqual(state["steps"], [])
        self.assertEqual(state["title"], "New Project Plan")
        self.assertEqual(state["metrics"]["total_tasks"], 0)
        self.assertEqual(state["plan_file"], "PLAN.md")

    def test_load_rehydrates_when_markdown_is_newer(self):
        self.write("PLAN.md", PLAN_MD)
        state = ft.load_plan_state()
        self.assertEqual(len(state["steps"]), 5)

        self.write("PLAN.md", "# Project Plan: Changed\n\n## 1. Solo\n- [ ] Only task\n")
        os.utime(os.path.join(self.tmp, "PLAN.md"), (9e9, 9e9))
        state = ft.load_plan_state()
        self.assertEqual(state["title"], "Changed")
        self.assertEqual([s["title"] for s in state["steps"]], ["Only task"])

    def test_update_plan_task_status(self):
        self._save_demo()
        ft.update_plan_task_status("task-2", "completed", detail_note="done by test", files=["z.py"])
        state = self.read_state()
        task = state["steps"][1]
        self.assertEqual(task["status"], "completed")
        self.assertIn("done by test", task["details"])
        self.assertIn("z.py", task["files"])

    def test_update_plan_task_status_unknown_id_is_noop(self):
        self._save_demo()
        before = self.read_state()
        ft.update_plan_task_status("does-not-exist", "completed")
        after = self.read_state()
        self.assertEqual([s["status"] for s in before["steps"]], [s["status"] for s in after["steps"]])

    def test_sync_plan_on_disk_returns_state(self):
        self._save_demo()
        self.assertEqual(len(ft.sync_plan_on_disk()["steps"]), 5)

    def test_rehydration_side_decides_whether_files_survive(self):
        """Pins a pre-existing markdown round-trip limitation.

        compile_plan_json_to_markdown() emits statuses and details but never a
        Files line, so whichever side load_plan_state() prefers decides whether
        deliverables survive. The refactor preserves this behaviour verbatim; if
        it ever changes, audit and rollback results change with it, so this test
        is here to fail loudly on that day.
        """
        self.save_state({
            "title": "Round Trip",
            "sections": [{"id": "sec-1", "title": "1. S", "tasks": [
                {"id": "task-1", "section": "1. S", "title": "Alpha", "status": "completed",
                 "is_ui": False, "details": ["note one"], "files": ["alpha.py"]},
                {"id": "task-2", "section": "1. S", "title": "Beta", "status": "pending",
                 "is_ui": False, "details": [], "files": ["beta.py"]},
            ]}],
        })

        # 1. plan.json authoritative -> deliverables intact.
        from_json = self.read_state()
        self.assertEqual([t["files"] for t in from_json["steps"]], [["alpha.py"], ["beta.py"]])

        # 2. markdown strictly newer -> deliverables dropped, details retained.
        md_path = os.path.join(self.tmp, "PLAN.md")
        bumped = os.path.getmtime(md_path) + 5
        os.utime(md_path, (bumped, bumped))
        from_md = ft.load_plan_state()
        self.assertEqual([t["files"] for t in from_md["steps"]], [[], []])
        self.assertEqual([t["status"] for t in from_md["steps"]], ["completed", "pending"])
        self.assertEqual([t["details"] for t in from_md["steps"]], [["note one"], []])

    def test_list_plan_files(self):
        self.write("PLAN.md", PLAN_MD)
        self.write("ROADMAP.md", PLAN_MD)
        self.write("notes.txt", "x")
        self.assertEqual(ft.list_plan_files(), ["PLAN.md", "ROADMAP.md"])


class BackupAuditRollbackTests(WorkspaceTestCase):
    """Backup snapshots, two-phase audit, sync resolution and rollback."""

    def _seed_completed_task_with_missing_deliverable(self):
        self.save_state({
            "title": "Audit Demo",
            "sections": [{
                "id": "sec-1",
                "title": "1. Setup",
                "tasks": [
                    {
                        "id": "task-1", "section": "1. Setup", "title": "Has its file",
                        "status": "completed", "is_ui": False, "details": [], "files": ["kept.py"],
                    },
                    {
                        "id": "task-2", "section": "1. Setup", "title": "Missing its file",
                        "status": "completed", "is_ui": False, "details": [], "files": ["gone.py"],
                    },
                    {
                        "id": "task-3", "section": "1. Setup", "title": "Pending but built",
                        "status": "pending", "is_ui": False, "details": [], "files": ["built.py"],
                    },
                ],
            }],
        })
        ft.write_file.invoke({"filename": "kept.py", "content": "k\n"})
        ft.write_file.invoke({"filename": "built.py", "content": "b\n"})

    def test_backup_records_modified_and_created(self):
        ft.write_file.invoke({"filename": "existing.py", "content": "v1\n"})
        backup_path = ft.backup_file_for_task("task-9", "existing.py")
        self.assertTrue(backup_path and os.path.isfile(backup_path))

        created = ft.backup_file_for_task("task-9", "brand_new.py")
        self.assertIsNone(created)

        meta = self.read(os.path.join(".deepagents_backups", "task-9", "_meta.json"))
        self.assertIn('"action": "modified"', meta)
        self.assertIn('"action": "created"', meta)

    def test_audit_detects_both_discrepancy_classes(self):
        self._seed_completed_task_with_missing_deliverable()
        ft.write_file.invoke({"filename": "untracked.py", "content": "u\n"})

        audit = ft.audit_codebase_plan_sync()
        self.assertFalse(audit["in_sync"])
        self.assertEqual(audit["discrepancy_count"], 2)
        self.assertEqual([t["task_id"] for t in audit["completed_missing_files"]], ["task-2"])
        self.assertEqual([t["task_id"] for t in audit["pending_existing_files"]], ["task-3"])
        self.assertIn("untracked.py", audit["untracked_files"])
        self.assertIn("Detected 2 discrepancy(ies)", audit["summary"])

    def test_audit_clean_workspace(self):
        self.save_state({
            "title": "Clean",
            "sections": [{"id": "sec-1", "title": "1. S", "tasks": [
                {"id": "task-1", "section": "1. S", "title": "Done", "status": "completed",
                 "is_ui": False, "details": [], "files": ["only.py"]},
            ]}],
        })
        ft.write_file.invoke({"filename": "only.py", "content": "x\n"})

        audit = ft.audit_codebase_plan_sync()
        self.assertTrue(audit["in_sync"])
        self.assertEqual(audit["discrepancy_count"], 0)
        self.assertEqual(audit["summary"], "Codebase and plan are in 100% synchronization.")

    def test_resolve_sync_plan_to_codebase_marks_pending_complete(self):
        self._seed_completed_task_with_missing_deliverable()
        res = ft.resolve_sync_plan_to_codebase()

        self.assertTrue(res["success"])
        self.assertEqual(res["action"], "plan_to_codebase")
        self.assertEqual(res["modified_tasks"], ["Pending but built"])
        statuses = {s["id"]: s["status"] for s in res["tree"]}
        self.assertEqual(statuses["task-2"], "completed")
        self.assertEqual(statuses["task-3"], "completed")

    def test_resolve_sync_code_to_plan_resets_missing(self):
        self._seed_completed_task_with_missing_deliverable()
        res = ft.resolve_sync_code_to_plan()

        self.assertTrue(res["success"])
        self.assertEqual(res["action"], "code_to_plan")
        self.assertEqual(res["modified_tasks"], ["Missing its file"])
        statuses = {s["id"]: s["status"] for s in res["tree"]}
        self.assertEqual(statuses["task-1"], "completed")
        self.assertEqual(statuses["task-2"], "pending")
        self.assertEqual(statuses["task-3"], "pending")

    def test_rollback_restores_modified_file_and_archives_new_file(self):
        ft.write_file.invoke({"filename": "restored.py", "content": "original\n"})
        ft.backup_file_for_task("task-1", "restored.py")
        ft.write_file.invoke({"filename": "restored.py", "content": "patched\n"})

        ft.backup_file_for_task("task-1", "flaky.py")
        ft.write_file.invoke({"filename": "flaky.py", "content": "new\n"})

        self.save_state({
            "title": "RB",
            "sections": [{"id": "sec-1", "title": "1. S", "tasks": [
                {"id": "task-1", "section": "1. S", "title": "Rollback me", "status": "completed",
                 "is_ui": False, "details": [], "files": ["restored.py", "flaky.py"]},
            ]}],
        })

        res = ft.rollback_task_state("task-1")
        self.assertTrue(res["success"])
        self.assertEqual(res["task_title"], "Rollback me")
        self.assertEqual(res["tree"][0]["status"], "pending")
        self.assertEqual(self.read("restored.py"), "original\n")
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "flaky.py.rollback_bak")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "flaky.py")))

    def test_rollback_unknown_task(self):
        self._seed_completed_task_with_missing_deliverable()
        res = ft.rollback_task_state("ghost")
        self.assertFalse(res["success"])
        self.assertIn("not found in active plan", res["error"])


class RegistryContractTests(WorkspaceTestCase):
    """Agent introspection surface consumed by the PyWebView bridge and UI."""

    def test_agent_summary_shape(self):
        import registry as registry_module

        summary = registry_module.registry.get_agent_summary()
        self.assertEqual(set(summary), {"main_agents", "coder_agents", "workspace_dir", "active_plan", "plan_files"})
        self.assertEqual(summary["workspace_dir"], os.path.abspath(self.tmp))
        self.assertEqual(summary["active_plan"], "PLAN.md")

        self.assertEqual([a["id"] for a in summary["main_agents"]], ["software-architect"])
        architect = summary["main_agents"][0]
        self.assertEqual(architect["type"], "main")
        self.assertEqual(architect["role"], "Coordinator")
        self.assertEqual(architect["model"], "openai:policy/architect")
        self.assertEqual(architect["subagents"], ["coder-deep", "coder-standard"])
        self.assertEqual(architect["status"], "ready")
        self.assertIn("execute_restricted_command", architect["tools"])
        self.assertTrue(architect["file_path"].endswith("architect.py"))

        self.assertEqual([a["id"] for a in summary["coder_agents"]], ["coder-deep", "coder-standard"])
        deep = summary["coder_agents"][0]
        self.assertEqual(deep["type"], "coder")
        self.assertEqual(deep["role"], "Sub-Agent")
        self.assertEqual(deep["parent_agent"], "software-architect")
        self.assertEqual(deep["display_name"], "Senior Backend Coder")
        self.assertIn("execute_shell_command", deep["tools"])

    def test_get_agent_lookup(self):
        import registry as registry_module

        reg = registry_module.registry
        self.assertEqual(reg.get_agent("software-architect")["id"], "software-architect")
        self.assertEqual(reg.get_agent("coder-deep")["id"], "coder-deep")
        self.assertIsNone(reg.get_agent("nobody"))

    def test_prompt_variable_resolution(self):
        import registry as registry_module

        self.assertEqual(
            registry_module.registry.resolve_prompt_variables("see {{ACTIVE_PLAN_FILE}} now"),
            "see PLAN.md now",
        )
        self.assertEqual(registry_module.registry.resolve_prompt_variables(""), "")

    def test_scan_agents_is_idempotent(self):
        import registry as registry_module

        reg = registry_module.registry
        first = reg.get_agent_summary()
        reg.scan_agents()
        second = reg.get_agent_summary()
        self.assertEqual([a["id"] for a in first["main_agents"]], [a["id"] for a in second["main_agents"]])
        self.assertEqual([a["id"] for a in first["coder_agents"]], [a["id"] for a in second["coder_agents"]])


class WorkflowEventContractTests(WorkspaceTestCase):
    """Locks the ordered event stream the frontend consumes.

    Only the admin-bypass actions are covered here because they touch no
    subprocesses and are therefore deterministic and fast.
    """

    def _collect(self, action_type, message, params=None):
        import registry as registry_module

        # Registry reads plan state at the top of the workflow; pin it so the
        # rehydration branch cannot flip mid-suite.
        self.pin_plan_json_authoritative()
        events = []
        registry_module.registry.run_agent_workflow(
            message, events.append, action_type=action_type, action_params=params or {}
        )
        return events

    def test_create_plan_file_scaffolds_roadmap(self):
        import registry as registry_module

        res = registry_module.registry.create_new_plan_file("PLAN.md", "A weather microservice")
        self.assertTrue(res["success"])
        self.assertEqual(res["filename"], "PLAN.md")
        self.assertEqual(len(res["tree"]), 8)
        self.assertTrue(res["content"].startswith("# Project Plan: A weather microservice"))
        self.assertTrue(registry_module.registry.get_current_plan_data()["exists"])
        self.assertEqual(res["tree"][0]["status"], "completed")

    def test_recommend_action_event_sequence(self):
        import registry as registry_module

        registry_module.registry.create_new_plan_file("PLAN.md", "Demo roadmap")
        events = self._collect("recommend", "[ACTION: RECOMMEND_NEXT_STEPS]")

        self.assertEqual(
            [(e["type"], e.get("agent")) for e in events],
            [
                ("workflow_started", None),
                ("architect_spawn", "software-architect"),
                ("log", "software-architect"),
                ("log", "software-architect"),
                ("tool_call", "software-architect"),
                ("tool_result", "software-architect"),
                ("log", "software-architect"),
                ("log", "software-architect"),
                ("log", "software-architect"),
                ("architect_summary", "software-architect"),
                ("workflow_complete", None),
            ],
        )
        self.assertEqual(events[0]["action_type"], "recommend")
        self.assertEqual(events[0]["plan_file"], "PLAN.md")
        self.assertEqual(events[-1]["status"], "finished")
        summary = events[-2]["summary"]
        self.assertEqual(summary["title"], "Architect Strategic Roadmap Recommendations")
        self.assertEqual(summary["status"], "Advisory Formulated")
        # The first pending task is not UI-tagged, so the vision proposal is omitted.
        self.assertEqual(len(summary["proposals"]), 2)
        self.assertTrue(summary["proposals"][0].startswith("Execute immediate next task:"))

    def test_update_plan_action_event_sequence(self):
        import registry as registry_module

        registry_module.registry.create_new_plan_file("PLAN.md", "Demo roadmap")
        events = self._collect("update_plan", "Instructions: add task: wire up telemetry")

        self.assertEqual(
            [e["type"] for e in events],
            [
                "workflow_started",
                "architect_spawn",
                "log", "log",
                "tool_call", "tool_result",
                "log",
                "tool_call", "tool_result",
                "plan_updated",
                "log", "log",
                "architect_summary",
                "workflow_complete",
            ],
        )
        plan_event = [e for e in events if e["type"] == "plan_updated"][0]
        self.assertEqual(plan_event["filename"], "PLAN.md")
        titles = [t["title"] for t in plan_event["tree"]]
        self.assertIn("wire up telemetry", titles)

    def test_action_intent_detected_from_message_tag(self):
        import registry as registry_module

        registry_module.registry.create_new_plan_file("PLAN.md", "Demo roadmap")
        events = self._collect("custom", "[ACTION: ANALYZE_CODEBASE]")

        self.assertEqual(events[0]["action_type"], "analyze")
        self.assertEqual(events[-1]["status"], "finished")

    def test_stop_workflow_signals_the_stop_event(self):
        import registry as registry_module

        reg = registry_module.registry
        reg.stop_event.clear()
        reg.stop_workflow()
        self.assertTrue(reg.stop_event.is_set())
        reg.stop_event.clear()


class FacadeContractTests(WorkspaceTestCase):
    """tools.file_tools is a re-export shell over the decomposed modules.

    These assertions pin the two things a facade is uniquely capable of breaking:
    the set of names it publishes, and whether the mutable workspace globals it
    forwards stay live rather than freezing at import time.
    """

    # Names that other modules import from tools.file_tools (plus the helpers that
    # only ever appear via the facade). Adding to this list is safe; dropping any
    # name from the module is a breaking change.
    EXPECTED_EXPORTS = (
        "get_project_dir", "set_project_dir", "get_active_plan_filename",
        "set_active_plan_filename", "get_plan_json_path", "list_plan_files",
        "walk_workspace", "IGNORE_DIRS", "PROJECT_DIR", "ACTIVE_PLAN_FILE",
        "parse_markdown_to_plan_dict", "compile_plan_json_to_markdown",
        "parse_plan_tree", "load_plan_state", "save_plan_state",
        "sync_plan_on_disk", "update_plan_task_status", "read_file", "write_file",
        "append_to_file", "list_workspace_files", "all_file_tools",
        "MAX_FILE_READ_CHARS", "get_backup_dir", "backup_file_for_task",
        "audit_codebase_plan_sync", "resolve_sync_plan_to_codebase",
        "resolve_sync_code_to_plan", "rollback_task_state", "PLAN_JSON_FILE",
        "BACKUP_SUBDIR",
    )

    def test_all_public_names_resolve(self):
        for name in self.EXPECTED_EXPORTS:
            with self.subTest(name=name):
                self.assertTrue(
                    hasattr(ft, name),
                    "tools.file_tools no longer exposes " + name,
                )

    def test_mutable_globals_are_proxied_live(self):
        """A frozen re-export would still report the plan that was active at import."""
        self.assertEqual(ft.ACTIVE_PLAN_FILE, "PLAN.md")
        ft.set_active_plan_filename("Switched.md")
        self.assertEqual(ft.ACTIVE_PLAN_FILE, "Switched.md")
        self.assertEqual(ft.PROJECT_DIR, ft.get_project_dir())

    def test_star_import_yields_identical_tool_objects(self):
        """Agent definitions hold these same objects; copies would diverge."""
        namespace = {}
        exec("from tools.file_tools import *", namespace)
        self.assertIs(namespace["write_file"], ft.write_file)
        self.assertIs(namespace["all_file_tools"], ft.all_file_tools)
        self.assertEqual(len(namespace["all_file_tools"]), 3)


class PromptEditorTests(unittest.TestCase):
    """save_system_prompt rewrites the agents/ data files in place.

    Those modules are simultaneously Python and the store for the editable prompts,
    so both the on-disk text and the in-memory globals are part of the contract.
    The real sources are never touched here: ``__file__`` is redirected at
    throwaway copies, and the prompt globals are restored in tearDown.
    """

    def setUp(self):
        import agents.architect as arch_mod
        import agents.coders as coders_mod
        import registry as registry_module

        self.arch_mod = arch_mod
        self.coders_mod = coders_mod
        self.registry = registry_module.registry

        self.tmp = tempfile.mkdtemp(prefix="deepagents_prompttest_")
        self._orig = {
            "arch_file": arch_mod.__file__,
            "coders_file": coders_mod.__file__,
            "arch_system": arch_mod.ARCHITECT_SYSTEM_PROMPT,
            "arch_custom": arch_mod.ARCHITECT_CUSTOM_INSTRUCTIONS,
            "deep_custom": coders_mod.CODER_DEEP_CUSTOM_INSTRUCTIONS,
            "deep_system": coders_mod.CODER_DEEP_SYSTEM_PROMPT,
        }
        shutil.copyfile(arch_mod.__file__, os.path.join(self.tmp, "architect.py"))
        shutil.copyfile(coders_mod.__file__, os.path.join(self.tmp, "coders.py"))
        arch_mod.__file__ = os.path.join(self.tmp, "architect.py")
        coders_mod.__file__ = os.path.join(self.tmp, "coders.py")

    def tearDown(self):
        self.arch_mod.__file__ = self._orig["arch_file"]
        self.coders_mod.__file__ = self._orig["coders_file"]
        self.arch_mod.ARCHITECT_SYSTEM_PROMPT = self._orig["arch_system"]
        self.arch_mod.ARCHITECT_CUSTOM_INSTRUCTIONS = self._orig["arch_custom"]
        self.coders_mod.CODER_DEEP_CUSTOM_INSTRUCTIONS = self._orig["deep_custom"]
        self.coders_mod.CODER_DEEP_SYSTEM_PROMPT = self._orig["deep_system"]
        self.coders_mod.coder_deep["system_prompt"] = self._orig["deep_system"]
        self.arch_mod.build_architect_agent()
        self.registry.scan_agents()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def written(self, name):
        with open(os.path.join(self.tmp, name), "r", encoding="utf-8") as fh:
            return fh.read()

    def test_custom_only_edit_protects_core_and_patches_source(self):
        result = self.registry.save_system_prompt(
            "software-architect", "Use tabs.", is_custom_only=True
        )

        self.assertTrue(result["success"], result)
        self.assertEqual(result["message"], "Successfully updated system prompt for software-architect")
        self.assertEqual(self.arch_mod.ARCHITECT_CUSTOM_INSTRUCTIONS, "Use tabs.")
        self.assertEqual(
            self.arch_mod.ARCHITECT_SYSTEM_PROMPT,
            self.arch_mod.ARCHITECT_CORE_PROMPT + "\n\n### Custom Developer Directives:\nUse tabs.",
        )

        on_disk = self.written("architect.py")
        self.assertIn('ARCHITECT_CUSTOM_INSTRUCTIONS = """Use tabs."""', on_disk)
        self.assertIn(self.arch_mod.ARCHITECT_CORE_PROMPT, on_disk)

    def test_coder_custom_only_edit_updates_dict_and_source(self):
        result = self.registry.save_system_prompt(
            "coder-deep", "Prefer dataclasses.", is_custom_only=True
        )

        self.assertTrue(result["success"], result)
        self.assertEqual(self.coders_mod.CODER_DEEP_CUSTOM_INSTRUCTIONS, "Prefer dataclasses.")
        self.assertEqual(
            self.coders_mod.coder_deep["system_prompt"],
            self.coders_mod.CODER_DEEP_SYSTEM_PROMPT,
        )
        self.assertIn(
            self.coders_mod.CODER_DEEP_CORE_PROMPT + "\n\n### Custom Developer Directives:\nPrefer dataclasses.",
            self.coders_mod.coder_deep["system_prompt"],
        )
        self.assertIn(
            'CODER_DEEP_CUSTOM_INSTRUCTIONS = """Prefer dataclasses."""',
            self.written("coders.py"),
        )

    def test_unknown_agent_is_reported_not_raised(self):
        self.assertEqual(
            self.registry.save_system_prompt("ghost", "x"),
            {"success": False, "error": "Agent ghost not found in registry."},
        )

    def test_prompt_globals_stay_fully_reloadable(self):
        """The catalog must reflect the edit immediately (hot reload, no restart)."""
        self.registry.save_system_prompt("software-architect", "Be terse.", is_custom_only=True)

        architect = self.registry.get_agent("software-architect")
        self.assertEqual(architect["custom_instructions"], "Be terse.")
        self.assertIn("Be terse.", architect["system_prompt"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
