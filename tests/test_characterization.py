"""Behavioral equivalence harness.

Every assertion here was captured from the pre-refactor implementation and must
keep passing afterwards. Tests exercise the public surface only (the names other
modules import), so they survive internal module reorganization.

    .\\venv\\Scripts\\python.exe -m unittest discover -s tests -t . -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tools import file_tools as ft
from tools.task_tags import UI_TAG
from orchestration.workflow import templates

PLAN_MD = """# Project Plan: Demo

## 1. Setup
- [x] Scaffolding
  - Files Created/Modified: `setup.py`, `config.py`
- [ ] Pending item
- [-] Working item
- [!] not a real checkbox
- [ ] [FE] Dashboard view

## 2. Build
- [ ] Second section task
"""

RELAXED_MD = """# Notes

1. First numbered item
2. Second numbered item
- Plain bullet item
"""

NESTED_MD = """# Project Plan: Steps

## 🌍 Global State Summary
- **Architecture:** dual-brain orchestration.
- **Target Upgrade:** sub-step hierarchy.

---

## 1. Build
- [ ] Parent task
  - [ ] Child one
  - [ ] Child two
    - [ ] Grandchild
- [x] Childless task
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
        self.assertEqual(ui_task["tag"], UI_TAG)
        self.assertEqual(ui_task["title"], "Dashboard view")
        self.assertIsNone(steps[0]["tag"])

    def test_deliverables_are_kept_as_raw_detail_text(self):
        # NOTE (pre-existing behavior, deliberately preserved): the parser's file
        # extraction regex only matches forms like "Files: a.py" / "File Modified: a.py".
        # The "Files Created/Modified: `a.py`" shape written by the Coder prompt is
        # retained verbatim as a detail but yields NO parsed files. This is captured
        # here so the refactor cannot silently change it -- extracting it would move
        # paths into `files` and alter audit_codebase_plan_sync() results.
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

    def test_deliverables_declared_after_other_details_are_extracted(self):
        # Regression: the task scan stops at the first nested list_item_close, so any
        # bullet after the first reaches the fallthrough branch. It must sort detail
        # lines into `files` the same way, or compile->reparse drops deliverables.
        md = "# P\n\n## 1. S\n- [x] Alpha\n  - note one\n  - Files: `alpha.py`, `shared.py`\n"
        steps = ft.parse_markdown_to_plan_dict(md, "P.md")["steps"]
        self.assertEqual(steps[0]["details"], ["note one"])
        self.assertEqual(steps[0]["files"], ["alpha.py", "shared.py"])

    def test_compile_then_reparse_preserves_deliverables(self):
        original = {
            "title": "Round Trip",
            "sections": [{"id": "sec-1", "title": "1. S", "tasks": [
                {"id": "task-1", "section": "1. S", "title": "Alpha", "status": "completed",
                 "details": ["note one"], "files": ["alpha.py", "shared.py"]},
                {"id": "task-2", "section": "1. S", "title": "Beta", "status": "pending",
                 "details": [], "files": ["beta.py"]},
                {"id": "task-3", "section": "1. S", "title": "Gamma", "status": "in_progress",
                 "tag": "FE", "details": ["Assigned: coder-deep"], "files": ["gamma.py"]},
            ]}],
        }

        compiled = ft.compile_plan_json_to_markdown(original)
        reparsed = ft.parse_markdown_to_plan_dict(compiled, "PLAN.md")

        self.assertEqual(
            [("t", s["title"], s["status"], s.get("tag"), s["details"], s["files"]) for s in reparsed["steps"]],
            [("t", s["title"], s["status"], s.get("tag"), s["details"], s["files"]) for s in original["sections"][0]["tasks"]],
        )
        # A second compile must be byte-identical, so the round-trip has converged.
        self.assertEqual(ft.compile_plan_json_to_markdown(reparsed), compiled)

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
        self.assertIn("- [ ] [FE] Dashboard view", compiled)
        self.assertTrue(compiled.endswith("\n"))

        reparsed = ft.parse_markdown_to_plan_dict(compiled, "PLAN.md")
        self.assertEqual(
            [(s["title"], s["status"], s["tag"]) for s in reparsed["steps"]],
            [(s["title"], s["status"], s["tag"]) for s in original["steps"]],
        )
        self.assertEqual([len(s["details"]) for s in reparsed["steps"]],
                         [len(s["details"]) for s in original["steps"]])

    def test_parse_plan_tree_returns_steps_only(self):
        self.assertEqual(ft.parse_plan_tree(PLAN_MD), ft.parse_markdown_to_plan_dict(PLAN_MD)["steps"])

    def test_nested_checkboxes_become_sub_steps_instead_of_tasks(self):
        # A 6-line roadmap used to parse as 4 milestones: each parent absorbed its first
        # nested checkbox into `details` and hardened every later one into a sibling task.
        # Sub-steps are part of their parent card, so only the real milestones remain.
        parsed = ft.parse_markdown_to_plan_dict(NESTED_MD, "PLAN.md")
        steps = parsed["steps"]

        self.assertEqual([s["title"] for s in steps], ["Parent task", "Childless task"])
        self.assertEqual([s["status"] for s in steps], ["pending", "completed"])
        self.assertEqual(parsed["metrics"]["total_tasks"], 2)
        self.assertEqual(parsed["metrics"]["pending_tasks"], 1)

    def test_sub_steps_flatten_both_indent_depths_under_their_parent(self):
        steps = ft.parse_markdown_to_plan_dict(NESTED_MD, "PLAN.md")["steps"]
        parent = steps[0]

        self.assertEqual(
            [s["title"] for s in parent["sub_steps"]],
            ["Child one", "Child two", "Grandchild"],
        )
        self.assertEqual([s["status"] for s in parent["sub_steps"]], ["pending"] * 3)
        # Sub-steps own their lines, so the parent must not also carry them as details.
        self.assertEqual(parent["details"], [])
        self.assertEqual(parent["files"], [])
        self.assertEqual(steps[1]["sub_steps"], [])

    def test_sub_steps_survive_compile_then_reparse(self):
        parsed = ft.parse_markdown_to_plan_dict(NESTED_MD, "PLAN.md")
        compiled = ft.compile_plan_json_to_markdown(parsed)
        self.assertIn("  - [ ] Child one", compiled)

        reparsed = ft.parse_markdown_to_plan_dict(compiled, "PLAN.md")
        self.assertEqual(
            [s["title"] for s in reparsed["steps"][0]["sub_steps"]],
            [s["title"] for s in parsed["steps"][0]["sub_steps"]],
        )
        self.assertEqual(reparsed["metrics"]["total_tasks"], 2)
        # The round trip must have converged, not grown another generation of tasks.
        self.assertEqual(ft.compile_plan_json_to_markdown(reparsed), compiled)

    def test_global_state_summary_is_captured_and_is_not_a_section(self):
        parsed = ft.parse_markdown_to_plan_dict(NESTED_MD, "PLAN.md")

        self.assertEqual(
            parsed["state_summary"],
            {
                "title": "🌍 Global State Summary",
                "bullets": [
                    "**Architecture:** dual-brain orchestration.",
                    "**Target Upgrade:** sub-step hierarchy.",
                ],
            },
        )
        # The heading holds no tasks, so it must not appear as an empty section.
        self.assertEqual([s["title"] for s in parsed["sections"]], ["1. Build"])
        self.assertNotIn("Global State Summary", [s["title"] for s in parsed["steps"]])

    def test_state_summary_is_absent_when_the_plan_has_no_header(self):
        self.assertIsNone(ft.parse_markdown_to_plan_dict(PLAN_MD, "PLAN.md")["state_summary"])

    def test_global_state_summary_survives_compile_then_reparse(self):
        parsed = ft.parse_markdown_to_plan_dict(NESTED_MD, "PLAN.md")
        compiled = ft.compile_plan_json_to_markdown(parsed)

        self.assertIn("## 🌍 Global State Summary", compiled)
        self.assertIn("- **Architecture:** dual-brain orchestration.", compiled)

        reparsed = ft.parse_markdown_to_plan_dict(compiled, "PLAN.md")
        self.assertEqual(reparsed["state_summary"], parsed["state_summary"])
        self.assertEqual(ft.compile_plan_json_to_markdown(reparsed), compiled)

    def test_ui_tag_is_only_a_tag_when_it_stands_alone(self):
        # A bare substring test stripped the tag out of the middle of a title that merely
        # mentioned it, leaving an orphaned empty code span behind.
        md = "# P\n\n## 1. S\n- [ ] Wire Laya zero-token `[UI]` task tagging into the parser\n- [ ] [FE] Dashboard view\n"
        steps = ft.parse_markdown_to_plan_dict(md, "P.md")["steps"]

        self.assertIsNone(steps[0]["tag"])
        self.assertEqual(steps[0]["title"], "Wire Laya zero-token `[UI]` task tagging into the parser")
        self.assertEqual(steps[1]["tag"], UI_TAG)
        self.assertEqual(steps[1]["title"], "Dashboard view")


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

        # System 2 is enabled by default once a provider is configured, which would make
        # these structural tests depend on the developer's shell and reach the network.
        # Pin the offline path for every workspace test; the live path has its own tests.
        env_patcher = mock.patch.dict(os.environ, {"DEEPAGENTS_SYSTEM2": "0"})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

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

    def test_load_recovers_when_plan_json_is_unreadable(self):
        # An unreadable plan.json (interrupted write, transient file lock, cloud-sync
        # placeholder) must not be reported to the UI as "no tasks" while the markdown
        # plan is still intact: the markdown is used to rebuild the machine state.
        self.write("PLAN.md", PLAN_MD)
        self.write("plan.json", "{ this is not valid json")
        state = ft.load_plan_state()
        self.assertEqual(len(state["steps"]), 5)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "plan.json")))

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

    def test_files_survive_rehydration_from_markdown(self):
        """Both rehydration sides must now agree on a task's deliverables.

        compile_plan_json_to_markdown() emits a `Files: ...` detail line for every
        task that has deliverables, and the parser reads it back. Whichever
        representation the mtime check prefers, `files` is therefore stable;
        previously the markdown side silently dropped every entry, so audit and
        rollback results depended on which write won a sub-millisecond race.
        """
        self.save_state({
            "title": "Round Trip",
            "sections": [{"id": "sec-1", "title": "1. S", "tasks": [
                {"id": "task-1", "section": "1. S", "title": "Alpha", "status": "completed",
                 "details": ["note one"], "files": ["alpha.py"]},
                {"id": "task-2", "section": "1. S", "title": "Beta", "status": "pending",
                 "details": [], "files": ["beta.py"]},
            ]}],
        })

        # 1. plan.json authoritative -> deliverables intact.
        from_json = self.read_state()
        self.assertEqual([t["files"] for t in from_json["steps"]], [["alpha.py"], ["beta.py"]])

        # 2. markdown strictly newer -> same deliverables, rehydrated from markdown.
        md_path = os.path.join(self.tmp, "PLAN.md")
        bumped = os.path.getmtime(md_path) + 5
        os.utime(md_path, (bumped, bumped))
        from_md = ft.load_plan_state()
        self.assertEqual([t["files"] for t in from_md["steps"]], [["alpha.py"], ["beta.py"]])
        self.assertEqual([t["status"] for t in from_md["steps"]], ["completed", "pending"])
        self.assertEqual([t["details"] for t in from_md["steps"]], [["note one"], []])

    def test_list_plan_files(self):
        self.write("PLAN.md", PLAN_MD)
        self.write("ROADMAP.md", PLAN_MD)
        self.write("notes.txt", "x")
        self.assertEqual(ft.list_plan_files(), ["PLAN.md", "ROADMAP.md"])

    def test_list_plan_files_skips_non_plan_documents(self):
        # A tracker/README in the workspace root is not a roadmap. Offering one as a
        # switchable plan made the Dual-Sync engine compile a non-plan document, which
        # surfaced as a blank plan tree with every action locked.
        self.write("PLAN.md", PLAN_MD)
        self.write("PROGRESS.md", "# Project Plan & Execution Tracker\n")
        self.write("README.md", "# readme\n")
        self.write("CHANGELOG.md", "# changelog\n")
        self.assertEqual(ft.list_plan_files(), ["PLAN.md"])


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
                        "status": "completed", "details": [], "files": ["kept.py"],
                    },
                    {
                        "id": "task-2", "section": "1. Setup", "title": "Missing its file",
                        "status": "completed", "details": [], "files": ["gone.py"],
                    },
                    {
                        "id": "task-3", "section": "1. Setup", "title": "Pending but built",
                        "status": "pending", "details": [], "files": ["built.py"],
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
                 "details": [], "files": ["only.py"]},
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
                 "details": [], "files": ["restored.py", "flaky.py"]},
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


