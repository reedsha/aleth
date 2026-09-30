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
from tools import workspace as workspace_module
from tools.shell_result import command_exit_code, command_failed
from tools.task_tags import UI_TAG
from tools.workspace import get_plan_dir, set_plan_dir
from orchestration.workflow import templates
from storage.db import DB_FILENAME

# Commands now run in a Docker container. This suite runs on a machine with no Docker daemon,
# so the runtime is the in-repo double (``tests/fake_docker.py``): the argv, the child process
# and the output plumbing are real, and only the container itself is emulated.
FAKE_DOCKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_docker.py")
FAKE_DOCKER_ENV = {"ALETH_DOCKER_BIN": f"{sys.executable} {FAKE_DOCKER}"}

PLAN_MD = """# Project Plan: Demo

## 1. Setup
- [x] Scaffolding
  - Files Created/Modified: `setup.py`, `config.py`
- [ ] Pending item
- [-] Working item
- [?] not a real checkbox
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
        # A bracket token that is not one of the four marks (`x`, `-`, `!`, ` `) is not a
        # checkbox, so the raw item text (including its leading "[?] ") survives verbatim.
        self.assertEqual(steps[2]["details"], ["[?] not a real checkbox"])
        # ...and it does not create a task of its own.
        self.assertNotIn("[?] not a real checkbox", [s["title"] for s in steps])

    def test_failed_mark_is_a_real_status_and_round_trips(self):
        # `[!]` marks a task the Coder wrote but that did not pass verification. It has to be
        # a real mark and survive compile -> reparse, or a reload would turn a failed task
        # back into a pending one.
        steps = ft.parse_markdown_to_plan_dict(
            "# P\n\n## 1. S\n- [!] Broken thing\n- [ ] Untouched\n", "P.md"
        )["steps"]
        self.assertEqual([s["status"] for s in steps], ["failed", "pending"])
        self.assertEqual(steps[0]["title"], "Broken thing")

        plan = {"title": "P", "sections": [{"id": "sec-1", "title": "1. S", "tasks": [
            {"id": "task-1", "section": "1. S", "title": "Broken thing", "status": "failed"},
        ]}]}
        compiled = ft.compile_plan_json_to_markdown(plan)
        self.assertIn("- [!] Broken thing", compiled)
        self.assertEqual(
            ft.parse_markdown_to_plan_dict(compiled, "P.md")["steps"][0]["status"], "failed"
        )

    def test_metrics(self):
        parsed = ft.parse_markdown_to_plan_dict(PLAN_MD)
        self.assertEqual(
            parsed["metrics"],
            {
                "total_tasks": 5,
                "completed_tasks": 1,
                "in_progress_tasks": 1,
                "failed_tasks": 0,
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


def _read_if_present(path):
    """The file's bytes, or None when it is absent or unreadable right now.

    Best-effort on purpose. This repository is synced by OneDrive, which takes short
    exclusive handles on files in it, and a hiccup in a harness fixture must not fail
    whichever test happened to be starting. An unreadable file reads as changed, which
    makes the caller put the snapshot back -- the safe direction.
    """
    for attempt in range(5):
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except FileNotFoundError:
            return None
        except OSError:
            if attempt == 4:
                return None
            time.sleep(0.05)
    return None


def _restore_file(path, content):
    """Puts the developer's file back: retried, and reported rather than raised.

    A restore that cannot be made is not worth failing a test over -- the plan state is
    derived and rebuildable -- but it is worth saying out loud, so a persistently locked
    file is not mistaken for a clean run.
    """
    for attempt in range(10):
        try:
            with open(path, "wb") as fh:
                fh.write(content)
            return
        except OSError as error:
            if attempt == 9:
                print(f"[harness] could not restore {path}: {error}")
            else:
                time.sleep(0.05)


def _remove_file(path):
    """Deletes a file that appeared during a test, retried like the restore."""
    for attempt in range(10):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except OSError as error:
            if attempt == 9:
                print(f"[harness] could not remove {path}: {error}")
            else:
                time.sleep(0.05)


class WorkspaceTestCase(unittest.TestCase):
    """Base class: isolates every test in a throwaway workspace.

    The real plan directory is the developer's own repository, so the suite points both
    pointers at a temporary directory for the length of each test and puts them back
    afterwards. Putting them back is done by assigning the module globals rather than by
    calling the setters: the setters load the plan, which would create a state database in
    the developer's own directory on every single test.

    The real files are still snapshotted, and put back only if their bytes actually
    changed: that covers a test that escapes the sandbox, without writing anything in the
    ordinary case.
    """

    _SNAPSHOT_FILES = ("PLAN.md",)

    def setUp(self):
        self._orig_dir = ft.get_project_dir()
        self._orig_plan_dir = get_plan_dir()
        self._orig_plan = ft.get_active_plan_filename()
        self._snapshot = {}
        for name in self._SNAPSHOT_FILES:
            path = os.path.join(self._orig_plan_dir, name)
            self._snapshot[name] = _read_if_present(path)

        self.tmp = tempfile.mkdtemp(prefix="aleth_chartest_")
        # Point the plan at the throwaway directory *first*: the setters load the plan, and
        # a load while the pointer still named the developer's directory would create a
        # state database beside their roadmap.
        set_plan_dir(self.tmp)
        ft.set_project_dir(self.tmp)
        ft.set_active_plan_filename("PLAN.md")

        # System 2 is enabled by default once a provider is configured, which would make
        # these structural tests depend on the developer's shell and reach the network.
        # Pin the offline path for every workspace test; the live path has its own tests.
        #
        # The container runtime is pinned to the in-repo double for the same reason: a branch
        # that verifies with ``execute_restricted_command`` runs that command in a container,
        # and this suite must not need a Docker daemon to be deterministic.
        env_patcher = mock.patch.dict(
            os.environ, {"ALETH_SYSTEM2": "0", **FAKE_DOCKER_ENV}
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def tearDown(self):
        # Assigned directly, not through the setters: see the class docstring.
        workspace_module.PLAN_DIR = self._orig_plan_dir
        workspace_module.PROJECT_DIR = self._orig_dir
        workspace_module.ACTIVE_PLAN_FILE = self._orig_plan

        for name in self._SNAPSHOT_FILES:
            path = os.path.join(self._orig_plan_dir, name)
            original = self._snapshot.get(name)
            if original is None:
                # It did not exist before the test, so it has no business existing after.
                if os.path.exists(path):
                    _remove_file(path)
                continue
            if _read_if_present(path) == original:
                # Nothing wrote to it, so there is nothing to put back -- and no write
                # that a synced filesystem could refuse.
                continue
            _restore_file(path, original)
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
    # The state lives in a SQLite database in this throwaway directory, so the store is
    # authoritative and there is no representation race to pin: a save is committed and a
    # load reads exactly what was committed. These thin wrappers are kept so the tests
    # below read the same way they always have.

    def save_state(self, plan_dict):
        """save_plan_state, and return the canonicalised dictionary it stored."""
        return ft.save_plan_state(plan_dict)

    def read_state(self):
        """load_plan_state."""
        return ft.load_plan_state()


class FileOpTests(WorkspaceTestCase):
    """read_file / write_file / append_to_file / list_workspace_files."""

    def test_missing_file_message(self):
        # The engine reader answers "nothing to read" rather than writing an error string as
        # file content -- a caller can tell the difference.
        self.assertEqual(ft.read_source("nope.py"), "")

    def test_write_then_read_round_trip(self):
        result = ft.overwrite_source("pkg/mod.py", "x = 1\n")
        self.assertEqual(result, "Successfully wrote 6 characters to pkg/mod.py.")
        self.assertEqual(ft.read_source("pkg/mod.py"), "x = 1\n")

    def test_a_whole_file_rewrite_cannot_escape_the_workspace(self):
        """Containment is the engine writer's own guard, not a caller's convention.

        The model's refuse-overwrite chokehold lived in the deleted ``write_file`` tool. What
        replaces it for the engine's own writer is a resolved-path containment check, so a
        deliverable can never be published outside the workspace.
        """
        with self.assertRaises(Exception) as caught:
            ft.overwrite_source("../../escaped.py", "boom = True\n")
        self.assertIn("Path traversal denied", str(caught.exception))

    def test_a_file_is_refreshed_in_place(self):
        # The engine's writer is not the model's tool: re-running a task must be able to
        # refresh a deliverable that already exists.
        ft.overwrite_source("once.py", "first\n")
        ft.overwrite_source("once.py", "second\n")
        self.assertEqual(ft.read_source("once.py"), "second\n")

    def test_read_source_is_never_truncated(self):
        """A caller that writes the text back must get the whole file.

        The token-optimised middle-truncating view belonged to the model's ``read_file`` tool,
        which is an MCP tool now. The engine reader has no budget: an omitted middle would be
        silently deleted when the text is written back.
        """
        body = "A" * 7000 + "B" * 7000
        ft.overwrite_source("big.py", body)
        self.assertEqual(ft.read_source("big.py"), body)

    def test_list_workspace_files_prunes_internal_dirs(self):
        ft.overwrite_source("sub/a.py", "")
        ft.overwrite_source("__pycache__/b.pyc", "")
        ft.overwrite_source(".aleth_backups/task-1/c.py", "")
        ft.overwrite_source("node_modules/d.js", "")

        paths = [f["path"] for f in ft.list_workspace_files()]
        self.assertIn("sub/a.py", paths)
        self.assertNotIn("__pycache__/b.pyc", paths)
        self.assertNotIn(".aleth_backups/task-1/c.py", paths)
        self.assertNotIn("node_modules/d.js", paths)


class PlanStateTests(WorkspaceTestCase):
    """load_plan_state / save_plan_state / update_plan_task_status / sync_plan_on_disk."""

    def _save_demo(self):
        return self.save_state(ft.parse_markdown_to_plan_dict(PLAN_MD, "PLAN.md"))

    def test_save_writes_the_store_and_the_markdown_projection(self):
        saved = self._save_demo()

        # The store is the source of truth; the markdown is a projection written beside it.
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, DB_FILENAME)))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "PLAN.md")))
        # plan.json is gone for good.
        self.assertFalse(os.path.isfile(os.path.join(self.tmp, "plan.json")))
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

    def test_markdown_does_not_determine_state_once_the_plan_is_stored(self):
        # The one-time import reads an authored plan; after that the store is authoritative
        # and editing the markdown must not move state (the dual-sync behaviour is gone).
        self.write("PLAN.md", PLAN_MD)
        self.assertEqual(len(ft.load_plan_state()["steps"]), 5)

        self.write("PLAN.md", "# Project Plan: Changed\n\n## 1. Solo\n- [ ] Only task\n")
        os.utime(os.path.join(self.tmp, "PLAN.md"), (9e9, 9e9))
        state = ft.load_plan_state()
        self.assertEqual(len(state["steps"]), 5)
        self.assertEqual(state["title"], "Demo")

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

    def test_append_pending_task_lands_in_the_last_section(self):
        from tools.plan_state import append_pending_task

        self._save_demo()
        state = self.read_state()
        task = append_pending_task(state, "Survey the telemetry surface", note="Proposed by the architect.")
        saved = self.save_state(state)

        self.assertEqual(task["id"], "task-6")
        self.assertEqual(task["section"], "2. Build")
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["details"], ["Proposed by the architect."])
        self.assertEqual(task["files"], [])
        self.assertIsNone(task["tag"])
        # The task must survive the write: save_plan_state rebuilds `steps` from `sections`,
        # so a section that was not attached to the plan would discard the task silently.
        self.assertEqual(saved["steps"][-1]["title"], "Survey the telemetry surface")
        self.assertEqual(self.read_state()["steps"][-1]["title"], "Survey the telemetry surface")

    def test_append_pending_task_creates_a_general_section_for_an_empty_plan(self):
        from tools.plan_state import append_pending_task

        plan = {"title": "Empty", "sections": [], "steps": []}
        task = append_pending_task(plan, "First milestone", note="From a recommendation.")
        saved = self.save_state(plan)

        self.assertEqual(task["id"], "task-1")
        self.assertEqual(task["section"], "General")
        self.assertEqual(plan["sections"][0]["id"], "sec-1")
        self.assertEqual(saved["steps"][0]["title"], "First milestone")

    def test_append_pending_task_none_note_yields_empty_details(self):
        from tools.plan_state import append_pending_task

        plan = {"sections": [{"id": "sec-1", "title": "1. Setup", "tasks": []}], "steps": []}
        task = append_pending_task(plan, "Untitled", tag=UI_TAG)
        self.assertEqual(task["details"], [])
        self.assertEqual(task["tag"], UI_TAG)

    def test_save_plan_state_raises_rather_than_reporting_a_failed_write(self):
        from tools.plan_state import PlanWriteError

        self._save_demo()
        # A failure in either half of the plan pair must not return the state as if it had
        # been written -- that is what let the UI show a plan the disk did not hold.
        with mock.patch("tools.plan_state.compile_plan_json_to_markdown",
                        side_effect=RuntimeError("compile exploded")):
            with self.assertRaises(PlanWriteError):
                ft.save_plan_state(self.read_state())

    def test_sync_plan_on_disk_returns_state(self):
        self._save_demo()
        self.assertEqual(len(ft.sync_plan_on_disk()["steps"]), 5)

    def test_a_corrupt_plan_json_is_reported_not_hidden(self):
        # A readable markdown plan and a plan.json that cannot be parsed: the plan is recovered
        # from the markdown, and the corruption is explained rather than shown as an empty tree
        # (audit F3).
        from tools.plan_state import last_plan_load_error

        self.write("PLAN.md", "# P\n\n## 1. S\n- [ ] Do a thing\n")
        self.write("plan.json", "{not valid json")

        state = ft.load_plan_state()

        self.assertIn("plan.json could not be read", last_plan_load_error())
        self.assertEqual(len(state["steps"]), 1)

    def test_a_readable_plan_reports_no_load_error(self):
        from tools.plan_state import last_plan_load_error

        self._save_demo()
        ft.load_plan_state()
        self.assertEqual(last_plan_load_error(), "")

    def test_files_and_details_survive_a_store_round_trip(self):
        """The store is the only representation, so `files`/`details` cannot be lost.

        The old machine kept two files and picked between them by mtime; the markdown
        side dropped every `files` entry, so audit and rollback results depended on which
        write won a sub-millisecond race. There is one representation now.
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

        state = self.read_state()
        self.assertEqual([t["files"] for t in state["steps"]], [["alpha.py"], ["beta.py"]])
        self.assertEqual([t["status"] for t in state["steps"]], ["completed", "pending"])
        self.assertEqual([t["details"] for t in state["steps"]], [["note one"], []])

    def test_list_plan_files(self):
        self.write("PLAN.md", PLAN_MD)
        self.write("ROADMAP.md", PLAN_MD)
        self.write("notes.txt", "x")
        self.assertEqual(ft.list_plan_files(), ["PLAN.md", "ROADMAP.md"])

    def test_list_plan_files_skips_non_plan_documents(self):
        # Candidacy is decided by content, not by name: prose documents that are not shaped
        # like a roadmap (no sections/milestones) are not offered as switchable plans, so
        # the Dual-Sync engine cannot be pointed at one and left with a blank, locked tree.
        self.write("PLAN.md", PLAN_MD)
        self.write("PROGRESS.md", "# Project Plan & Execution Tracker\n")
        self.write("README.md", "# readme\n")
        self.write("CHANGELOG.md", "# changelog\n")
        self.assertEqual(ft.list_plan_files(), ["PLAN.md"])

    def test_list_plan_files_offers_a_milestone_shaped_tracker(self):
        # A tracker that actually carries sections and checkboxes is a switchable plan
        # whatever it is called -- no filename may hide it.
        self.write("PLAN.md", PLAN_MD)
        self.write("PROGRESS.md", "# Progress\n\n## Roadmap\n- [x] done\n- [ ] todo\n")
        self.assertEqual(ft.list_plan_files(), ["PLAN.md", "PROGRESS.md"])

    def test_list_plan_files_always_includes_the_active_plan(self):
        # Even an unstructured active plan stays in the list: it is what the app is
        # working on, and the Normalization Gate exists to repair its shape.
        self.write("PLAN.md", "# Just a title\n\nsome prose, no milestones\n")
        self.write("ROADMAP.md", PLAN_MD)
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
        ft.overwrite_source("kept.py", "k\n")
        ft.overwrite_source("built.py", "b\n")

    def test_backup_records_modified_and_created(self):
        ft.overwrite_source("existing.py", "v1\n")
        backup_path = ft.backup_file_for_task("task-9", "existing.py")
        self.assertTrue(backup_path and os.path.isfile(backup_path))

        created = ft.backup_file_for_task("task-9", "brand_new.py")
        self.assertIsNone(created)

        meta = self.read(os.path.join(".aleth_backups", "task-9", "_meta.json"))
        self.assertIn('"action": "modified"', meta)
        self.assertIn('"action": "created"', meta)

    def test_audit_detects_both_discrepancy_classes(self):
        self._seed_completed_task_with_missing_deliverable()
        ft.overwrite_source("untracked.py", "u\n")

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
        ft.overwrite_source("only.py", "x\n")

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
        ft.overwrite_source("restored.py", "original\n")
        ft.backup_file_for_task("task-1", "restored.py")
        ft.overwrite_source("restored.py", "patched\n")

        ft.backup_file_for_task("task-1", "flaky.py")
        ft.overwrite_source("flaky.py", "new\n")

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

    def test_rollback_reports_a_failed_file_restore_instead_of_success(self):
        # A rollback that could not put a file back must not report success: the task's status
        # was reset but the workspace still holds the patched code (audit M7).
        ft.overwrite_source("restored.py", "original\n")
        ft.backup_file_for_task("task-1", "restored.py")
        self.save_state({
            "title": "RB2",
            "sections": [{"id": "sec-1", "title": "1. S", "tasks": [
                {"id": "task-1", "section": "1. S", "title": "Rollback me", "status": "completed",
                 "details": [], "files": ["restored.py"]},
            ]}],
        })

        with mock.patch("tools.recovery.shutil.copy2", side_effect=OSError("disk full")):
            res = ft.rollback_task_state("task-1")

        self.assertFalse(res["success"])
        self.assertTrue(res["restore_errors"])
        self.assertIn("restored.py", res["error"])

    # --- whole-plan revision snapshot / revert ---------------------------------

    def test_snapshot_then_revert_restores_the_previous_plan(self):
        self.save_state({
            "title": "Rev",
            "sections": [{"id": "sec-1", "title": "1. S", "tasks": [
                {"id": "task-1", "section": "1. S", "title": "Original", "status": "pending",
                 "details": [], "files": []},
            ]}],
        })
        self.assertTrue(ft.snapshot_plan_revision())

        # A revision lands: the original milestone is rewritten.
        self.save_state({
            "title": "Rev",
            "sections": [{"id": "sec-1", "title": "1. S", "tasks": [
                {"id": "task-1", "section": "1. S", "title": "Rewritten", "status": "pending",
                 "details": [], "files": []},
            ]}],
        })
        self.assertEqual([s["title"] for s in self.read_state()["steps"]], ["Rewritten"])

        res = ft.revert_plan_revision()
        self.assertTrue(res["success"])
        self.assertEqual([s["title"] for s in res["tree"]], ["Original"])
        self.assertEqual([s["title"] for s in self.read_state()["steps"]], ["Original"])

    def test_revert_without_a_snapshot_is_refused_not_raised(self):
        res = ft.revert_plan_revision()
        self.assertFalse(res["success"])
        self.assertIn("no captured plan revision", res["error"])

    def test_snapshot_with_no_plan_on_disk_reports_nothing_captured(self):
        self.assertFalse(ft.snapshot_plan_revision())


