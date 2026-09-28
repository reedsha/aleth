"""Tests for ``tools.test_runner`` -- the real pass/fail verdict behind the result view.

The verdict was the one cell the polymorphic result view could not fill: the Coder writes a
test file, but nothing ran it, so the strip could only say "not run". These tests hold the
runner to the two things that make it trustworthy -- it finds the right test file, and it
classifies what happened without ever turning "could not run" into a pass.

The execution tests spawn a real pytest against a throwaway workspace, so they exercise the
same path the bridge does, including the read-only guarantee.
"""

import json
import os
import shutil
import tempfile
import unittest

from tools import test_runner


PASSING_TEST = "def test_ok():\n    assert 1 + 1 == 2\n"
FAILING_TEST = "def test_bad():\n    assert 1 + 1 == 3\n"


def _write_meta(base_dir, task_id, meta):
    task_dir = os.path.join(base_dir, test_runner.BACKUP_SUBDIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    with open(os.path.join(task_dir, "_meta.json"), "w", encoding="utf-8") as handle:
        json.dump(meta, handle)


def _write_file(base_dir, name, text):
    path = os.path.join(base_dir, name)
    os.makedirs(os.path.dirname(path) or base_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class ClassificationTests(unittest.TestCase):
    """The pure parts: the summary parser and the worst-answer-wins aggregation."""

    def test_counts_read_the_pytest_summary_line(self):
        self.assertEqual(
            test_runner._counts("2 passed, 1 failed, 1 error in 0.03s"),
            {"passed": 2, "failed": 1, "errors": 1, "skipped": 0},
        )

    def test_counts_plural_error_is_read(self):
        self.assertEqual(test_runner._counts("2 errors in 0.01s")["errors"], 2)

    def test_counts_are_zero_without_a_summary(self):
        self.assertEqual(
            test_runner._counts("no summary here"),
            {"passed": 0, "failed": 0, "errors": 0, "skipped": 0},
        )

    def test_a_failure_outranks_a_pass(self):
        self.assertEqual(test_runner._aggregate_verdict(["passed", "failed"]), "failed")

    def test_no_verdicts_is_none(self):
        self.assertEqual(test_runner._aggregate_verdict([]), "none")


class WorkspaceTests(unittest.TestCase):
    """Finding the test file, and running it, in a throwaway workspace."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="deepagents_testrunner_")
        self._real = test_runner.get_project_dir
        test_runner.get_project_dir = lambda: self.tmp

    def tearDown(self):
        test_runner.get_project_dir = self._real
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_metadata_reports_no_tests(self):
        res = test_runner.run_task_tests("task-1")
        self.assertTrue(res["success"])
        self.assertFalse(res["found"])
        self.assertFalse(res["ran"])
        self.assertEqual(res["verdict"], "none")

    def test_empty_task_id_is_rejected(self):
        res = test_runner.run_task_tests("   ")
        self.assertFalse(res["success"])

    def test_a_passing_test_reports_a_green_verdict(self):
        _write_meta(self.tmp, "task-1", {"main.py": {"action": "created"}, "test_main.py": {"action": "created"}})
        _write_file(self.tmp, "test_main.py", PASSING_TEST)
        res = test_runner.run_task_tests("task-1")
        self.assertTrue(res["ran"])
        self.assertEqual(res["verdict"], "passed")
        self.assertEqual(res["totals"]["passed"], 1)
        self.assertEqual(res["summary"], "1 passed")

    def test_a_failing_test_reports_a_red_verdict(self):
        _write_meta(self.tmp, "task-1", {"main.py": {"action": "created"}, "test_main.py": {"action": "created"}})
        _write_file(self.tmp, "test_main.py", FAILING_TEST)
        res = test_runner.run_task_tests("task-1")
        self.assertEqual(res["verdict"], "failed")
        self.assertEqual(res["totals"]["failed"], 1)

    def test_the_paired_test_is_derived_when_only_the_source_was_recorded(self):
        # fix_bug snapshots only the patched file; the test sits beside it by the same rule
        # the Coder writes with, so the runner has to derive and use it.
        _write_meta(self.tmp, "bugfix", {"main.py": {"action": "modified"}})
        _write_file(self.tmp, "test_main.py", PASSING_TEST)
        self.assertEqual(test_runner.test_files_for_task("bugfix"), ["test_main.py"])
        self.assertEqual(test_runner.run_task_tests("bugfix")["verdict"], "passed")

    def test_a_recorded_test_that_is_gone_is_reported_not_silently_skipped(self):
        _write_meta(self.tmp, "task-1", {"test_main.py": {"action": "created"}})
        res = test_runner.run_task_tests("task-1")
        self.assertTrue(res["found"])
        self.assertFalse(res["ran"])
        self.assertEqual(res["verdict"], "missing")

    def test_opening_the_result_view_does_not_litter_the_workspace(self):
        _write_meta(self.tmp, "task-1", {"test_main.py": {"action": "created"}})
        _write_file(self.tmp, "test_main.py", PASSING_TEST)
        test_runner.run_task_tests("task-1")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, ".pytest_cache")))
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "__pycache__")))


if __name__ == "__main__":
    unittest.main()