class TaskDiffTests(WorkspaceTestCase):
    """The read-only diff surface behind the UI's tracked-edits pane."""

    def _seed_diffable_task(self):
        ft.write_file.invoke({"filename": "existing.py", "content": "v1\n"})
        ft.backup_file_for_task("task-9", "existing.py")
        ft.write_file.invoke({"filename": "existing.py", "content": "v2\n"})

        ft.backup_file_for_task("task-9", "brand_new.py")
        ft.write_file.invoke({"filename": "brand_new.py", "content": "b\n"})

    def test_task_diff_reports_modified_and_created(self):
        self._seed_diffable_task()
        res = ft.task_diff("task-9")

        self.assertTrue(res["success"])
        self.assertTrue(res["found"])
        self.assertEqual(res["task_id"], "task-9")
        # One entry per recorded file, ordered by name rather than by write order.
        self.assertEqual([f["filename"] for f in res["files"]], ["brand_new.py", "existing.py"])

        created, modified = res["files"]
        self.assertEqual(created["action"], "created")
        self.assertIsNone(created["before"])
        self.assertEqual(created["after"], "b\n")
        self.assertTrue(created["available"])
        self.assertEqual((created["added"], created["removed"]), (1, 0))

        self.assertEqual(modified["action"], "modified")
        self.assertEqual(modified["before"], "v1\n")
        self.assertEqual(modified["after"], "v2\n")
        self.assertTrue(modified["available"])
        self.assertEqual((modified["added"], modified["removed"]), (1, 1))

        self.assertIn("-v1", modified["diff"])
        self.assertIn("+v2", modified["diff"])
        self.assertEqual(res["totals"], {"files": 2, "added": 2, "removed": 1})

    def test_task_diff_unknown_task_is_empty_not_an_error(self):
        # analyze / recommend / update_plan never snapshot files, so an absent backup
        # directory is an ordinary answer, not a failure the pane should shout about.
        res = ft.task_diff("task-unknown")
        self.assertTrue(res["success"])
        self.assertFalse(res["found"])
        self.assertEqual(res["files"], [])
        self.assertEqual(res["totals"], {"files": 0, "added": 0, "removed": 0})

    def test_task_diff_blank_task_id_is_rejected(self):
        res = ft.task_diff("  ")
        self.assertFalse(res["success"])
        self.assertEqual(res["files"], [])

    def test_task_diff_does_not_modify_the_workspace(self):
        self._seed_diffable_task()
        ft.task_diff("task-9")
        self.assertEqual(self.read("existing.py"), "v2\n")
        self.assertEqual(self.read("brand_new.py"), "b\n")
        self.assertEqual(
            self.read(os.path.join(".deepagents_backups", "task-9", "existing.py")),
            "v1\n",
        )