class TaskDiffTests(WorkspaceTestCase):
    """The read-only diff surface behind the UI's tracked-edits pane."""

    def _seed_diffable_task(self):
        ft.overwrite_source("existing.py", "v1\n")
        ft.backup_file_for_task("task-9", "existing.py")
        ft.overwrite_source("existing.py", "v2\n")

        ft.backup_file_for_task("task-9", "brand_new.py")
        ft.overwrite_source("brand_new.py", "b\n")

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
            self.read(os.path.join(".aleth_backups", "task-9", "existing.py")),
            "v1\n",
        )


class VerificationResultParserTests(unittest.TestCase):
    """The exit-code reader behind the Architect's compile gate.

    ``run_command_in_workspace`` always returns a non-empty block -- ``[Exit Code: N]``
    plus the output, or "(Command executed successfully with no output)" when there is
    none. Reading success with ``bool(result)`` therefore marks *every* verified task
    failed. These pin the three-way reading: zero, non-zero, and no verdict at all.
    """

    def test_a_clean_run_exits_zero(self):
        result = "[Exit Code: 0]\n(Command executed successfully with no output)"
        self.assertEqual(command_exit_code(result), 0)
        self.assertFalse(command_failed(result))

    def test_a_syntax_error_is_a_non_zero_exit(self):
        result = (
            '[Exit Code: 1]\n[STDERR]\n  File "main.py", line 3\n    def broken(\n'
            "SyntaxError: invalid syntax"
        )
        self.assertEqual(command_exit_code(result), 1)
        self.assertTrue(command_failed(result))

    def test_an_inconclusive_result_has_no_exit_code_and_does_not_fail(self):
        # A timeout, a start-up error or a whitelist refusal never produced a verdict, and
        # "could not judge" must not be read as "failed".
        for result in (
            "Error: Command timed out after 30 seconds.",
            "Execution error: [WinError 2] The system cannot find the file specified",
            "Permission Denied: Command 'x' is not in the Architect's approved whitelist.",
            "",
        ):
            with self.subTest(result=result):
                self.assertIsNone(command_exit_code(result))
                self.assertFalse(command_failed(result))


