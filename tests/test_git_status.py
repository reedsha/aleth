"""Tests for ``tools.git_status`` -- the sidebar tree's change letters.

The module has two sources and one vocabulary. The snapshot source is pure filesystem and is
tested directly; the git source is tested against a real throwaway repository, and skipped
when git is not installed rather than failing.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

from tools import git_status


def _write_meta(base_dir, task_id, meta):
    task_dir = os.path.join(base_dir, git_status.BACKUP_SUBDIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    with open(os.path.join(task_dir, "_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)


class BackupStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aleth_gitstatus_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_modified_and_created_map_to_m_and_u(self):
        _write_meta(self.tmp, "task-1", {
            "main.py": {"action": "modified", "backup": "x"},
            "new_file.py": {"action": "created", "backup": None},
        })
        status = git_status._backup_status(self.tmp)
        self.assertEqual(status["main.py"], "M")
        self.assertEqual(status["new_file.py"], "U")

    def test_created_then_modified_reads_as_modified(self):
        _write_meta(self.tmp, "task-1", {"a.py": {"action": "created"}})
        _write_meta(self.tmp, "task-2", {"a.py": {"action": "modified"}})
        self.assertEqual(git_status._backup_status(self.tmp)["a.py"], "M")

    def test_no_backups_is_empty(self):
        self.assertEqual(git_status._backup_status(self.tmp), {})

    def test_unreadable_metadata_is_skipped_not_raised(self):
        task_dir = os.path.join(self.tmp, git_status.BACKUP_SUBDIR, "task-1")
        os.makedirs(task_dir, exist_ok=True)
        with open(os.path.join(task_dir, "_meta.json"), "w", encoding="utf-8") as f:
            f.write("{ not json")
        _write_meta(self.tmp, "task-2", {"ok.py": {"action": "modified"}})
        # The broken file is skipped; the good one still answers.
        self.assertEqual(git_status._backup_status(self.tmp), {"ok.py": "M"})


class SourceSelectionTests(unittest.TestCase):
    """`None` from git means "ask something else"; `{}` means "a repo with nothing to show"."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aleth_gitstatus_")
        _write_meta(self.tmp, "task-1", {"from_backup.py": {"action": "modified"}})
        self._real = git_status._git_status

    def tearDown(self):
        git_status._git_status = self._real
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_not_a_repo_falls_back_to_snapshots(self):
        git_status._git_status = lambda _base: None
        self.assertEqual(git_status.workspace_vcs_status(self.tmp), {"from_backup.py": "M"})

    def test_clean_repo_reports_nothing_rather_than_falling_back(self):
        git_status._git_status = lambda _base: {}
        self.assertEqual(git_status.workspace_vcs_status(self.tmp), {})

    def test_git_wins_when_it_applies(self):
        git_status._git_status = lambda _base: {"git.py": "U"}
        self.assertEqual(git_status.workspace_vcs_status(self.tmp), {"git.py": "U"})


@unittest.skipIf(shutil.which("git") is None, "git is not installed")
class RealRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aleth_gitstatus_")
        self._run("init", "-q")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args):
        return subprocess.run(
            ["git", "-C", self.tmp, *args],
            capture_output=True, text=True, timeout=20,
        )

    def _write(self, name, text):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_untracked_file_reads_as_new(self):
        self._write("fresh.py", "x = 1\n")
        self.assertEqual(git_status.workspace_vcs_status(self.tmp).get("fresh.py"), "U")

    def test_a_folder_inside_a_repo_is_not_its_own_work_tree(self):
        nested = os.path.join(self.tmp, "nested")
        os.makedirs(nested, exist_ok=True)
        self._write(os.path.join("nested", "inner.py"), "x = 1\n")
        # The parent repo reports `nested/inner.py`, which is not the workspace-relative
        # `inner.py`: git must decline rather than hand back paths the tree cannot match.
        self.assertIsNone(git_status._git_status(nested))
        self.assertEqual(git_status.workspace_vcs_status(nested), {})

    def test_modified_tracked_file_reads_as_changed(self):
        self._write("tracked.py", "x = 1\n")
        self._run("add", "tracked.py")
        self._run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        self._write("tracked.py", "x = 2\n")
        self.assertEqual(git_status.workspace_vcs_status(self.tmp).get("tracked.py"), "M")


if __name__ == "__main__":
    unittest.main()