class PreviewSourceTests(WorkspaceTestCase):
    """The read-only text surface behind the UI's live preview.

    The preview renders through the bridge rather than pointing an iframe at a file://
    URL, because WebView2 does not reliably finish a local document load. These pin what
    the bridge hands over instead.
    """

    def test_reads_the_interface_file(self):
        ft.write_file.invoke({"filename": ft.PREVIEW_FILENAME, "content": "<h1>hi</h1>\n"})
        res = ft.read_preview_source()

        self.assertTrue(res["success"])
        self.assertTrue(res["found"])
        self.assertEqual(res["filename"], ft.PREVIEW_FILENAME)
        self.assertEqual(res["content"], "<h1>hi</h1>\n")
        self.assertFalse(res["truncated"])
        self.assertEqual(res["error"], "")

    def test_missing_file_is_an_ordinary_answer(self):
        # The workspace legitimately holds no interface until a task builds one, so an
        # absent file is data the preview explains rather than an error it shouts about.
        res = ft.read_preview_source()

        self.assertTrue(res["success"])
        self.assertFalse(res["found"])
        self.assertEqual(res["content"], "")
        self.assertEqual(res["error"], "")

    def test_a_path_cannot_reach_outside_the_workspace(self):
        outside = tempfile.mkdtemp(prefix="deepagents_previewoutside_")
        try:
            with open(os.path.join(outside, "secret.txt"), "w", encoding="utf-8") as fh:
                fh.write("not for the preview")

            res = ft.read_preview_source(os.path.join(outside, "secret.txt"))

            # Only the basename survives, so the lookup stays inside the workspace and
            # finds nothing rather than serving the outside file.
            self.assertFalse(res["found"])
            self.assertEqual(res["filename"], "secret.txt")
            self.assertTrue(os.path.isfile(os.path.join(outside, "secret.txt")))
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_blank_filename_is_rejected(self):
        res = ft.read_preview_source("   ")

        self.assertFalse(res["success"])
        self.assertFalse(res["found"])

    def test_an_oversized_file_is_truncated_not_refused(self):
        ft.write_file.invoke({"filename": ft.PREVIEW_FILENAME, "content": "0123456789ABCDEF"})
        with mock.patch("tools.file_ops.MAX_PREVIEW_CHARS", 10):
            res = ft.read_preview_source()

        self.assertTrue(res["found"])
        self.assertTrue(res["truncated"])
        self.assertEqual(res["content"], "0123456789")