class PreviewSourceTests(WorkspaceTestCase):
    """The read-only text surface behind the UI's live preview.

    The preview renders through the bridge rather than pointing an iframe at a file://
    URL, because WebView2 does not reliably finish a local document load. These pin what
    the bridge hands over instead.
    """

    def test_reads_the_interface_file(self):
        ft.overwrite_source(ft.PREVIEW_FILENAME, "<h1>hi</h1>\n")
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
        outside = tempfile.mkdtemp(prefix="aleth_previewoutside_")
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
        ft.overwrite_source(ft.PREVIEW_FILENAME, "0123456789ABCDEF")
        with mock.patch("tools.workspace_io.MAX_PREVIEW_CHARS", 10):
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
        path = self._write_env("ALETH_TEST_UNSET_NAME=v\n")

        res = ft.read_environment_variables(path)

        self.assertEqual(res["variables"],
                         [{"name": "ALETH_TEST_UNSET_NAME", "set": False, "length": 0}])

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
            f"ALETH_TEST_KEY_{i}=v" for i in range(ft.MAX_ENVIRONMENT_VARIABLES + 10)
        )
        path = self._write_env(lines + "\n")

        res = ft.read_environment_variables(path)

        self.assertEqual(len(res["variables"]), ft.MAX_ENVIRONMENT_VARIABLES)
        self.assertEqual(res["variables"][0]["name"], "ALETH_TEST_KEY_0")


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
        # The shell is no longer a static catalog entry: it is the exec server's tool, bound
        # per run from the session (see tests/test_mcp.py).
        self.assertNotIn("execute_restricted_command", architect["tools"])
        self.assertTrue(architect["file_path"].endswith("architect.py"))

        self.assertEqual([a["id"] for a in summary["coder_agents"]], ["coder-deep", "coder-standard"])
        deep = summary["coder_agents"][0]
        self.assertEqual(deep["type"], "coder")
        self.assertEqual(deep["role"], "Sub-Agent")
        self.assertEqual(deep["parent_agent"], "software-architect")
        self.assertEqual(deep["display_name"], "Senior Backend Coder")
        # The Coder's shell is the exec server's ``execute_command`` now, bound per run from
        # the session rather than snapshotted into the catalog.
        self.assertNotIn("execute_shell_command", deep["tools"])

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