class EnvironmentVariableTests(WorkspaceTestCase):
    """The masked environment-variable surface behind the sidebar's env panel.

    The panel names which variables the app loaded; it never shows a value. These pin
    that the masking happens on the Python side, so the payload the bridge hands the
    webview carries no secret at all -- not merely a secret the UI chooses not to draw.
    """

    def _write_env(self, text):
        path = os.path.join(self.tmp, ".env")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_reports_names_and_lengths_but_never_values(self):
        path = self._write_env(
            "OPENAI_API_KEY=sk-secret-value\nOPENAI_BASE_URL=https://api.example\n"
        )
        with mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "sk-secret-value",
            "OPENAI_BASE_URL": "https://api.example",
        }):
            res = ft.read_environment_variables(path)

        self.assertTrue(res["success"])
        self.assertTrue(res["found"])
        self.assertEqual(res["filename"], ".env")
        self.assertEqual(res["variables"], [
            {"name": "OPENAI_API_KEY", "set": True, "length": 15},
            {"name": "OPENAI_BASE_URL", "set": True, "length": 19},
        ])
        # The whole payload, at any depth: a value here would be the secret in the webview.
        self.assertNotIn("sk-secret-value", json.dumps(res))

    def test_a_name_the_environment_does_not_supply_is_reported_as_unset(self):
        path = self._write_env("DEEPAGENTS_TEST_UNSET_NAME=v\n")

        res = ft.read_environment_variables(path)

        self.assertEqual(res["variables"],
                         [{"name": "DEEPAGENTS_TEST_UNSET_NAME", "set": False, "length": 0}])

    def test_a_missing_file_is_an_ordinary_answer(self):
        res = ft.read_environment_variables(os.path.join(self.tmp, "absent.env"))

        self.assertTrue(res["success"])
        self.assertFalse(res["found"])
        self.assertEqual(res["variables"], [])
        self.assertEqual(res["error"], "")

    def test_parses_comments_export_lines_and_duplicate_names(self):
        path = self._write_env(
            "# a comment\n\nOPENAI_API_KEY=one\nexport OPENAI_BASE_URL=two\nOPENAI_API_KEY=three\n"
        )
        with mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "five5",
            "OPENAI_BASE_URL": "seven77",
        }):
            res = ft.read_environment_variables(path)

        # Last line wins is dotenv's own rule, but a name must still appear only once.
        self.assertEqual([v["name"] for v in res["variables"]],
                         ["OPENAI_API_KEY", "OPENAI_BASE_URL"])
        self.assertEqual([v["length"] for v in res["variables"]], [5, 7])

    def test_the_name_list_is_capped(self):
        lines = "\n".join(
            f"DEEPAGENTS_TEST_KEY_{i}=v" for i in range(ft.MAX_ENVIRONMENT_VARIABLES + 10)
        )
        path = self._write_env(lines + "\n")

        res = ft.read_environment_variables(path)

        self.assertEqual(len(res["variables"]), ft.MAX_ENVIRONMENT_VARIABLES)
        self.assertEqual(res["variables"][0]["name"], "DEEPAGENTS_TEST_KEY_0")


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
        self.assertEqual(architect["model"], "openai:policy/coder-deep-test")
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

    def test_tool_events_carry_the_full_wire_payload(self):
        """Locks the exact tool_call/tool_result payloads and their key order.

        Payloads are JSON-serialised in insertion order and the UI animates the
        pair as a unit, so a renamed or reordered key breaks the frontend even
        when the event sequence itself is unchanged.
        """
        import registry as registry_module

        registry_module.registry.create_new_plan_file("PLAN.md", "Demo roadmap")
        events = self._collect("recommend", "[ACTION: RECOMMEND_NEXT_STEPS]")

        call = [e for e in events if e["type"] == "tool_call"][0]
        result = [e for e in events if e["type"] == "tool_result"][0]

        self.assertEqual(list(call), ["type", "agent", "tool", "args", "description"])
        self.assertEqual(
            call,
            {
                "type": "tool_call",
                "agent": "software-architect",
                "tool": "load_plan_state",
                "args": {"plan_file": "PLAN.md"},
                "description": "Inspecting current plan progress and task milestones",
            },
        )
        self.assertEqual(list(result), ["type", "agent", "tool", "result"])
        self.assertEqual(
            result,
            {
                "type": "tool_result",
                "agent": "software-architect",
                "tool": "load_plan_state",
                "result": "Plan loaded: 8 total tasks.",
            },
        )

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


class CoderDelegationEventTests(WorkspaceTestCase):
    """Locks the event streams for the branches that actually spawn a Coder.

    The admin-bypass actions were covered first because they touch no subprocesses
    and are deterministic. These three paths -- fix_bug, next_step and the
    coder-delegation half of custom -- were not, which is why they are pinned here
    before their ~540 lines move out of ``registry.py``: without a frozen event
    stream, relocating them puts the zero-breakage guarantee at risk.

    ``time.sleep`` only paces the stream for a human reader; it emits nothing and
    is not part of the contract, so it is stubbed out to keep the suite fast.
    """

    def setUp(self):
        super().setUp()
        patcher = mock.patch("time.sleep", lambda *a, **k: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _collect(self, action_type, message, params=None):
        import registry as registry_module

        # The workflow reads plan state at the top; pin it so the markdown
        # rehydration branch cannot flip mid-suite.
        self.pin_plan_json_authoritative()
        events = []
        registry_module.registry.run_agent_workflow(
            message, events.append, action_type=action_type, action_params=params or {}
        )
        return events

    @staticmethod
    def _types(events):
        return [e["type"] for e in events]

    def _seed_scaffold(self):
        """Create the standard 8-task roadmap used by most of these tests."""
        import registry as registry_module

        registry_module.registry.create_new_plan_file("PLAN.md", "Demo roadmap")

    def test_next_step_event_sequence(self):
        self._seed_scaffold()
        events = self._collect("next_step", "[ACTION: EXECUTE_NEXT_STEP]")

        self.assertEqual(self._types(events), [
            "workflow_started", "architect_spawn",
            "log", "log", "log",
            "delegation", "coder_spawn",
            "log", "log",
            "tool_call", "tool_result",
            "tool_call", "tool_result",
            "tool_call", "tool_result",
            "plan_updated",
            "coder_summary",
            "log",
            "tool_call", "tool_result",
            "architect_summary",
            "workflow_complete",
        ])
        self.assertEqual(events[0]["action_type"], "next_step")

        # The scaffold's first pending task is plain, non-core and non-UI, so it
        # goes to the standard coder rather than coder-deep.
        delegation = events[5]
        self.assertEqual(delegation["target_agent"], "coder-standard")
        self.assertEqual(delegation["target_name"], "Junior Developer")
        self.assertEqual(delegation["task"], "Project scaffolding and runtime dependencies")
        self.assertEqual(events[6]["model"], "openai:policy/coder-standard-test")

        # Deliverables land on disk and are recorded against the task.
        self.assertIn("class SolutionEngine", self.read("main.py"))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "test_main.py")))

        plan_event = [e for e in events if e["type"] == "plan_updated"][0]
        self.assertEqual(list(plan_event), ["type", "filename", "content", "tree", "plan_json"])
        task = plan_event["tree"][1]
        self.assertEqual(task["id"], "task-2")
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["files"], ["main.py", "test_main.py"])
        self.assertIn("Deliverables: `main.py`, `test_main.py`", task["details"])
        self.assertIn("Files: `main.py`, `test_main.py`", plan_event["content"])

        summary = events[-2]["summary"]
        self.assertEqual(summary["title"], "Lead Architect Task Approval & Handoff")
        self.assertEqual(summary["files"], ["PLAN.md", "main.py", "test_main.py"])
        self.assertEqual(
            summary["proposals"][0],
            "Execute next step: 'Build core domain models and application logic'",
        )
        self.assertEqual(
            events[-1]["message"],
            "Task 'Project scaffolding and runtime dependencies' completed and verified.",
        )

        # A rollback snapshot is recorded before the deliverables are written.
        meta = json.loads(self.read(os.path.join(".deepagents_backups", "task-2", "_meta.json")))
        self.assertEqual(meta["main.py"]["action"], "created")
        self.assertEqual(meta["test_main.py"]["action"], "created")

    def test_next_step_targets_the_deep_coder_for_core_tasks(self):
        self._seed_scaffold()
        events = self._collect(
            "next_step", "[ACTION: EXECUTE_NEXT_STEP]", {"targetTaskId": "task-3"}
        )

        delegation = [e for e in events if e["type"] == "delegation"][0]
        self.assertEqual(delegation["target_agent"], "coder-deep")
        self.assertEqual(delegation["target_name"], "Senior Backend Coder")
        self.assertEqual(delegation["task"], "Build core domain models and application logic")

        spawn = [e for e in events if e["type"] == "coder_spawn"][0]
        self.assertEqual(spawn["model"], "openai:policy/coder-deep-test")

        # An explicit target completes that task, not the first pending one.
        plan_event = [e for e in events if e["type"] == "plan_updated"][0]
        completed = [t["id"] for t in plan_event["tree"] if t["status"] == "completed"]
        self.assertEqual(completed, ["task-1", "task-3"])

    def test_next_step_ui_task_emits_the_vision_directive(self):
        self.save_state({
            "title": "UI Demo",
            "sections": [{"id": "sec-1", "title": "1. S", "tasks": [
                {"id": "task-1", "section": "1. S", "title": "Dashboard view", "status": "pending",
                 "tag": "FE", "details": [], "files": []},
            ]}],
        })
        events = self._collect("next_step", "[ACTION: EXECUTE_NEXT_STEP]")

        logs = [e["text"] for e in events if e["type"] == "log"]
        self.assertIn("> \U0001f3a8 [MULTIMODAL UI TASK DETECTED] Tag: [FE]\n", logs)
        self.assertIn("> Reference Image: Wireframe & dark glassmorphic styling guide\n", logs)

        # UI tasks are routed to the deep coder and get the HTML deliverable.
        self.assertEqual([e for e in events if e["type"] == "delegation"][0]["target_agent"], "coder-deep")
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "ui_view.html")))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "test_ui_view.py")))

        plan_event = [e for e in events if e["type"] == "plan_updated"][0]
        self.assertEqual(plan_event["tree"][0]["files"], ["ui_view.html", "test_ui_view.py"])

        summary = events[-2]["summary"]
        self.assertEqual(summary["proposals"][0], "All roadmap tasks finished! Run final test audit.")
        self.assertEqual(events[-1]["message"], "Task 'Dashboard view' completed and verified.")

    def test_next_step_when_no_pending_tasks_remain(self):
        self.save_state({
            "title": "Done",
            "sections": [{"id": "sec-1", "title": "1. S", "tasks": [
                {"id": "task-1", "section": "1. S", "title": "Only", "status": "completed",
                 "details": [], "files": ["only.py"]},
            ]}],
        })
        events = self._collect("next_step", "[ACTION: EXECUTE_NEXT_STEP]")

        self.assertEqual(self._types(events), [
            "workflow_started", "architect_spawn", "log", "architect_summary", "workflow_complete",
        ])
        summary = events[-2]["summary"]
        self.assertEqual(summary["title"], "All Plan Tasks Completed")
        self.assertEqual(summary["status"], "Finished")
        self.assertEqual(summary["files"], ["PLAN.md"])
        self.assertEqual(events[-1]["message"], "All steps complete.")

    def _seed_buggy_main(self):
        self._seed_scaffold()
        ft.write_file.invoke({"filename": "main.py", "content": (
            "from typing import Dict, Any\n\n"
            "class SolutionEngine:\n"
            "    def run(self) -> Dict[str, Any]:\n"
            "        return {'status': 'success'}\n"
        )})
        ft.write_file.invoke({"filename": "test_main.py", "content": "def test_ok():\n    assert True\n"})

    def test_fix_bug_event_sequence(self):
        self._seed_buggy_main()
        events = self._collect(
            "fix_bug", "[ACTION: FIX_BUG] something broke",
            {"bugDescription": "IndexError in run()"},
        )

        self.assertEqual(self._types(events), [
            "workflow_started", "architect_spawn",
            "log", "log",
            "tool_call", "tool_result",
            "tool_call", "tool_result",
            "log", "log", "log", "log", "log", "log",
            "delegation", "coder_spawn",
            "log", "log",
            "tool_call", "tool_result",
            "log",
            "tool_call", "tool_result",
            "coder_summary",
            "log",
            "tool_call", "tool_result",
            "architect_summary",
            "workflow_complete",
        ])

        self.assertEqual(
            [e for e in events if e["type"] == "tool_result"][0]["result"],
            "Found code files: main.py, test_main.py",
        )
        delegation = [e for e in events if e["type"] == "delegation"][0]
        self.assertEqual(delegation["target_agent"], "coder-deep")
        self.assertEqual(delegation["task"], "Fix bug in main.py: IndexError in run()")

        # The surgical patch inserts a guard comment and rewrites the regression test.
        patched = self.read("main.py")
        self.assertIn("# Bugfix: Input validation & error boundary", patched)
        self.assertIn("def run(self) -> Dict[str, Any]:", patched)
        self.assertEqual(self.read("test_main.py"), templates.BUGFIX_REGRESSION_TEST)
        self.assertTrue(
            os.path.isfile(os.path.join(self.tmp, ".deepagents_backups", "bugfix", "main.py"))
        )

        coder_summary = [e for e in events if e["type"] == "coder_summary"][0]["summary"]
        self.assertEqual(coder_summary["title"], "Surgical Bug Patch: main.py")
        self.assertEqual(coder_summary["files"], ["main.py", "test_main.py"])
        self.assertEqual(
            coder_summary["deliverables"][0],
            "Applied surgical patch addressing: IndexError in run().",
        )

        summary = events[-2]["summary"]
        self.assertEqual(summary["title"], "Lead Architect Bug Fix Approval")
        self.assertEqual(summary["files"], ["main.py", "test_main.py"])
        self.assertEqual(
            events[-1]["message"], "Bug surgically diagnosed, patched, and verified."
        )

    def test_fix_bug_falls_back_to_the_message_and_strips_action_tags(self):
        self._seed_buggy_main()
        events = self._collect("fix_bug", "[ACTION: FIX_BUG] something broke")

        delegation = [e for e in events if e["type"] == "delegation"][0]
        self.assertEqual(delegation["task"], "Fix bug in main.py: something broke")
        coder_summary = [e for e in events if e["type"] == "coder_summary"][0]["summary"]
        self.assertEqual(
            coder_summary["deliverables"][0],
            "Applied surgical patch addressing: something broke.",
        )

    def test_fix_bug_refuses_an_empty_report_without_spawning_a_coder(self):
        """The client blocks an empty report; the console path has to block it too.

        A directive typed into the console never passes through the webview's
        validation, so the rule is mirrored on this side of the bridge.
        """
        self._seed_buggy_main()
        events = self._collect("fix_bug", "[ACTION: FIX_BUG]")

        self.assertEqual(self._types(events), [
            "workflow_started", "architect_spawn",
            "log", "log", "log",
            "architect_summary", "workflow_complete",
        ])
        summary = events[-2]["summary"]
        self.assertEqual(summary["title"], "Lead Architect Bug Report Assessment")
        self.assertEqual(summary["status"], "Input Required")
        self.assertEqual(summary["files"], [])
        self.assertEqual(
            events[-1]["message"],
            "Bug report needs a description before diagnosis can start.",
        )

    def test_fix_bug_accepts_an_attachment_with_no_description(self):
        """Either half of the client rule is enough, so the gate is not stricter here."""
        self._seed_buggy_main()
        events = self._collect(
            "fix_bug", "[ACTION: FIX_BUG]", {"bugAttachment": "traceback.txt"}
        )

        self.assertIn("delegation", self._types(events))
        self.assertEqual(events[-1]["message"], "Bug surgically diagnosed, patched, and verified.")

    def test_custom_directive_delegates_to_the_deep_coder(self):
        self._seed_scaffold()
        events = self._collect("custom", "add a login endpoint")

        self.assertEqual(self._types(events), [
            "workflow_started", "architect_spawn", "log", "log", "log",
            "delegation", "coder_spawn", "log", "coder_summary", "log",
            "architect_summary", "workflow_complete",
        ])
        delegation = events[5]
        self.assertEqual(delegation["target_agent"], "coder-deep")
        self.assertEqual(delegation["task"], "add a login endpoint")

        self.assertIn("# Custom Solution: add a login endpoint", self.read("main.py"))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "test_main.py")))

        # Only the primary deliverable is snapshotted on the custom path.
        meta = json.loads(self.read(os.path.join(".deepagents_backups", "custom", "_meta.json")))
        self.assertEqual(list(meta), ["main.py"])
        self.assertEqual(meta["main.py"]["action"], "created")

        self.assertEqual(events[8]["summary"]["title"], "Custom Implementation: add a login endpoint")
        self.assertEqual(events[-1]["message"], "Custom task completed and verified.")

    def test_custom_directive_bypasses_the_coder_for_analytical_prompts(self):
        self._seed_scaffold()
        events = self._collect("custom", "explain the architecture")

        self.assertEqual(self._types(events), [
            "workflow_started", "architect_spawn", "log", "log", "log",
            "tool_call", "tool_result", "log", "log", "log", "log",
            "architect_summary", "workflow_complete",
        ])
        # The Gatekeeper resolves analytical prompts without spawning a Coder.
        self.assertNotIn("delegation", self._types(events))
        summary = events[-2]["summary"]
        self.assertEqual(summary["title"], "Lead Architect Directive Assessment")
        self.assertEqual(summary["status"], "Handled Directly")
        self.assertEqual(events[-1]["message"], "Custom directive executed directly by Architect.")

    def test_gatekeeper_delegates_an_imperative_request_that_sounds_administrative(self):
        """An action verb now beats an administrative-sounding word.

        The gate this replaced tested nine bare substrings, so "status" on its own
        routed "fix the status endpoint" to the Architect and no code was written.
        """
        self._seed_scaffold()
        events = self._collect("custom", "fix the status endpoint")

        self.assertIn("delegation", self._types(events))
        delegation = [e for e in events if e["type"] == "delegation"][0]
        self.assertEqual(delegation["target_agent"], "coder-deep")

    def test_gatekeeper_answers_a_question_the_old_gate_would_have_delegated(self):
        """A question is administrative on its shape alone, with no keyword needed."""
        self._seed_scaffold()
        events = self._collect("custom", "what does main.py do?")

        self.assertNotIn("delegation", self._types(events))
        self.assertEqual(
            events[-1]["message"], "Custom directive executed directly by Architect."
        )

    def test_the_gatekeeper_log_carries_the_evidence_for_its_decision(self):
        """The routing reason reaches the console, so a surprising route is explainable."""
        self._seed_scaffold()
        events = self._collect("custom", "add a login endpoint")

        evidence_lines = [
            e["text"] for e in events
            if e["type"] == "log" and "evidence:" in e.get("text", "")
        ]
        self.assertEqual(len(evidence_lines), 1)
        self.assertIn("opens with the code action 'add'", evidence_lines[0])

    def test_coder_event_payloads_carry_the_full_wire_shape(self):
        """Locks key order on the payloads the coder branches emit.

        Payloads are JSON-serialised in insertion order, so a reordered key breaks
        the frontend even when the event sequence itself is unchanged.
        """
        self._seed_scaffold()
        events = self._collect("next_step", "[ACTION: EXECUTE_NEXT_STEP]")

        first = {}
        for e in events:
            first.setdefault(e["type"], e)

        self.assertEqual(list(first["workflow_started"]), ["type", "message", "plan_file", "action_type"])
        self.assertEqual(list(first["architect_spawn"]), ["type", "agent", "name", "role", "model"])
        self.assertEqual(list(first["log"]), ["type", "agent", "log_type", "text"])
        self.assertEqual(
            list(first["delegation"]),
            ["type", "from_agent", "target_agent", "target_name", "task"],
        )
        self.assertEqual(list(first["coder_spawn"]), ["type", "agent", "name", "role", "model"])
        self.assertEqual(list(first["coder_summary"]), ["type", "agent", "summary"])
        self.assertEqual(list(first["plan_updated"]), ["type", "filename", "content", "tree", "plan_json"])
        self.assertEqual(list(first["architect_summary"]), ["type", "agent", "summary"])
        self.assertEqual(list(first["workflow_complete"]), ["type", "status", "message"])