class _SilentCtx:
    """A no-op WorkflowContext, so a test can plan without polluting the event stream."""

    plan_file = "PLAN.md"

    def emit_fn(self, event):
        return None

    def stream_text(self, *args, **kwargs):
        return None

    def should_stop(self):
        return False


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

    def setUp(self):
        super().setUp()
        # Planning requires System 2, which this suite deliberately disables. Patch the
        # planner with a canned artifact so these tests exercise the *execution* half of the
        # lifecycle; the real planner is covered in tests/test_artifact_gate.py.
        from orchestration.workflow import generation, planner, templates

        def _canned(task, *, plan_id=None, workspace_dir=None, completer=None, session=None,
                    model=None, base_url=None, api_key=None):
            # ``model``/``base_url``/``api_key`` are part of ``plan_task``'s contract now: the
            # caller names the route the router selected and the endpoint that reaches it. The
            # canned planner ignores them -- it is a fixture, not a router -- but it must accept
            # them, exactly as the real planner does.
            title = str(task.get("title") or "")
            deliverable = templates.select(title, task.get("tag"))
            files = [str(name) for name in (task.get("files") or []) if str(name).strip()]
            primary = files[0] if files else deliverable.filename
            return planner.ImplementationPlanArtifact(
                plan_id=plan_id or "PLAN",
                task_id=str(task.get("id") or title),
                summary=title or "canned",
                ast_targets=[
                    planner.ASTTarget(file_path=primary, operation="insert", content=deliverable.code),
                    planner.ASTTarget(
                        file_path=generation.paired_test_path(primary),
                        operation="insert",
                        content=deliverable.test_code,
                    ),
                ],
                estimated_impact="canned plan for the execution-half tests",
                # Mandatory on the artifact (Phase 7): a payload without an assessment is refused,
                # so the canned planner states one like any real one would.
                complexity_score=2,
                required_capabilities=[],
            )

        patcher = mock.patch("orchestration.workflow.planner.plan_task", side_effect=_canned)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _approve(self, task_id):
        """Approve a task: record its plan, then move it to ``in_progress``.

        This mirrors the real lifecycle -- an artifact exists *because* the task was planned,
        and approval is what makes it executable. Approving without planning would leave the
        executor with nothing to apply.
        """
        from orchestration.workflow import planner

        task = planner.task_by_id(ft.load_plan_state(), task_id)
        if task is not None:
            planner.plan_and_yield(_SilentCtx(), task=task, plan_file="PLAN.md")
        ft.update_plan_task_status(task_id, "in_progress")

    def _approve_adhoc(self, files, title="Ad-hoc work"):
        """Pre-create and approve the ephemeral node a directive will look for.

        ``fix_bug``/``custom`` spawn an ``adhoc-N`` node when they name no plan task. To
        exercise the *execution* half of the lifecycle, that node must already exist and be
        approved (``in_progress``) before the run -- ``ensure_task_for_directive`` reuses an
        existing planned/in_progress ad-hoc node rather than spawning a second one.
        """
        from orchestration.workflow import planner

        node = planner.spawn_ephemeral_task(plan_id="PLAN", title=title, files=list(files))
        self._approve(node["id"])
        return node["id"]

    def _approve_adhoc_with_artifact(self, files, title="Ad-hoc work"):
        """Plan an ad-hoc node (recording its artifact), then approve it.

        ``_approve_adhoc`` marks a node ``in_progress`` with no artifact, which is the right
        fixture for the *branch* execution halves. The approval pass applies the stored
        artifact, so it needs one: this records the plan first, exactly as a real planning
        run would, then approves it.
        """
        from orchestration.workflow import planner

        node = planner.spawn_ephemeral_task(plan_id="PLAN", title=title, files=list(files))
        planner.plan_and_yield(_SilentCtx(), task=node, plan_file="PLAN.md")
        ft.update_plan_task_status(node["id"], "in_progress")
        return node["id"]

    def test_a_pending_task_is_planned_and_the_run_yields(self):
        """Phase 1: a fresh task produces a plan, not a diff.

        Nothing is written and the run ends ``planned`` -- the task waits for a human
        approval before any executor touches the code.
        """
        self._seed_scaffold()
        events = self._collect("next_step", "[ACTION: EXECUTE_NEXT_STEP]")

        self.assertEqual(events[-1]["type"], "workflow_complete")
        self.assertEqual(events[-1]["status"], "planned")
        self.assertIn("Awaiting Approval", [e.get("summary", {}).get("status") for e in events])
        # Nothing was written during planning.
        self.assertFalse(os.path.isfile(os.path.join(self.tmp, "main.py")))
        # And the task is halted in the store, with the artifact persisted for the executor.
        halted = [t for t in ft.load_plan_state()["steps"] if t["id"] == "task-2"][0]
        self.assertEqual(halted["status"], "planned")
        from storage.db import get_store

        self.assertIsNotNone(get_store().get_artifact("PLAN", "task-2"))

    def test_next_step_event_sequence(self):
        self._seed_scaffold()
        self._approve("task-2")
        events = self._collect("next_step", "[ACTION: EXECUTE_NEXT_STEP]")

        self.assertEqual(self._types(events), [
            "workflow_started", "architect_spawn",
            "log", "log", "log",
            "delegation", "coder_spawn",
            "log", "log",
            "tool_call", "tool_result",  # write_file deliverable
            "tool_call", "tool_result",  # write_file test
            "log",                       # verifying narration
            "tool_call", "tool_result",  # py_compile
            "tool_call", "tool_result",  # run_task_tests
            "tool_call", "tool_result",  # save_plan_state
            "plan_updated",
            "coder_summary",
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
        # A clean compile must read as verified, not as an error: the result block is never
        # empty, so the gate reads its exit code rather than the string's truthiness.
        compile_result = [
            e for e in events
            if e["type"] == "tool_result" and e["tool"] == "execute_restricted_command"
        ][0]
        self.assertEqual(compile_result["result"], "Syntax and compilation verified with 0 errors")
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
        meta = json.loads(self.read(os.path.join(".aleth_backups", "task-2", "_meta.json")))
        self.assertEqual(meta["main.py"]["action"], "created")
        self.assertEqual(meta["test_main.py"]["action"], "created")

    def test_next_step_marks_a_task_failed_when_its_tests_fail(self):
        """A verified-negative is the only thing that flips the mark to `[!]`.

        The Coder's own test is the gate: when it runs and fails, the task is not
        completed, the failure is written into the plan (so a reload remembers why), and
        the proposals offer a retry rather than a rollback. The template seam is injected
        so the failing test is deterministic rather than dependent on generated content.
        """
        self._seed_scaffold()
        failing = templates.Deliverable(
            filename="main.py",
            test_filename="test_main.py",
            code="class SolutionEngine:\n    pass\n",
            test_code="def test_always_fails():\n    assert False\n",
        )
        with mock.patch("orchestration.workflow.templates.select", return_value=failing):
            # The plan must be written *under the patch*: the artifact carries the payload,
            # so the failing test only reaches disk if it was in the approved plan.
            self._approve("task-2")
            events = self._collect("next_step", "[ACTION: EXECUTE_NEXT_STEP]")

        plan_event = [e for e in events if e["type"] == "plan_updated"][0]
        task = plan_event["tree"][1]
        self.assertEqual(task["id"], "task-2")
        self.assertEqual(task["status"], "failed")
        self.assertTrue(
            any(
                detail.startswith("Verification failed: tests did not pass")
                for detail in task["details"]
            ),
            task["details"],
        )

        summary = events[-2]["summary"]
        self.assertEqual(summary["status"], "Verification Failed")
        self.assertTrue(summary["proposals"][0].startswith("Retry '"))
        self.assertEqual(
            events[-1]["message"],
            "Task 'Project scaffolding and runtime dependencies' failed verification and needs attention.",
        )

    def test_the_scheduler_skips_a_blocked_task_and_plans_the_eligible_one(self):
        """Eligibility decides, not list order.

        The scaffold gates task-3 on the still-pending task-2, so the next eligible node is
        task-2. Selecting by list order would plan task-3 and, once approved, execute it
        before the state it depends on exists.
        """
        from storage.db import get_store

        self._seed_scaffold()
        events = self._collect("next_step", "[ACTION: EXECUTE_NEXT_STEP]")

        self.assertEqual(events[-1]["status"], "planned")
        planned = [n.id for n in get_store().get_dag("PLAN").ordered_nodes() if n.status == "planned"]
        self.assertEqual(planned, ["task-2"])

    def test_a_targeted_task_with_incomplete_blockers_is_refused(self):
        """A blocked node is never run, even when it is named explicitly."""
        self._seed_scaffold()
        events = self._collect(
            "next_step", "[ACTION: EXECUTE_NEXT_STEP]", {"targetTaskId": "task-5"}
        )

        self.assertNotIn("delegation", self._types(events))
        logs = "\n".join(str(e.get("text", "")) for e in events if e["type"] == "log")
        self.assertIn("blocked", logs.lower())
        self.assertIn("task-3", logs)
        self.assertIn("Input Required", [e.get("summary", {}).get("status") for e in events])
        self.assertEqual(events[-1]["type"], "workflow_complete")
        self.assertEqual(events[-1]["status"], "finished")

    def test_completing_the_blockers_lets_a_targeted_task_run(self):
        """The gate opens when the blockers are completed, and only then."""
        self._seed_scaffold()
        ft.update_plan_task_status("task-2", "completed")
        self._approve("task-3")
        events = self._collect(
            "next_step", "[ACTION: EXECUTE_NEXT_STEP]", {"targetTaskId": "task-3"}
        )

        delegation = [e for e in events if e["type"] == "delegation"]
        self.assertTrue(delegation, self._types(events))
        self.assertEqual(delegation[0]["target_agent"], "coder-deep")

    def test_a_fix_bug_targeted_at_a_blocked_task_is_refused(self):
        """A blocked node is a blocked node, however it was named.

        task-5 is gated on the incomplete task-3, so a fix aimed at it would have the model
        diagnose a node whose prerequisite state does not exist -- and invent the difference.
        """
        self._seed_buggy_main()
        events = self._collect(
            "fix_bug", "[ACTION: FIX_BUG] something broke",
            {"bugDescription": "IndexError in run()", "targetTaskId": "task-5"},
        )

        self.assertNotIn("delegation", self._types(events))
        logs = "\n".join(str(e.get("text", "")) for e in events if e["type"] == "log")
        self.assertIn("blocked", logs.lower())
        self.assertIn("task-3", logs)
        self.assertIn("Input Required", [e.get("summary", {}).get("status") for e in events])
        self.assertEqual(events[-1]["type"], "workflow_complete")

    def test_a_custom_directive_targeted_at_a_blocked_task_is_refused(self):
        self._seed_scaffold()
        events = self._collect("custom", "add a login endpoint", {"targetTaskId": "task-5"})

        self.assertNotIn("delegation", self._types(events))
        logs = "\n".join(str(e.get("text", "")) for e in events if e["type"] == "log")
        self.assertIn("task-3", logs)
        self.assertEqual(events[-1]["type"], "workflow_complete")

    def test_an_adhoc_directive_is_instantly_eligible(self):
        """No target node -> an ephemeral one with no blockers, so the gate never fires."""
        self._seed_scaffold()
        events = self._collect("custom", "add a login endpoint")

        self.assertEqual(events[-1]["status"], "planned")
        self.assertNotIn("Task Blocked By Dependencies", [
            e.get("summary", {}).get("title") for e in events
        ])

    def test_next_step_targets_the_deep_coder_for_core_tasks(self):
        self._seed_scaffold()
        # task-3 is gated on task-2, so the blocker is completed first -- the scheduler will
        # not run a node whose blockers are incomplete, targeted or not.
        ft.update_plan_task_status("task-2", "completed")
        self._approve("task-3")
        events = self._collect(
            "next_step", "[ACTION: EXECUTE_NEXT_STEP]", {"targetTaskId": "task-3"}
        )

        delegation = [e for e in events if e["type"] == "delegation"][0]
        self.assertEqual(delegation["target_agent"], "coder-deep")
        self.assertEqual(delegation["target_name"], "Senior Backend Coder")
        self.assertEqual(delegation["task"], "Build core domain models and application logic")

        spawn = [e for e in events if e["type"] == "coder_spawn"][0]
        self.assertEqual(spawn["model"], "openai:policy/coder-deep-test")

        # An explicit target completes that task, not the first pending one. task-2 is
        # completed too -- it was the precondition that made task-3 eligible to run at all.
        plan_event = [e for e in events if e["type"] == "plan_updated"][0]
        completed = [t["id"] for t in plan_event["tree"] if t["status"] == "completed"]
        self.assertEqual(completed, ["task-1", "task-2", "task-3"])
        self.assertEqual(
            [t["status"] for t in plan_event["tree"] if t["id"] == "task-4"], ["pending"]
        )

    def test_next_step_ui_task_emits_the_vision_directive(self):
        self.save_state({
            "title": "UI Demo",
            "sections": [{"id": "sec-1", "title": "1. S", "tasks": [
                {"id": "task-1", "section": "1. S", "title": "Dashboard view", "status": "pending",
                 "tag": "FE", "details": [], "files": []},
            ]}],
        })
        self._approve("task-1")
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

    def test_fix_bug_treats_an_uncollectable_regression_suite_as_a_failure(self):
        """verdict 'error' (the suite could not be collected) must not read as a pass.

        The canned regression suite imports a symbol the target need not define, which
        surfaces as a collection error -- 'error', not 'failed' (audit H2).
        """
        self._seed_buggy_main()
        self._approve_adhoc(["main.py"])
        with mock.patch("orchestration.workflow.actions_impl.run_task_tests",
                        return_value={"verdict": "error", "summary": "1 error"}):
            events = self._collect("fix_bug", "[ACTION: FIX_BUG] something broke",
                                   {"bugDescription": "IndexError in run()"})

        summary = [e for e in events if e["type"] == "architect_summary"][0]["summary"]
        self.assertEqual(summary["status"], "Verification Failed")

    def test_custom_verification_failure_is_reported_not_assumed(self):
        """The Architect's verdict must come from the compile check, not a literal.

        ``v_res`` used to be computed and discarded, so broken custom code still read as
        'Verified & Approved' (audit C1). The compile is mocked to fail here.
        """
        self._seed_scaffold()
        self._approve_adhoc(["main.py"])
        # The compile runs through the run's MCP session now, so the session is what a test
        # stubs -- there is no module-level tool object left to patch.
        with mock.patch(
            "orchestration.mcp_session.MCPSessionContext.execute",
            return_value="[Exit Code: 1]\nSyntaxError: invalid syntax",
        ):
            events = self._collect("custom", "add a login endpoint")

        summary = [e for e in events if e["type"] == "architect_summary"][0]["summary"]
        self.assertEqual(summary["status"], "Verification Failed")
        self.assertIn("Verification failed:", summary["deliverables"][1])
        self.assertIn("verification failed", events[-1]["message"])

    def _seed_buggy_main(self):
        self._seed_scaffold()
        # The canned regression suite asserts ``run()`` returns ``verified: True``, so the
        # fixture has to return one for the suite to be a passing check rather than an
        # accidental failure -- the "always failed" trap the compile gate was fixed for,
        # in a second guise.
        ft.overwrite_source("main.py", (
            "from typing import Dict, Any\n\n"
            "class SolutionEngine:\n"
            "    def run(self) -> Dict[str, Any]:\n"
            "        return {'status': 'success', 'verified': True}\n"
        ))
        ft.overwrite_source("test_main.py", "def test_ok():\n    assert True\n")

    def test_an_unapproved_fix_bug_is_planned_and_the_run_yields(self):
        """Phase 1 for a fix: the report is planned and the run halts, writing nothing.

        This is the path the console and the palette take -- no pre-approved node -- and it
        is the one the Artifact Gate has to intercept. It is also the path a stale variable
        name silently broke: without a test here, planning a fix raised before it could halt.
        """
        self._seed_buggy_main()
        before = self.read("main.py")
        events = self._collect(
            "fix_bug", "[ACTION: FIX_BUG] something broke",
            {"bugDescription": "IndexError in run()"},
        )

        self.assertEqual(events[-1]["type"], "workflow_complete")
        self.assertEqual(events[-1]["status"], "planned")
        self.assertIn("Awaiting Approval", [e.get("summary", {}).get("status") for e in events])
        # Nothing was written during planning.
        self.assertEqual(self.read("main.py"), before)

        from storage.db import get_store

        node = [n for n in get_store().get_dag("PLAN").ordered_nodes() if n.id.startswith("adhoc-")][0]
        self.assertEqual(node.status, "planned")
        self.assertIsNotNone(get_store().get_artifact("PLAN", node.id))

    def test_fix_bug_event_sequence(self):
        self._seed_buggy_main()
        self._approve_adhoc(["main.py"])
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
            "tool_call", "tool_result",   # the approved artifact applied
            "coder_summary",
            "log",
            "tool_call", "tool_result",   # py_compile
            "tool_call", "tool_result",   # run_task_tests
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

        # The approved artifact IS the payload: the executor writes exactly the bytes the
        # plan carried -- here the canned plan's deliverable -- and nothing is generated.
        patched = self.read("main.py")
        self.assertIn("class SolutionEngine", patched)
        self.assertIn("def run(self) -> Dict[str, Any]:", patched)
        self.assertEqual(self.read("test_main.py"), templates.SOLUTION_ENGINE_TEST)
        self.assertTrue(
            os.path.isfile(os.path.join(self.tmp, ".aleth_backups", "bugfix", "main.py"))
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
        # The diagnosis is structured for the result view, not only narrated into the log:
        # the frontend's root-cause / resolution split reads these two fields.
        # The offline path does not diagnose, so the field states the reported symptom rather
        # than a canned cause (audit M14).
        self.assertEqual(summary["root_cause"], "Reported symptom: IndexError in run()")
        self.assertEqual(
            summary["fix_spec"],
            "Apply a defensive guard for the reported failure in `test_main.py`.",
        )
        self.assertEqual(
            events[-1]["message"], "Bug surgically diagnosed, patched, and verified."
        )

        # No target task was named, so no plan state is written: the console/palette path
        # stays byte-for-byte what it has always emitted.
        self.assertEqual([e for e in events if e["type"] == "plan_updated"], [])

    def test_fix_bug_refuses_when_no_file_can_be_targeted(self):
        """No code in the workspace and no file named: refuse rather than invent main.py (M15)."""
        self._seed_scaffold()
        events = self._collect("fix_bug", "[ACTION: FIX_BUG] something is broken",
                               {"bugDescription": "it crashes"})

        summary = [e for e in events if e["type"] == "architect_summary"][0]["summary"]
        self.assertEqual(summary["status"], "Input Required")
        self.assertEqual(events[-1]["message"], "Bug report needs a target file.")
        # Nothing was written: the refusal happens before any Coder is summoned.
        self.assertNotIn("delegation", self._types(events))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "main.py")))

    def test_fix_bug_reports_failure_when_the_regression_test_fails(self):
        """The fix is gated like a task: a failing regression test is not an approval.

        The suite text is injected so the failure is deterministic. In the default path it
        cannot be relied on either way -- the canned suite imports ``SolutionEngine``, which
        a real target file need not contain, so the runner reports an *error* and the fix is
        left approved rather than failed. A definite failure has to come from a test that
        actually runs.
        """
        self._seed_buggy_main()
        with mock.patch(
            "orchestration.workflow.templates.SOLUTION_ENGINE_TEST",
            "def test_regression():\n    assert False\n",
        ):
            # The plan must be recorded under the patch: the artifact carries the test's
            # bytes, so a failing suite only reaches disk if it was in the approved plan.
            self._approve_adhoc(["main.py"])
            events = self._collect(
                "fix_bug", "[ACTION: FIX_BUG] something broke",
                {"bugDescription": "IndexError in run()"},
            )

        coder_summary = [e for e in events if e["type"] == "coder_summary"][0]["summary"]
        # The coder claims only what it did -- the approved plan was applied; the
        # Architect runs the tests.
        self.assertEqual(coder_summary["status"], "Patch Applied")

        summary = events[-2]["summary"]
        self.assertEqual(summary["status"], "Verification Failed")
        self.assertTrue(
            any(
                detail.startswith("Verification failed: tests did not pass")
                for detail in summary["deliverables"]
            ),
            summary["deliverables"],
        )
        self.assertTrue(summary["proposals"][0].startswith("Retry the bug fix"))
        self.assertEqual(
            events[-1]["message"], "Bug fix failed verification and needs attention."
        )

    def test_fix_bug_with_a_target_task_marks_that_task_failed(self):
        """A targeted fix writes its verdict onto the task it names.

        The Fix affordance on a failed plan card passes the task id, so the failure the fix
        uncovers has to land on that task -- otherwise the mark and the reason would live
        only in the transcript and a reload would lose both.
        """
        self._seed_buggy_main()
        with mock.patch(
            "orchestration.workflow.templates.SOLUTION_ENGINE_TEST",
            "def test_regression():\n    assert False\n",
        ):
            self._approve("task-2")
            events = self._collect(
                "fix_bug", "[ACTION: FIX_BUG] something broke",
                {"bugDescription": "IndexError in run()", "targetTaskId": "task-2"},
            )

        plan_event = [e for e in events if e["type"] == "plan_updated"][0]
        self.assertEqual(plan_event["filename"], "PLAN.md")
        task = next(t for t in plan_event["tree"] if t["id"] == "task-2")
        self.assertEqual(task["status"], "failed")
        self.assertTrue(
            any(
                detail.startswith("Verification failed: tests did not pass")
                for detail in task["details"]
            ),
            task["details"],
        )

    def test_fix_bug_with_a_target_task_clears_a_failed_mark_back_to_pending(self):
        """A verified fix leaves the approved task runnable again, not completed.

        Approval is what unlocks execution, so the task the fix answers for arrives
        `in_progress`; the fix records its verdict there and does not claim the milestone,
        so only a re-run can complete it.
        """
        self._seed_buggy_main()
        state_dict = self.read_state()
        for sec in state_dict.get("sections", []):
            for t in sec.get("tasks", []):
                if t.get("id") == "task-2":
                    t["status"] = "failed"
        self.save_state(state_dict)
        self._approve("task-2")

        events = self._collect(
            "fix_bug", "[ACTION: FIX_BUG] something broke",
            {"bugDescription": "IndexError in run()", "targetTaskId": "task-2"},
        )

        plan_event = [e for e in events if e["type"] == "plan_updated"][0]
        task = next(t for t in plan_event["tree"] if t["id"] == "task-2")
        self.assertEqual(task["status"], "in_progress")
        self.assertIn("Bug fix verified in `main.py`.", task["details"])
        self.assertEqual(
            events[-1]["message"], "Bug surgically diagnosed, patched, and verified."
        )

    def test_fix_bug_with_a_target_task_leaves_a_non_failed_task_in_its_own_state(self):
        """A verified fix never invents or destroys a completion.

        The task is approved to `in_progress` so the fix may run; the fix records its
        evidence and leaves that approved status alone rather than claiming a milestone
        its own gate never verified.
        """
        self._seed_buggy_main()
        state_dict = self.read_state()
        for sec in state_dict.get("sections", []):
            for t in sec.get("tasks", []):
                if t.get("id") == "task-2":
                    t["status"] = "completed"
        self.save_state(state_dict)
        self._approve("task-2")

        events = self._collect(
            "fix_bug", "[ACTION: FIX_BUG] something broke",
            {"bugDescription": "IndexError in run()", "targetTaskId": "task-2"},
        )

        plan_event = [e for e in events if e["type"] == "plan_updated"][0]
        task = next(t for t in plan_event["tree"] if t["id"] == "task-2")
        self.assertEqual(task["status"], "in_progress")
        self.assertIn("Bug fix verified in `main.py`.", task["details"])

    def test_fix_bug_falls_back_to_the_message_and_strips_action_tags(self):
        self._seed_buggy_main()
        self._approve_adhoc(["main.py"])
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
        self._approve_adhoc(["main.py"])
        events = self._collect(
            "fix_bug", "[ACTION: FIX_BUG]", {"bugAttachment": "traceback.txt"}
        )

        self.assertIn("delegation", self._types(events))
        self.assertEqual(events[-1]["message"], "Bug surgically diagnosed, patched, and verified.")

    def test_custom_directive_delegates_to_the_deep_coder(self):
        self._seed_scaffold()
        self._approve_adhoc(["main.py"])
        events = self._collect("custom", "add a login endpoint")

        self.assertEqual(self._types(events), [
            "workflow_started", "architect_spawn", "log", "log", "log",
            "delegation", "coder_spawn", "log", "coder_summary", "log",
            "architect_summary", "workflow_complete",
        ])
        delegation = events[5]
        self.assertEqual(delegation["target_agent"], "coder-deep")
        self.assertEqual(delegation["task"], "add a login endpoint")

        # The approved artifact is the payload: the executor wrote the plan's bytes, not a
        # fresh generation. The canned plan uses the task's own title.
        self.assertIn("class SolutionEngine", self.read("main.py"))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "test_main.py")))

        # Only the primary deliverable is snapshotted on the custom path.
        meta = json.loads(self.read(os.path.join(".aleth_backups", "custom", "_meta.json")))
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
        # The directive named no plan task, so it was planned and the run yielded: the
        # execution half -- and the approved artifact it needs -- is still ahead of it.
        self.assertEqual(events[-1]["status"], "planned")

    def test_an_unapproved_custom_directive_is_planned_and_the_run_yields(self):
        """Phase 1 for a free-form coding directive: planned and halted, writing nothing.

        The custom path is the Gatekeeper's fallback, so its plan half is reached by a
        directive that names no plan task and is classified as code modification. Nothing
        may be written until a human approves the artifact -- the same gate every other
        branch is behind. This is the custom counterpart of the fix_bug plan-half test:
        without it, a fault in the plan half (as the fix_bug NameError was) would only be
        caught by the execution-half tests that pre-approve.
        """
        self._seed_scaffold()
        events = self._collect("custom", "add a login endpoint")

        self.assertEqual(events[-1]["type"], "workflow_complete")
        self.assertEqual(events[-1]["status"], "planned")
        self.assertIn("Awaiting Approval", [e.get("summary", {}).get("status") for e in events])
        # Nothing was written during planning.
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "main.py")))

        from storage.db import get_store

        node = [n for n in get_store().get_dag("PLAN").ordered_nodes() if n.id.startswith("adhoc-")][0]
        self.assertEqual(node.status, "planned")
        self.assertIsNotNone(get_store().get_artifact("PLAN", node.id))

    def test_the_approval_pass_applies_the_artifact_without_re_deciding(self):
        """The execution half calls neither System 1 nor System 2.

        The bug this pins: approval used to re-issue the originating request, so a `custom`
        directive was re-classified during execution and a different verdict could strand the
        artifact a human had approved. ``laya_model.classify`` is the only classifier, so
        making it raise proves the pass never reaches it.
        """
        self._seed_scaffold()
        task_id = self._approve_adhoc_with_artifact(["main.py"])

        with mock.patch(
            "orchestration.workflow.actions_impl.laya_model.classify",
            side_effect=AssertionError("System 1 must not run during execution"),
        ), mock.patch(
            "orchestration.workflow.execution.run_task_tests",
            return_value={"verdict": "passed", "summary": "1 passed"},
        ):
            events = self._collect("execute_artifact", "", {"taskId": task_id, "planId": "PLAN"})

        # The approved artifact landed, verbatim: what was approved is what is on disk.
        from storage.db import get_store

        expected = get_store().get_artifact("PLAN", task_id)["ast_targets"][0]["content"]
        self.assertEqual(self.read("main.py"), expected)
        # The task reached a terminal state the store records. The status write goes through
        # update_plan_task_status, which emits on the process-wide bridge bus rather than
        # through this run's emit_fn -- so the store is the authority here, as it is for the UI.
        self.assertEqual(get_store().get_dag("PLAN").nodes[task_id].status, "completed")
        self.assertEqual(events[-1]["type"], "workflow_complete")
        self.assertEqual(events[-1]["status"], "finished")
        # Nothing was classified: no Gatekeeper evaluation line appears.
        logs = "\n".join(str(e.get("text", "")) for e in events if e["type"] == "log")
        self.assertNotIn("GATEKEEPER", logs)

    def test_the_approval_pass_refuses_when_no_artifact_is_stored(self):
        """An approval with no artifact is reported and failed, never a silent no-op."""
        from orchestration.workflow import planner

        self._seed_scaffold()
        node = planner.spawn_ephemeral_task(plan_id="PLAN", title="No plan", files=["main.py"])
        ft.update_plan_task_status(node["id"], "in_progress")

        events = self._collect("execute_artifact", "", {"taskId": node["id"], "planId": "PLAN"})

        self.assertFalse(os.path.exists(os.path.join(self.tmp, "main.py")))
        logs = "\n".join(str(e.get("text", "")) for e in events if e["type"] == "log")
        self.assertIn("No approved artifact", logs)
        # The refusal is recorded on the task, not only narrated.
        from storage.db import get_store

        self.assertEqual(get_store().get_dag("PLAN").nodes[node["id"]].status, "failed")
        self.assertEqual(events[-1]["type"], "workflow_complete")

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
        self._approve("task-2")
        events = self._collect("next_step", "[ACTION: EXECUTE_NEXT_STEP]")

        first = {}
        for e in events:
            first.setdefault(e["type"], e)

        self.assertEqual(list(first["workflow_started"]), ["type", "plan_file", "action_type"])
        self.assertEqual(list(first["architect_spawn"]), ["type", "agent", "name", "model"])
        self.assertEqual(list(first["log"]), ["type", "agent", "log_type", "text"])
        self.assertEqual(
            list(first["delegation"]),
            ["type", "from_agent", "target_agent", "target_name", "task"],
        )
        self.assertEqual(list(first["coder_spawn"]), ["type", "agent", "name", "model"])
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
        "set_active_plan_filename", "list_plan_files",
        "walk_workspace", "IGNORE_DIRS", "PROJECT_DIR", "ACTIVE_PLAN_FILE",
        "parse_markdown_to_plan_dict", "compile_plan_json_to_markdown",
        "parse_plan_tree", "load_plan_state", "save_plan_state",
        "sync_plan_on_disk", "update_plan_task_status", "read_source", "overwrite_source",
        "list_workspace_files", "read_preview_source",
        "get_backup_dir", "backup_file_for_task",
        "audit_codebase_plan_sync", "resolve_sync_plan_to_codebase",
        "resolve_sync_code_to_plan", "rollback_task_state",
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
        """The façade re-exports the engine primitives, and no model tool catalog.

        The ``@tool`` wrappers that used to be asserted here are MCP tools now, bound per run
        from a live server -- so there is no static list to be identical to, and asserting one
        would be asserting the architecture this change removed.
        """
        namespace = {}
        exec("from tools.file_tools import *", namespace)
        self.assertIs(namespace["overwrite_source"], ft.overwrite_source)
        self.assertIs(namespace["read_source"], ft.read_source)
        self.assertNotIn("all_file_tools", namespace)
        self.assertNotIn("write_file", namespace)


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

        self.tmp = tempfile.mkdtemp(prefix="aleth_prompttest_")
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
        self.coders_mod.coder_deep.system_prompt = self._orig["deep_system"]
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
            self.coders_mod.coder_deep.system_prompt,
            self.coders_mod.CODER_DEEP_SYSTEM_PROMPT,
        )
        self.assertIn(
            self.coders_mod.CODER_DEEP_CORE_PROMPT + "\n\n### Custom Developer Directives:\nPrefer dataclasses.",
            self.coders_mod.coder_deep.system_prompt,
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

    def _resolve_from(self, cwd, env=None):
        result = subprocess.run(
            [sys.executable, "-c", self._RESOLVE_SCRIPT, self.project_root],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        return result.stdout.strip()

    def test_the_workspace_comes_from_the_environment(self):
        """``ALETH_WORKSPACE_DIR`` is the single source, so the host decides where it lives.

        The workspace is a path *in the filesystem the Docker daemon sees* -- a container
        bind-mounts it -- so it cannot be hardcoded to a directory beside the repository.
        """
        target = tempfile.mkdtemp(prefix="aleth_ws_env_")
        self.addCleanup(shutil.rmtree, target, ignore_errors=True)
        env = {**os.environ, "ALETH_WORKSPACE_DIR": target}

        self.assertEqual(
            self._resolve_from(self.project_root, env=env), os.path.abspath(target)
        )

    def test_the_default_workspace_is_absolute_and_cwd_independent(self):
        """With no environment, the default is expanded and absolute -- cwd cannot move it.

        Anchoring matters because the app can be launched from a shortcut or an IDE run
        configuration; a cwd-relative default would silently point at an empty directory and
        leave the UI with no plan to render.
        """
        env = {k: v for k, v in os.environ.items() if k != "ALETH_WORKSPACE_DIR"}
        resolved = set()
        for cwd in (
            self.project_root,
            os.path.join(self.project_root, "ui"),
            os.path.dirname(self.project_root),
        ):
            if not os.path.isdir(cwd):
                continue
            value = self._resolve_from(cwd, env=env)
            self.assertTrue(os.path.isabs(value), value)
            resolved.add(value)
        self.assertEqual(len(resolved), 1, f"the default moved with the cwd: {resolved}")

    def test_a_foreign_cwd_does_not_create_a_workspace_beside_it(self):
        foreign = tempfile.mkdtemp(prefix="aleth_foreigncwd_")
        try:
            resolved = self._resolve_from(foreign)
            self.assertFalse(
                os.path.abspath(resolved).startswith(os.path.abspath(foreign)),
                f"a workspace was created beside the cwd: {resolved}",
            )
        finally:
            shutil.rmtree(foreign, ignore_errors=True)


class AddPlanTaskBridgeTests(WorkspaceTestCase):
    """BridgeAPI.add_plan_task -- the one-click "Add to Plan" write.

    The result view calls this directly rather than routing a proposal through the Update
    Plan drawer, so the contract is the ordinary one: a real pending task appended to the
    active plan, persisted, and announced with a single ``plan_updated``.
    """

    def setUp(self):
        super().setUp()
        try:
            import app
        except Exception as exc:  # pragma: no cover - only when credentials are absent
            raise unittest.SkipTest(f"app needs credentials: {exc}")
        self.app = app

    def _api(self):
        api = self.app.BridgeAPI()
        api._events = []
        api.emit_event = api._events.append
        return api

    def test_adds_a_pending_task_and_announces_it_once(self):
        self.save_state(ft.parse_markdown_to_plan_dict(PLAN_MD, "PLAN.md"))
        api = self._api()

        res = api.add_plan_task("Dashboard view")

        self.assertTrue(res["success"])
        self.assertEqual(res["filename"], "PLAN.md")
        self.assertEqual(res["task_id"], "task-6")
        self.assertEqual(res["tree"][-1]["title"], "Dashboard view")
        self.assertEqual(res["tree"][-1]["status"], "pending")
        self.assertEqual([e["type"] for e in api._events], ["plan_updated"])
        # The write is real: a reload from disk sees the task.
        self.assertEqual(ft.load_plan_state()["steps"][-1]["title"], "Dashboard view")

    def test_an_empty_title_fails_and_writes_nothing(self):
        self.save_state(ft.parse_markdown_to_plan_dict(PLAN_MD, "PLAN.md"))
        before = self.read_state()
        api = self._api()

        res = api.add_plan_task("   ")

        self.assertFalse(res["success"])
        self.assertNotIn("task_id", res)
        self.assertEqual(api._events, [])
        after = self.read_state()
        self.assertEqual(len(after["steps"]), len(before["steps"]))


class RevertPlanUpdateBridgeTests(WorkspaceTestCase):
    """BridgeAPI.revert_plan_update -- the one-click undo of a saved plan revision.

    Update Plan auto-commits, so the result view's revert control is the only way back to the
    previous roadmap. This is the endpoint behind it: restore the snapshot taken immediately
    before the revision, then announce it with the usual ``plan_updated``.
    """

    def setUp(self):
        super().setUp()
        try:
            import app
        except Exception as exc:  # pragma: no cover - only when credentials are absent
            raise unittest.SkipTest(f"app needs credentials: {exc}")
        self.app = app

    def _api(self):
        api = self.app.BridgeAPI()
        api._events = []
        api.emit_event = api._events.append
        return api

    def test_reverts_to_the_captured_revision_and_announces_it(self):
        self.save_state(ft.parse_markdown_to_plan_dict(PLAN_MD, "PLAN.md"))
        ft.snapshot_plan_revision()
        # A revision lands through the same one-click endpoint the UI uses.
        self._api().add_plan_task("A later addition")

        api = self._api()
        res = api.revert_plan_update()

        self.assertTrue(res["success"])
        self.assertEqual(res["filename"], "PLAN.md")
        titles = [t["title"] for t in res["tree"]]
        self.assertNotIn("A later addition", titles)
        self.assertEqual([e["type"] for e in api._events], ["plan_updated"])
        self.assertEqual([t["title"] for t in self.read_state()["steps"]], titles)

    def test_revert_without_a_captured_revision_fails_and_emits_nothing(self):
        self.save_state(ft.parse_markdown_to_plan_dict(PLAN_MD, "PLAN.md"))
        api = self._api()

        res = api.revert_plan_update()

        self.assertFalse(res["success"])
        self.assertEqual(api._events, [])


class WorkflowWriteGuardTests(WorkspaceTestCase):
    """_write_checked writes through the engine writer and makes a failure loud.

    The model's ``write_file`` tool refuses to overwrite (the chokehold), so the workflow
    publishes its own deliverables through ``overwrite_source``; a failure there raises, so
    the runner aborts the action instead of narrating a write that never happened (H1).
    """

    def test_a_failed_write_raises(self):
        from orchestration.workflow import actions_impl

        with mock.patch(
            "orchestration.workflow.actions_impl.overwrite_source",
            side_effect=OSError("permission denied"),
        ):
            with self.assertRaises(RuntimeError):
                actions_impl._write_checked("coder-standard", "x.py", "code")

    def test_a_successful_write_returns_the_writer_message(self):
        from orchestration.workflow import actions_impl

        message = actions_impl._write_checked("coder-standard", "ok.py", "x = 1\n")
        self.assertTrue(message.startswith("Successfully wrote"))
        self.assertEqual(self.read("ok.py"), "x = 1\n")


class StopExecutionBridgeTests(WorkspaceTestCase):
    """stop_execution's terminal event must carry a status.

    With no run live the endpoint emits ``workflow_stopped`` itself; ``finalizeWorkflow``
    branches on ``status``, so omitting it made a halt read as a clean finish (audit H4).
    """

    def setUp(self):
        super().setUp()
        try:
            import app
        except Exception as exc:  # pragma: no cover - only when credentials are absent
            raise unittest.SkipTest(f"app needs credentials: {exc}")
        self.app = app

    def test_a_stray_stop_reports_the_stopped_status(self):
        import registry as registry_module

        registry_module.registry.stop_event.clear()
        self.addCleanup(registry_module.registry.stop_event.clear)
        api = self.app.BridgeAPI()
        api._events = []
        api.emit_event = api._events.append

        # No run is live, so the endpoint emits the terminal event itself.
        res = api.stop_execution()

        self.assertEqual(res["status"], "stopped")
        self.assertEqual([e["type"] for e in api._events], ["workflow_stopped"])
        self.assertEqual(api._events[0]["status"], "stopped")


class RunLockBridgeTests(WorkspaceTestCase):
    """The run lock is server-authoritative (audit H5/H6/H10).

    The client lock can be defeated by a reload, a retry or the normalize path; these pin the
    backend refusals that actually stop two runs interleaving their plan writes.
    """

    def setUp(self):
        super().setUp()
        try:
            import app
        except Exception as exc:  # pragma: no cover - only when credentials are absent
            raise unittest.SkipTest(f"app needs credentials: {exc}")
        self.app = app

    def _api_with_live_run(self):
        """A BridgeAPI whose "run" is a thread that stays alive until cleanup."""
        import threading

        api = self.app.BridgeAPI()
        stop = threading.Event()
        thread = threading.Thread(target=stop.wait, daemon=True)
        thread.start()
        self.addCleanup(stop.set)
        api._execution_thread = thread
        return api

    def test_get_run_state_is_idle_with_no_thread(self):
        self.assertFalse(self.app.BridgeAPI().get_run_state()["running"])

    def test_get_run_state_reports_a_live_run(self):
        self.assertTrue(self._api_with_live_run().get_run_state()["running"])

    def test_a_second_start_is_refused_while_a_run_is_live(self):
        res = self._api_with_live_run().start_execution("do something else")
        self.assertFalse(res["success"])
        self.assertIn("already in progress", res["error"])

    def test_switching_plans_is_refused_while_a_run_is_live(self):
        res = self._api_with_live_run().set_active_plan("OTHER.md")
        self.assertFalse(res["success"])
        self.assertIn("run is in progress", res["error"])


class ApproveArtifactBridgeTests(WorkspaceTestCase):
    """Approval releases execution, and the dispatch is deterministic.

    The plan half returns and its thread ends, so without a dispatch on approval an approved
    artifact would never reach disk. The dispatch is a dedicated pass
    (``orchestration.workflow.execution``) that applies the stored artifact directly: it
    never re-issues the originating request, because re-entering an action branch would
    re-run System 1 (the Gatekeeper router) and System 2 (the planner). These pin the
    dispatch and its refusal while a run is live.
    """

    def setUp(self):
        super().setUp()
        try:
            import app
        except Exception as exc:  # pragma: no cover - only when credentials are absent
            raise unittest.SkipTest(f"app needs credentials: {exc}")
        self.app = app
        self._apis = []

    def tearDown(self):
        # Drain and close every swarm the bridge built *before* the workspace is unlinked. The
        # swarm's worker callbacks reach the database from the pool's own thread, so unlinking the
        # temporary directory first leaves them running against a file that is gone -- the
        # unhandled-traceback race this drain removes.
        for api in self._apis:
            api.shutdown_swarm()
        super().tearDown()

    def _api(self):
        """A bridge whose swarm is guaranteed to be shut down before the workspace goes away."""
        api = self.app.BridgeAPI()
        self._apis.append(api)
        return api

    def _planned_node(self):
        """A plan with one ad-hoc task halted in ``planned``, with its artifact stored."""
        import registry as registry_module
        from orchestration.workflow import planner
        from tools import execution_gate

        registry_module.registry.create_new_plan_file("PLAN.md", "Demo roadmap")
        node = planner.spawn_ephemeral_task(plan_id="PLAN", title="Ad-hoc work", files=["main.py"])
        execution_gate.plan_artifact(
            plan_id="PLAN",
            task_id=node["id"],
            summary="Ad-hoc work",
            ast_targets=[planner.ASTTarget(file_path="main.py", operation="insert", content="x = 1\n")],
        )
        return node["id"]

    def test_approving_dispatches_the_deterministic_executor(self):
        import registry as registry_module

        task_id = self._planned_node()
        api = self._api()

        with mock.patch.object(registry_module.registry, "run_agent_workflow") as runner:
            res = api.approve_artifact(task_id, "PLAN")
            if api._execution_thread is not None:
                api._execution_thread.join(timeout=5)

        self.assertTrue(res["success"], res)
        self.assertTrue(res["dispatched"])
        self.assertTrue(runner.called)
        _args, kwargs = runner.call_args
        # The dedicated execution pass -- not the request that produced the plan, which would
        # re-run System 1 and System 2 on the way to the executor.
        self.assertEqual(kwargs["action_type"], "execute_artifact")
        self.assertEqual(kwargs["action_params"]["taskId"], task_id)
        self.assertEqual(kwargs["action_params"]["planId"], "PLAN")

    def test_approval_does_not_dispatch_while_a_run_is_live(self):
        import threading

        import registry as registry_module

        task_id = self._planned_node()
        api = self._api()
        stop = threading.Event()
        thread = threading.Thread(target=stop.wait, daemon=True)
        thread.start()
        self.addCleanup(stop.set)
        api._execution_thread = thread

        with mock.patch.object(registry_module.registry, "run_agent_workflow") as runner:
            res = api.approve_artifact(task_id, "PLAN")

        self.assertTrue(res["success"])
        self.assertFalse(res["dispatched"])
        self.assertFalse(runner.called)

    def test_approving_a_non_planned_task_is_refused_without_dispatch(self):
        import registry as registry_module
        from orchestration.workflow import planner

        registry_module.registry.create_new_plan_file("PLAN.md", "Demo roadmap")
        node = planner.spawn_ephemeral_task(plan_id="PLAN", title="Not planned yet", files=["main.py"])
        api = self._api()

        with mock.patch.object(registry_module.registry, "run_agent_workflow") as runner:
            res = api.approve_artifact(node["id"], "PLAN")

        self.assertFalse(res["success"])
        self.assertIn("not 'planned'", res["error"])
        self.assertFalse(runner.called)


class AddTaskDependencyBridgeTests(WorkspaceTestCase):
    """Topology is structure, not prose.

    A blocker edge comes from a JSON ``dependencies`` array or from the explicit
    ``add_task_dependency`` IPC -- never from parsing the markdown, which is a read-only
    projection. These pin both sources and the store write behind the second.
    """

    def setUp(self):
        super().setUp()
        try:
            import app
        except Exception as exc:  # pragma: no cover - only when credentials are absent
            raise unittest.SkipTest(f"app needs credentials: {exc}")
        self.app = app

    def _seed(self):
        import registry as registry_module

        registry_module.registry.create_new_plan_file("PLAN.md", "Demo roadmap")

    def test_the_scaffold_declares_dependencies_as_structure(self):
        from storage.db import get_store

        self._seed()
        nodes = get_store().get_dag("PLAN").nodes

        self.assertEqual(nodes["task-1"].dependencies, [])
        self.assertEqual(nodes["task-2"].dependencies, ["task-1"])
        # A shared blocker: task-4 and task-5 are concurrent, not sequential.
        self.assertEqual(nodes["task-4"].dependencies, ["task-3"])
        self.assertEqual(nodes["task-5"].dependencies, ["task-3"])
        self.assertEqual(nodes["task-6"].dependencies, ["task-4", "task-5"])

    def test_the_projection_carries_the_dependencies_to_the_ui(self):
        import registry as registry_module

        self._seed()
        plan = registry_module.registry.get_current_plan_data()
        by_id = {task["id"]: task for task in plan["plan_json"]["steps"]}

        self.assertEqual(by_id["task-6"]["dependencies"], ["task-4", "task-5"])

    def test_the_ipc_adds_an_edge_to_the_store(self):
        from storage.db import get_store

        self._seed()
        res = self.app.BridgeAPI().add_task_dependency("task-1", "task-8", "PLAN")

        self.assertTrue(res["success"], res)
        self.assertIn("task-1", get_store().get_dag("PLAN").nodes["task-8"].dependencies)
        # The edge is on the node, so a later status write cannot drop it (save_dag rebuilds
        # the edge table from the nodes).
        get_store().update_task_status("PLAN", "task-8", "in_progress")
        self.assertIn("task-1", get_store().get_dag("PLAN").nodes["task-8"].dependencies)

    def test_the_ipc_refuses_an_unknown_node_or_a_self_edge(self):
        self._seed()
        api = self.app.BridgeAPI()

        self.assertFalse(api.add_task_dependency("task-1", "ghost", "PLAN")["success"])
        self.assertFalse(api.add_task_dependency("ghost", "task-1", "PLAN")["success"])
        self.assertFalse(api.add_task_dependency("task-1", "task-1", "PLAN")["success"])


class AmendArtifactBridgeTests(WorkspaceTestCase):
    """A planned artifact is editable, because a plan is a proposal.

    A hallucinated character should cost one edit, not a rejected run, wasted tokens and a
    re-plan. The amendment is refused once the task is approved: changing an ``in_progress``
    artifact would race the execution pass that is reading it.
    """

    def setUp(self):
        super().setUp()
        try:
            import app
        except Exception as exc:  # pragma: no cover - only when credentials are absent
            raise unittest.SkipTest(f"app needs credentials: {exc}")
        self.app = app

    def _planned_node(self):
        import registry as registry_module
        from orchestration.workflow import planner
        from tools import execution_gate

        registry_module.registry.create_new_plan_file("PLAN.md", "Demo roadmap")
        node = planner.spawn_ephemeral_task(plan_id="PLAN", title="Ad-hoc work", files=["main.py"])
        execution_gate.plan_artifact(
            plan_id="PLAN",
            task_id=node["id"],
            summary="Ad-hoc work",
            ast_targets=[planner.ASTTarget(file_path="main.py", operation="insert", content="x = 1\n")],
        )
        return node["id"]

    def test_it_rewrites_the_stored_target_content(self):
        from storage.db import get_store

        task_id = self._planned_node()

        res = self.app.BridgeAPI().update_artifact_target(task_id, 0, "x = 2\n", "PLAN")

        self.assertTrue(res["success"], res)
        stored = get_store().get_artifact("PLAN", task_id)
        self.assertEqual(stored["ast_targets"][0]["content"], "x = 2\n")

    def test_it_refuses_an_out_of_range_index(self):
        task_id = self._planned_node()

        res = self.app.BridgeAPI().update_artifact_target(task_id, 5, "x = 2\n", "PLAN")

        self.assertFalse(res["success"])
        self.assertIn("out of range", res["error"])

    def test_it_refuses_once_the_task_is_approved(self):
        from storage.db import get_store

        task_id = self._planned_node()
        get_store().update_task_status("PLAN", task_id, "in_progress")

        res = self.app.BridgeAPI().update_artifact_target(task_id, 0, "x = 2\n", "PLAN")

        self.assertFalse(res["success"])
        self.assertIn("only a 'planned' artifact", res["error"])


class SourceSpanBridgeTests(WorkspaceTestCase):
    """The diff surface reads one AST span, never a whole file (Phase 8).

    ``get_source_span`` is span-scoped on purpose: a whole-file read would make a whole-file
    diff possible, which is exactly what the artifact surface exists to prevent. The span is
    clamped to the file rather than refused, so a plan recorded against a file that has since
    shrunk still renders, and a path outside the workspace is refused outright.
    """

    def setUp(self):
        super().setUp()
        try:
            import app
        except Exception as exc:  # pragma: no cover - only when credentials are absent
            raise unittest.SkipTest(f"app needs credentials: {exc}")
        self.app = app

    def test_it_returns_the_bytes_at_the_recorded_span(self):
        ft.overwrite_source("main.py", "abcdef\n")

        res = self.app.BridgeAPI().get_source_span("main.py", 1, 4)

        self.assertTrue(res["success"], res)
        self.assertTrue(res["found"])
        self.assertEqual(res["text"], "bcd")
        # The span is read as raw bytes, which is the unit an AST target's byte_range is in
        # -- so the length is the file's byte count, not its character count (a Windows
        # text write expands the trailing newline).
        with open(os.path.join(self.tmp, "main.py"), "rb") as handle:
            self.assertEqual(res["length"], len(handle.read()))
        self.assertEqual((res["start"], res["end"]), (1, 4))

    def test_a_missing_file_reports_no_bytes_rather_than_an_error(self):
        res = self.app.BridgeAPI().get_source_span("nope.py", 0, 10)

        self.assertTrue(res["success"])
        self.assertFalse(res["found"])
        self.assertEqual(res["text"], "")

    def test_a_span_past_the_end_is_clamped(self):
        ft.overwrite_source("main.py", "abc")

        res = self.app.BridgeAPI().get_source_span("main.py", 0, 999)

        self.assertEqual(res["text"], "abc")
        self.assertEqual(res["end"], 3)

    def test_it_refuses_a_path_outside_the_workspace(self):
        res = self.app.BridgeAPI().get_source_span("../../secret.txt", 0, 5)

        self.assertFalse(res["success"])
        self.assertIn("Path traversal denied", res["error"])

    def test_it_refuses_a_backslash_traversal_and_an_absolute_path(self):
        candidates = ["/etc/passwd"]
        if sys.platform == "win32":
            # A backslash only traverses on Windows; on POSIX it is an ordinary filename
            # character, so a name containing one cannot leave the workspace.
            candidates.append("..\\..\\secret.txt")
        for candidate in candidates:
            res = self.app.BridgeAPI().get_source_span(candidate, 0, 5)
            self.assertFalse(res["success"], candidate)
            self.assertIn("Path traversal denied", res["error"])


class ValidatePlanStructureBridgeTests(WorkspaceTestCase):
    """A structure check that crashed must not read as 'structured' (audit M5).

    The Normalization Gate is advisory, so a broken check must not block a switch -- but it also
    must not present itself as a clean verdict. `checked` tells the two apart.
    """

    def setUp(self):
        super().setUp()
        try:
            import app
        except Exception as exc:  # pragma: no cover - only when credentials are absent
            raise unittest.SkipTest(f"app needs credentials: {exc}")
        self.app = app

    def test_a_crashed_check_is_marked_not_checked(self):
        api = self.app.BridgeAPI()
        with mock.patch("app.plan_structure_report", side_effect=RuntimeError("boom")):
            report = api.validate_plan_structure()
        self.assertTrue(report["structured"])
        self.assertFalse(report["checked"])

    def test_a_produced_verdict_is_marked_checked(self):
        report = self.app.BridgeAPI().validate_plan_structure()
        self.assertTrue(report["checked"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