class WorkflowTemplateTests(unittest.TestCase):
    """The coder delegation path writes canned deliverables; these pin what it writes.

    Byte-for-byte equality with the pre-refactor literals was verified once during
    the extraction; these assertions keep the shapes and the parameterisation honest.

    The selector reads the task's *tag* for the UI case and no longer re-inspects the
    title. The parser already turns UI wording into the tag, so a second inference here
    would be a second source of truth -- one that could pick the HTML view while the plan
    tree draws no pill. The wording rules themselves are pinned in ``test_laya.py``.
    """

    def test_a_ui_tag_selects_the_html_view(self):
        for title in ["Dashboard view", "Build frontend", "UI polish"]:
            deliverable = templates.select(title, UI_TAG)
            self.assertEqual(
                (deliverable.filename, deliverable.test_filename),
                ("ui_view.html", "test_ui_view.py"),
            )
            self.assertTrue(deliverable.code.startswith("<!DOCTYPE html>"))
            self.assertTrue(deliverable.code.endswith("</html>"))

    def test_the_ui_tag_beats_the_weather_keyword(self):
        self.assertEqual(templates.select("weather api service", UI_TAG).filename, "ui_view.html")
        self.assertEqual(templates.select("weather api service", None).filename, "weather_api.py")

    def test_an_untagged_title_is_not_re_inspected_for_ui_words(self):
        # The selector keys off the tag alone now. A title whose wording reads as UI is
        # the parser's business, and it has already stamped the tag by the time a
        # deliverable is selected; re-deciding here is what let the two disagree.
        self.assertEqual(templates.select("Build a weather API", None).filename, "weather_api.py")
        self.assertEqual(templates.select("Code review", None).filename, "main.py")
        self.assertEqual(templates.select("Dashboard views", None).filename, "main.py")
        self.assertEqual(templates.select("Public interfaces", None).filename, "main.py")

    def test_weather_task_selects_the_fastapi_service(self):
        deliverable = templates.select("weather forecast service", None)
        self.assertEqual(
            (deliverable.filename, deliverable.test_filename),
            ("weather_api.py", "test_weather_api.py"),
        )
        self.assertIn('@app.get("/weather/{city}")', deliverable.code)
        self.assertIn("client = TestClient(app)", deliverable.test_code)

    def test_unmatched_task_falls_back_to_the_generic_engine(self):
        deliverable = templates.select("Wire up telemetry", None)
        self.assertEqual((deliverable.filename, deliverable.test_filename), ("main.py", "test_main.py"))
        self.assertTrue(deliverable.code.startswith("# Generated module for task: Wire up telemetry\n"))
        self.assertIn('"task": "Wire up telemetry"', deliverable.code)
        self.assertEqual(deliverable.test_code, templates.SOLUTION_ENGINE_TEST)

    def test_custom_directive_echoes_the_message(self):
        message = "x" * 100
        deliverable = templates.custom_engine(message)
        self.assertEqual((deliverable.filename, deliverable.test_filename), ("main.py", "test_main.py"))
        # The header is truncated to 60 characters; the returned payload is not.
        self.assertEqual(deliverable.code.splitlines()[0], f"# Custom Solution: {message[:60]}")
        self.assertIn(f'"prompt": "{message}"', deliverable.code)
        self.assertEqual(deliverable.test_code, templates.SOLUTION_ENGINE_TEST)

    def test_generated_modules_and_tests_are_valid_python(self):
        for deliverable in (templates.default_engine("A title"), templates.custom_engine("A message")):
            compile(deliverable.code, deliverable.filename, "exec")
            compile(deliverable.test_code, deliverable.test_filename, "exec")

    def test_bugfix_regression_test_targets_the_solution_engine(self):
        compile(templates.BUGFIX_REGRESSION_TEST, "test_main.py", "exec")
        self.assertIn("def test_bug_regression():", templates.BUGFIX_REGRESSION_TEST)
        self.assertIn("from main import SolutionEngine", templates.BUGFIX_REGRESSION_TEST)


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
        "BACKUP_SUBDIR", "read_preview_source", "PREVIEW_FILENAME",
        "MAX_PREVIEW_CHARS", "read_environment_variables", "ENV_FILENAME",
        "MAX_ENVIRONMENT_VARIABLES",
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


class WorkspaceLocationTests(unittest.TestCase):
    """The default workspace must not depend on the process working directory.

    The desktop app is launched from shortcuts, IDE run configurations and plain
    ``python app.py`` invocations, and the workspace is resolved at import time.
    A cwd-relative default silently resolves to a different, freshly-created empty
    directory for some of those launches, which reaches the UI as a blank plan tree
    with every action control locked. Each case runs in a fresh interpreter because
    that is exactly where the resolution happens.
    """

    _RESOLVE_SCRIPT = (
        "import sys;"
        "sys.path.insert(0, sys.argv[1]);"
        "from tools.workspace import get_project_dir;"
        "print(get_project_dir())"
    )

    @property
    def project_root(self):
        return os.path.dirname(os.path.dirname(os.path.abspath(ft.__file__)))

    def _resolve_from(self, cwd):
        result = subprocess.run(
            [sys.executable, "-c", self._RESOLVE_SCRIPT, self.project_root],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def test_default_workspace_is_anchored_to_the_project_root(self):
        expected = os.path.join(self.project_root, "my_project_workspace")
        for cwd in (self.project_root, os.path.join(self.project_root, "ui"), os.path.dirname(self.project_root)):
            if not os.path.isdir(cwd):
                continue
            self.assertEqual(self._resolve_from(cwd), expected, f"cwd={cwd}")

    def test_foreign_cwd_does_not_create_a_second_workspace(self):
        foreign = tempfile.mkdtemp(prefix="deepagents_foreigncwd_")
        try:
            self._resolve_from(foreign)
            self.assertFalse(os.path.exists(os.path.join(foreign, "my_project_workspace")))
        finally:
            shutil.rmtree(foreign, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
