"""Phase 20: the shadow workspace, the delta, and the merge boundary.

These tests hold the file-system boundary to the claim that makes it worth having: an agent can
write anything it likes and the user's live tree is byte-identical afterwards, until a person
merges the delta. The unit tests exercise ``tools.staging`` directly; the service tests drive
the same machinery through ``EngineService`` -- the execution root the run loop sets, the
review read, and the gate that refuses to merge a plan that is not finished.
"""

import ast
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from tools import env_sanitizer, execution_io, staging, workspace
from tools.file_tools import list_workspace_files

FAKE_DOCKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_docker.py")


def fake_docker_env(**extra):
    """The environment that points the perimeter at the in-repo double."""
    env = {"ALETH_DOCKER_BIN": f"{sys.executable} {FAKE_DOCKER}"}
    env.update(extra)
    return env


def _write(root: str, name: str, text: str) -> None:
    """Write exactly these bytes: no newline translation, so a fixture is platform-independent."""
    path = os.path.join(root, name)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _read(root: str, name: str) -> str:
    with open(os.path.join(root, name), "r", encoding="utf-8") as handle:
        return handle.read()


class StagingModuleTests(unittest.TestCase):
    """``tools.staging`` is a filesystem transaction: copy, diff, merge, purge."""

    def setUp(self):
        self.host = tempfile.mkdtemp(prefix="aleth_staging_host_")
        self.addCleanup(shutil.rmtree, self.host, ignore_errors=True)
        # A clean slate for the plan-keyed lookups: a staging left by an earlier test would
        # otherwise answer a query this test did not make.
        staging.remove_tree(staging.staging_base())
        self.addCleanup(shutil.rmtree, staging.staging_base(), ignore_errors=True)

    def _host_with_files(self):
        _write(self.host, "pkg/mod.py", "value = 1\n")
        _write(self.host, "README.md", "# hi\n")

    def test_create_staging_copies_the_workspace_and_leaves_the_host_untouched(self):
        self._host_with_files()
        shadow = staging.create_staging(self.host, intent_id="i-1", plan_id="PLAN")
        self.assertTrue(shadow.path.startswith(staging.staging_base()))
        self.assertEqual(_read(shadow.path, "pkg/mod.py"), "value = 1\n")
        self.assertTrue(shadow.intent_id == "i-1")

        # A write in the shadow does not reach the host.
        _write(shadow.path, "pkg/mod.py", "value = 2\n")
        _write(shadow.path, "new.py", "fresh = True\n")
        self.assertEqual(_read(self.host, "pkg/mod.py"), "value = 1\n")
        self.assertFalse(os.path.exists(os.path.join(self.host, "new.py")))

    def test_create_staging_excludes_ignored_folders(self):
        self._host_with_files()
        _write(self.host, "node_modules/dep/index.js", "module.exports = 1\n")
        _write(self.host, "__pycache__/junk.pyc", "binary-ish\n")
        shadow = staging.create_staging(self.host, intent_id="i-ignored")
        self.assertFalse(os.path.exists(os.path.join(shadow.path, "node_modules")))
        self.assertFalse(os.path.exists(os.path.join(shadow.path, "__pycache__")))
        self.assertTrue(os.path.exists(os.path.join(shadow.path, "pkg", "mod.py")))

    def test_create_staging_refuses_a_missing_workspace(self):
        with self.assertRaises(staging.StagingError):
            staging.create_staging(os.path.join(self.host, "does-not-exist"))

    def test_diff_reports_added_modified_and_deleted(self):
        self._host_with_files()
        shadow = staging.create_staging(self.host, intent_id="i-diff")
        os.remove(os.path.join(shadow.path, "README.md"))
        _write(shadow.path, "pkg/mod.py", "value = 99\n")
        _write(shadow.path, "pkg/extra.py", "extra = True\n")

        delta = staging.compute_diff(shadow)
        self.assertEqual(delta["added"], ["pkg/extra.py"])
        self.assertEqual(delta["modified"], ["pkg/mod.py"])
        self.assertEqual(delta["deleted"], ["README.md"])
        self.assertFalse(delta["clean"])
        self.assertIn("-value = 1", delta["patch"])
        self.assertIn("+value = 99", delta["patch"])

    def test_diff_is_clean_when_nothing_changed(self):
        self._host_with_files()
        shadow = staging.create_staging(self.host, intent_id="i-clean")
        delta = staging.compute_diff(shadow)
        self.assertTrue(delta["clean"])
        self.assertEqual(delta["patch"], "")

    def test_merge_applies_the_delta_to_the_host(self):
        self._host_with_files()
        shadow = staging.create_staging(self.host, intent_id="i-merge")
        os.remove(os.path.join(shadow.path, "README.md"))
        _write(shadow.path, "pkg/mod.py", "value = 7\n")
        _write(shadow.path, "pkg/extra.py", "extra = True\n")

        result = staging.merge_staging(shadow)
        self.assertTrue(result["success"])
        self.assertEqual(result["applied"], {"added": 1, "modified": 1, "deleted": 1})
        self.assertEqual(_read(self.host, "pkg/mod.py"), "value = 7\n")
        self.assertEqual(_read(self.host, "pkg/extra.py"), "extra = True\n")
        self.assertFalse(os.path.exists(os.path.join(self.host, "README.md")))

    def test_purge_removes_the_shadow_and_leaves_the_host(self):
        self._host_with_files()
        shadow = staging.create_staging(self.host, intent_id="i-purge")
        _write(shadow.path, "pkg/mod.py", "value = 3\n")
        self.assertTrue(staging.purge_staging(shadow.staging_id))
        self.assertFalse(os.path.exists(shadow.path))
        self.assertEqual(_read(self.host, "pkg/mod.py"), "value = 1\n")
        # Idempotent: a second purge reports that there was nothing to remove.
        self.assertFalse(staging.purge_staging(shadow.staging_id))

    def test_find_staging_resolves_by_intent_and_by_id_never_by_plan(self):
        self._host_with_files()
        shadow = staging.create_staging(self.host, intent_id="i-find", plan_id="PLAN-X")
        self.assertEqual(staging.find_staging(intent_id="i-find").staging_id, shadow.staging_id)
        self.assertEqual(
            staging.find_staging(staging_id=shadow.staging_id).staging_id, shadow.staging_id
        )
        # The plan is metadata, never a key (Phase 21): a query that only matches this shadow's
        # *plan* resolves to nothing, and a blank query resolves to nothing either.
        self.assertIsNone(staging.find_staging(intent_id="never-created"))
        self.assertIsNone(staging.find_staging(intent_id="PLAN-X"))
        self.assertIsNone(staging.find_staging(intent_id=""))

    def test_load_staging_round_trips_the_manifest(self):
        self._host_with_files()
        shadow = staging.create_staging(self.host, intent_id="i-manifest", plan_id="PLAN-M")
        loaded = staging.load_staging(shadow.staging_id)
        self.assertEqual(loaded.intent_id, "i-manifest")
        self.assertEqual(loaded.plan_id, "PLAN-M")
        self.assertEqual(loaded.host_root, os.path.abspath(self.host))

    def test_the_manifest_never_appears_in_the_delta(self):
        self._host_with_files()
        shadow = staging.create_staging(self.host, intent_id="i-manifest-clean")
        self.assertEqual(staging.compute_diff(shadow)["added"], [])


def _git_init(root: str) -> None:
    """A real repository, initialised the way a person would, with the sanitized child env."""
    subprocess.run(
        ["git", "init", "-q"], cwd=root, check=True, capture_output=True,
        env=env_sanitizer.sanitized_environment(),
    )


class GitFilteredStagingTests(unittest.TestCase):
    """Phase 21: the shadow copy is the project git would keep, and nothing else.

    The ignore rules are not re-derived in Python -- they are whatever ``git ls-files`` answers,
    which is the only implementation that agrees with the user's own tooling.
    """

    def setUp(self):
        self.host = tempfile.mkdtemp(prefix="aleth_git_host_")
        self.addCleanup(shutil.rmtree, self.host, ignore_errors=True)
        staging.remove_tree(staging.staging_base())
        self.addCleanup(shutil.rmtree, staging.staging_base(), ignore_errors=True)

    def _repo(self):
        _git_init(self.host)
        _write(self.host, ".gitignore", "node_modules/\ndist/\n*.log\n")
        _write(self.host, "pkg/mod.py", "value = 1\n")
        _write(self.host, "build_artifact.log", "noise\n")
        _write(self.host, "node_modules/dep/index.js", "module.exports = 1\n")
        _write(self.host, "dist/bundle.js", "console.log(1)\n")
        _write(self.host, "untracked.py", "fresh = True\n")

    def test_the_copy_is_the_project_git_would_keep(self):
        self._repo()
        shadow = staging.create_staging(self.host, intent_id="i-git")
        self.assertTrue(os.path.exists(os.path.join(shadow.path, "pkg", "mod.py")))
        # Untracked but not ignored: still part of the project, so it is copied.
        self.assertTrue(os.path.exists(os.path.join(shadow.path, "untracked.py")))
        self.assertTrue(os.path.exists(os.path.join(shadow.path, ".gitignore")))
        # Ignored: the dead weight a naive copytree would have dragged in.
        self.assertFalse(os.path.exists(os.path.join(shadow.path, "node_modules")))
        self.assertFalse(os.path.exists(os.path.join(shadow.path, "dist")))
        self.assertFalse(os.path.exists(os.path.join(shadow.path, "build_artifact.log")))

    def test_an_ignored_host_file_is_not_reported_as_deleted(self):
        self._repo()
        shadow = staging.create_staging(self.host, intent_id="i-git-diff")
        delta = staging.compute_diff(shadow)
        # The shadow has no ``dist`` and no ``*.log``; the host does. Neither is a deletion,
        # because git already says they are not part of the project.
        self.assertEqual(delta["deleted"], [])
        self.assertTrue(delta["clean"], delta)

    def test_a_non_repository_workspace_still_stages(self):
        _write(self.host, "pkg/mod.py", "value = 1\n")
        _write(self.host, "node_modules/dep/index.js", "module.exports = 1\n")
        shadow = staging.create_staging(self.host, intent_id="i-nogit")
        self.assertTrue(os.path.exists(os.path.join(shadow.path, "pkg", "mod.py")))
        self.assertFalse(os.path.exists(os.path.join(shadow.path, "node_modules")))


class ExecutionRootTests(unittest.TestCase):
    """The process-wide root that selects the shadow during a run."""

    def setUp(self):
        self._original = workspace.get_project_dir()
        self.addCleanup(workspace.set_project_dir, self._original)
        self.addCleanup(workspace.set_execution_dir, None)
        self.addCleanup(shutil.rmtree, staging.staging_base(), ignore_errors=True)
        staging.remove_tree(staging.staging_base())

    def test_the_execution_root_defaults_to_the_project_and_follows_a_pointer(self):
        self.assertEqual(workspace.get_execution_dir(), workspace.get_project_dir())
        workspace.set_execution_dir("/tmp/aleth-shadow-example")
        self.assertEqual(workspace.get_execution_dir(), os.path.abspath("/tmp/aleth-shadow-example"))
        workspace.set_execution_dir(None)
        self.assertEqual(workspace.get_execution_dir(), workspace.get_project_dir())


class ShadowRunTests(unittest.TestCase):
    """``EngineService`` stages the run, exposes the delta, and gates the merge."""

    def setUp(self):
        from app import EngineService

        self.host = tempfile.mkdtemp(prefix="aleth_shadow_host_")
        self.addCleanup(shutil.rmtree, self.host, ignore_errors=True)
        _write(self.host, "pkg/mod.py", "value = 1\n")
        self._original_project = workspace.get_project_dir()
        workspace.set_project_dir(self.host)
        self.addCleanup(workspace.set_project_dir, self._original_project)
        self.addCleanup(workspace.set_execution_dir, None)
        staging.remove_tree(staging.staging_base())
        self.addCleanup(shutil.rmtree, staging.staging_base(), ignore_errors=True)
        self.service = EngineService()

    def test_staged_execution_sets_the_root_to_a_shadow_and_clears_it(self):
        with self.service._staged_execution("intent-staged") as shadow:
            self.assertIsNotNone(shadow)
            self.assertEqual(workspace.get_execution_dir(), shadow.path)
            self.assertEqual(_read(shadow.path, "pkg/mod.py"), "value = 1\n")
        self.assertEqual(workspace.get_execution_dir(), self.host)
        self.assertEqual(staging.find_staging(intent_id="intent-staged").staging_id, shadow.staging_id)

    def test_a_nested_staged_execution_reuses_the_active_shadow(self):
        with self.service._staged_execution("outer") as outer:
            with self.service._staged_execution("inner") as inner:
                self.assertIsNone(inner)
                self.assertEqual(workspace.get_execution_dir(), outer.path)
        self.assertEqual(workspace.get_execution_dir(), self.host)

    def test_workspace_diff_is_empty_without_a_shadow(self):
        result = self.service.workspace_diff(intent_id="nothing-staged")
        self.assertTrue(result["success"])
        self.assertFalse(result["staged"])
        self.assertEqual(result["counts"], {"added": 0, "modified": 0, "deleted": 0})

    def test_workspace_diff_reports_the_staged_delta(self):
        with self.service._staged_execution("intent-diff") as shadow:
            _write(shadow.path, "pkg/mod.py", "value = 42\n")
            _write(shadow.path, "pkg/new.py", "new = True\n")
        result = self.service.workspace_diff(intent_id="intent-diff")
        self.assertTrue(result["staged"])
        self.assertEqual(result["modified"], ["pkg/mod.py"])
        self.assertEqual(result["added"], ["pkg/new.py"])
        self.assertFalse(result["clean"])

    def test_workspace_merge_refuses_while_the_plan_is_unfinished(self):
        from orchestration import autonomy

        with self.service._staged_execution("intent-refused") as shadow:
            _write(shadow.path, "pkg/mod.py", "value = 5\n")
        # Verified, so the *plan* gate is the one under test rather than the verification gate.
        staging.record_verification(shadow, True)

        unfinished = autonomy.PlanProgress(total=2, completed=1, failed=0, planned=0,
                                            in_progress=0, pending=1, runnable=1)
        with mock.patch.object(autonomy, "plan_progress", return_value=unfinished):
            result = self.service.workspace_merge(intent_id="intent-refused")

        self.assertFalse(result["success"])
        self.assertIn("not complete", result["error"])
        # Refused means refused: the host is untouched and the shadow is still there to review.
        self.assertEqual(_read(self.host, "pkg/mod.py"), "value = 1\n")
        self.assertTrue(os.path.exists(shadow.path))

    def test_workspace_merge_applies_and_purges_when_the_plan_is_finished(self):
        from orchestration import autonomy

        with self.service._staged_execution("intent-merged") as shadow:
            _write(shadow.path, "pkg/mod.py", "value = 8\n")
            _write(shadow.path, "pkg/added.py", "added = True\n")
        staging.record_verification(shadow, True)

        finished = autonomy.PlanProgress(total=1, completed=1, failed=0, planned=0,
                                        in_progress=0, pending=0, runnable=0)
        with mock.patch.object(autonomy, "plan_progress", return_value=finished):
            result = self.service.workspace_merge(intent_id="intent-merged")

        self.assertTrue(result["success"])
        self.assertEqual(result["applied"], {"added": 1, "modified": 1, "deleted": 0})
        self.assertEqual(_read(self.host, "pkg/mod.py"), "value = 8\n")
        self.assertEqual(_read(self.host, "pkg/added.py"), "added = True\n")
        self.assertFalse(os.path.exists(shadow.path))
        self.assertIsNone(staging.find_staging(intent_id="intent-merged"))

    def test_workspace_merge_rejection_purges_and_leaves_the_host(self):
        with self.service._staged_execution("intent-rejected") as shadow:
            _write(shadow.path, "pkg/mod.py", "value = 6\n")

        result = self.service.workspace_merge(intent_id="intent-rejected", approve=False)
        self.assertTrue(result["success"])
        self.assertTrue(result["rejected"])
        self.assertFalse(os.path.exists(shadow.path))
        self.assertEqual(_read(self.host, "pkg/mod.py"), "value = 1\n")

    def test_a_merge_with_nothing_staged_refuses_with_the_envelope_intact(self):
        result = self.service.workspace_merge(intent_id="never-staged")
        self.assertFalse(result["success"])
        self.assertTrue(result["error"])

    def test_a_diff_without_an_intent_id_is_refused(self):
        for missing in (None, "", "   "):
            result = self.service.workspace_diff(intent_id=missing)
            self.assertFalse(result["success"], missing)
            self.assertIn("intent id", result["error"])

    def test_a_merge_without_an_intent_id_is_refused(self):
        for missing in (None, "", "   "):
            result = self.service.workspace_merge(intent_id=missing)
            self.assertFalse(result["success"], missing)
            self.assertIn("intent id", result["error"])

    def test_a_merge_under_a_held_lock_is_a_conflict(self):
        with self.service._staged_execution("intent-locked") as shadow:
            _write(shadow.path, "pkg/mod.py", "value = 9\n")

        with staging.merge_lock():
            with self.assertRaises(staging.StagingLocked):
                self.service.workspace_merge(intent_id="intent-locked")

        # The conflict refused the merge: the host and the shadow are both untouched.
        self.assertEqual(_read(self.host, "pkg/mod.py"), "value = 1\n")
        self.assertTrue(os.path.exists(shadow.path))

    def test_two_intents_do_not_share_a_shadow(self):
        with self.service._staged_execution("intent-a") as first:
            _write(first.path, "pkg/only-a.py", "a = 1\n")
        with self.service._staged_execution("intent-b") as second:
            self.assertNotEqual(first.path, second.path)
            self.assertFalse(os.path.exists(os.path.join(second.path, "pkg", "only-a.py")))
        # The second run's delta is its own: the first run's work is not in it.
        self.assertEqual(self.service.workspace_diff(intent_id="intent-b")["added"], [])

    def test_staging_the_same_intent_twice_reuses_its_shadow(self):
        with self.service._staged_execution("intent-reuse") as first:
            _write(first.path, "pkg/mod.py", "value = 11\n")
        with self.service._staged_execution("intent-reuse") as again:
            # The same execution keeps one shadow: a second pass joins the same delta rather than
            # replacing it (an approval releasing the artifact the first pass proposed).
            self.assertEqual(again.path, first.path)
            self.assertEqual(_read(again.path, "pkg/mod.py"), "value = 11\n")
        delta = self.service.workspace_diff(intent_id="intent-reuse")
        self.assertEqual(delta["modified"], ["pkg/mod.py"])
        self.assertFalse(delta["clean"])

    def test_the_container_would_mount_the_shadow_not_the_host(self):
        """The bind mount is the shadow: the one path the container can write is a copy."""
        from tools import docker_sandbox

        with mock.patch.dict(os.environ, fake_docker_env()):
            docker_sandbox.reset_caches()
            self.addCleanup(docker_sandbox.reset_caches)
            with self.service._staged_execution("intent-mount") as shadow:
                argv = docker_sandbox.build_command(
                    "echo hi", root=workspace.get_execution_dir(), name="aleth-test"
                )
        joined = " ".join(argv)
        self.assertIn(f"source={os.path.realpath(shadow.path)}", joined)
        self.assertNotIn(f"source={os.path.realpath(self.host)}", joined)


class DurableWriteTests(unittest.TestCase):
    """Phase 22: a write lands whole or not at all -- in the shadow, and on the host.

    The engine severs runs on purpose (the wall-clock TTL, the OOM killer), so a write in flight
    is a write that gets interrupted. Every test here is the same claim from a different angle:
    the target ends up as one of its two complete versions, never as a prefix.
    """

    def setUp(self):
        self.host = tempfile.mkdtemp(prefix="aleth_durable_host_")
        self.addCleanup(shutil.rmtree, self.host, ignore_errors=True)
        _write(self.host, "pkg/mod.py", "value = 1\n")
        staging.remove_tree(staging.staging_base())
        self.addCleanup(shutil.rmtree, staging.staging_base(), ignore_errors=True)

    @staticmethod
    def _scratch(root: str):
        """Scratch files left under ``root``: a temporary that never became its target."""
        found = []
        for current, _dirs, files in os.walk(root):
            for name in files:
                if name.startswith(".tmp."):
                    found.append(os.path.relpath(os.path.join(current, name), root))
        return sorted(found)

    def test_the_temporary_is_a_sibling_of_its_target(self):
        """``os.replace`` is atomic within one filesystem and ``EXDEV`` across two.

        The shadow lives under the state root and a merge writes into the user's repository, so
        a temporary in the system temp directory would be a different device in production and
        the rename would fail outright. Asserted on the real call, not on a convention.
        """
        shadow = staging.create_staging(self.host, intent_id="i-sibling")
        _write(shadow.path, "pkg/mod.py", "value = 5\n")
        seen = []
        real_replace = os.replace

        def _record(source, target):
            seen.append((os.path.dirname(source), os.path.dirname(os.path.abspath(target))))
            return real_replace(source, target)

        with mock.patch("tools.atomic_io.os.replace", _record):
            staging.merge_staging(shadow)

        self.assertTrue(seen)
        for source_dir, target_dir in seen:
            self.assertEqual(source_dir, target_dir)
        self.assertEqual(_read(self.host, "pkg/mod.py"), "value = 5\n")

    def test_a_failed_merge_leaves_the_host_file_whole_and_no_litter(self):
        shadow = staging.create_staging(self.host, intent_id="i-failed-merge")
        _write(shadow.path, "pkg/mod.py", "value = 7\n")

        with mock.patch("tools.atomic_io.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                staging.merge_staging(shadow)

        # The bytes the user had are still the bytes the user has, and the aborted temporary is
        # gone rather than sitting in their repository as an untracked file.
        self.assertEqual(_read(self.host, "pkg/mod.py"), "value = 1\n")
        self.assertEqual(self._scratch(self.host), [])

    def test_create_staging_leaves_no_scratch_manifest(self):
        shadow = staging.create_staging(self.host, intent_id="i-manifest-scratch")
        self.assertEqual(self._scratch(shadow.path), [])
        self.assertEqual(staging.load_staging(shadow.staging_id).intent_id, "i-manifest-scratch")


class OrphanedStagingTests(unittest.TestCase):
    """Phase 22: a shadow no run can reach any more is collected, and a live one is not."""

    def setUp(self):
        self.host = tempfile.mkdtemp(prefix="aleth_orphan_host_")
        self.addCleanup(shutil.rmtree, self.host, ignore_errors=True)
        _write(self.host, "pkg/mod.py", "value = 1\n")
        staging.remove_tree(staging.staging_base())
        self.addCleanup(shutil.rmtree, staging.staging_base(), ignore_errors=True)

    def test_a_shadow_awaiting_review_is_kept(self):
        shadow = staging.create_staging(self.host, intent_id="i-reviewable")

        self.assertEqual(staging.purge_orphaned_stagings({"i-reviewable"}), [])

        self.assertTrue(os.path.isdir(shadow.path))

    def test_a_shadow_whose_intent_is_gone_is_collected(self):
        shadow = staging.create_staging(self.host, intent_id="i-dead")

        self.assertEqual(staging.purge_orphaned_stagings({"i-live"}), [shadow.staging_id])

        self.assertFalse(os.path.exists(shadow.path))

    def test_a_directory_with_no_manifest_is_collected(self):
        """A kill between the copy and its manifest leaves exactly this: a copy nothing can
        resolve, and therefore nothing that would ever have removed it."""
        orphan = os.path.join(staging.staging_base(), "0123456789ab")
        _write(orphan, "pkg/mod.py", "value = 1\n")

        self.assertEqual(staging.purge_orphaned_stagings(set()), ["0123456789ab"])

        self.assertFalse(os.path.exists(orphan))

    def test_the_sweep_never_touches_the_host(self):
        staging.create_staging(self.host, intent_id="i-untouched")

        staging.purge_orphaned_stagings(set())

        self.assertEqual(_read(self.host, "pkg/mod.py"), "value = 1\n")


class MergeGateTests(unittest.TestCase):
    """Phase 29: a shadow that does not pass the project's own suite is unmergeable.

    The verdict is written to the shadow's *manifest*, not held in the run's memory: the merge is a
    later request from the UI, and a verdict that lived in the process would be gone by then.
    """

    def setUp(self):
        from app import EngineService

        self.host = tempfile.mkdtemp(prefix="aleth_gate_host_")
        self.addCleanup(shutil.rmtree, self.host, ignore_errors=True)
        _write(self.host, "pkg/mod.py", "value = 1\n")
        self._original_project = workspace.get_project_dir()
        workspace.set_project_dir(self.host)
        self.addCleanup(workspace.set_project_dir, self._original_project)
        self.addCleanup(workspace.set_execution_dir, None)
        staging.remove_tree(staging.staging_base())
        self.addCleanup(shutil.rmtree, staging.staging_base(), ignore_errors=True)
        self.service = EngineService()

    @staticmethod
    def _finished():
        from orchestration import autonomy

        return autonomy.PlanProgress(
            total=1, completed=1, failed=0, planned=0, in_progress=0, pending=0, runnable=0
        )

    def _merge(self, intent_id):
        from orchestration import autonomy

        with mock.patch.object(autonomy, "plan_progress", return_value=self._finished()):
            return self.service.workspace_merge(intent_id=intent_id)

    def test_a_failed_verification_makes_the_shadow_unmergeable(self):
        with self.service._staged_execution("intent-bad") as shadow:
            _write(shadow.path, "pkg/mod.py", "value = 9\n")
        staging.record_verification(shadow, False, "1 failed, 2 passed")

        result = self._merge("intent-bad")

        self.assertFalse(result["success"])
        self.assertIn("not verified", result["error"])
        self.assertIn("1 failed", result["error"])
        # The host is untouched, and the shadow is *kept* for debugging -- unmergeable, not deleted.
        self.assertEqual(_read(self.host, "pkg/mod.py"), "value = 1\n")
        self.assertTrue(os.path.exists(shadow.path))

    def test_a_passed_verification_merges(self):
        with self.service._staged_execution("intent-good") as shadow:
            _write(shadow.path, "pkg/mod.py", "value = 7\n")
        staging.record_verification(shadow, True)

        result = self._merge("intent-good")

        self.assertTrue(result["success"], result)
        self.assertEqual(_read(self.host, "pkg/mod.py"), "value = 7\n")

    def test_an_unrun_gate_is_not_a_pass(self):
        """Phase 30 sealed this: ``None`` used to merge, which was the gate's fatal bypass.

        A shadow staged before the gate existed -- or one whose runtime could not be determined --
        has no verdict, and an unchecked diff must not reach the user's tree.
        """
        with self.service._staged_execution("intent-unverified") as shadow:
            _write(shadow.path, "pkg/mod.py", "value = 8\n")
        staging.record_verification(shadow, None)

        result = self._merge("intent-unverified")

        self.assertFalse(result["success"])
        self.assertIsNone(result["verified"])
        self.assertIn("did not run", result["error"])
        self.assertEqual(_read(self.host, "pkg/mod.py"), "value = 1\n")

    def test_a_shadow_with_no_verdict_at_all_is_refused(self):
        """A shadow from an older build has no verdict key, which is also not a pass."""
        with self.service._staged_execution("intent-legacy") as shadow:
            _write(shadow.path, "pkg/mod.py", "value = 3\n")

        result = self._merge("intent-legacy")

        self.assertFalse(result["success"])
        self.assertIn("not verified", result["error"])
        self.assertEqual(_read(self.host, "pkg/mod.py"), "value = 1\n")

    def test_a_rejection_is_unaffected_by_the_gate(self):
        """Discarding work is always allowed: the gate guards the *merge*, not the purge."""
        with self.service._staged_execution("intent-reject") as shadow:
            _write(shadow.path, "pkg/mod.py", "value = 6\n")
        staging.record_verification(shadow, False, "boom")

        result = self.service.workspace_merge(intent_id="intent-reject", approve=False)

        self.assertTrue(result["success"])
        self.assertTrue(result["rejected"])
        self.assertFalse(os.path.exists(shadow.path))

    def test_the_verdict_survives_the_process(self):
        with self.service._staged_execution("intent-durable") as shadow:
            _write(shadow.path, "pkg/mod.py", "value = 5\n")
        staging.record_verification(shadow, False, "a syntax error")

        reloaded = staging.load_staging(shadow.staging_id)

        self.assertIs(reloaded.verified, False)
        self.assertIn("syntax error", reloaded.verify_error)

    def test_an_unrecorded_shadow_has_no_verdict(self):
        with self.service._staged_execution("intent-unknown") as shadow:
            _write(shadow.path, "pkg/mod.py", "value = 4\n")
        self.assertIsNone(staging.load_staging(shadow.staging_id).verified)


class ExecutionWiringTests(unittest.TestCase):
    """The execution call sites resolve the shadow, not the live tree."""

    def setUp(self):
        self.host = tempfile.mkdtemp(prefix="aleth_wiring_host_")
        self.addCleanup(shutil.rmtree, self.host, ignore_errors=True)
        _write(self.host, "pkg/mod.py", "value = 1\n")
        self._original_project = workspace.get_project_dir()
        workspace.set_project_dir(self.host)
        self.addCleanup(workspace.set_project_dir, self._original_project)
        self.addCleanup(workspace.set_execution_dir, None)
        staging.remove_tree(staging.staging_base())
        self.addCleanup(shutil.rmtree, staging.staging_base(), ignore_errors=True)

    def test_run_approved_artifact_applies_into_the_execution_root(self):
        from orchestration.workflow import execution

        shadow = staging.create_staging(self.host, intent_id="wired-artifact")
        captured = {}

        class _Stop(Exception):
            pass

        class _Store:
            def get_artifact(self, plan_id, task_id):
                return {
                    "plan_id": plan_id, "task_id": task_id,
                    "ast_targets": [{"file_path": "pkg/mod.py"}],
                }

        class _Ctx:
            def emit_fn(self, event):
                pass

            def stream_text(self, *args, **kwargs):
                pass

            def should_stop(self):
                return False

        def _execute(plan_id, task_id, *, workspace_dir):
            captured["workspace_dir"] = workspace_dir
            raise _Stop()

        workspace.set_execution_dir(shadow.path)
        with mock.patch.object(execution, "get_store", lambda: _Store()), \
                mock.patch.object(execution.executor, "execute_approved", _execute), \
                mock.patch.object(execution.time, "sleep", lambda *a, **k: None):
            with self.assertRaises(_Stop):
                execution.run_approved_artifact(_Ctx(), {}, {}, {"planId": "PLAN", "taskId": "t1"})

        self.assertEqual(captured["workspace_dir"], shadow.path)

    def test_the_test_runner_runs_inside_the_execution_root(self):
        from tools import test_runner

        shadow = staging.create_staging(self.host, intent_id="wired-runner")
        _write(shadow.path, "test_sample.py", "def test_ok():\n    assert True\n")
        captured = {}

        class _Result:
            timed_out = False
            stdout = "1 passed in 0.01s"
            stderr = ""
            returncode = 0

        def _run(command, *, cwd, timeout):
            captured["cwd"] = cwd
            return _Result()

        workspace.set_execution_dir(shadow.path)
        with mock.patch.object(test_runner, "test_files_for_task", lambda key: ["test_sample.py"]), \
                mock.patch.object(test_runner.docker_sandbox, "run_isolated", _run):
            verdict = test_runner.run_task_tests("task-wired")

        self.assertEqual(captured["cwd"], shadow.path)
        self.assertEqual(verdict["verdict"], "passed")

    def test_the_runner_reads_the_test_file_from_the_shadow(self):
        """A test file written only into the shadow is the one that runs."""
        from tools import test_runner

        shadow = staging.create_staging(self.host, intent_id="wired-shadow-file")
        _write(shadow.path, "test_sample.py", "def test_ok():\n    assert True\n")
        self.assertFalse(os.path.exists(os.path.join(self.host, "test_sample.py")))

        class _Result:
            timed_out = False
            stdout = "1 passed in 0.01s"
            stderr = ""
            returncode = 0

        workspace.set_execution_dir(shadow.path)
        with mock.patch.object(test_runner, "test_files_for_task", lambda key: ["test_sample.py"]), \
                mock.patch.object(test_runner.docker_sandbox, "run_isolated",
                                  lambda *a, **k: _Result()):
            verdict = test_runner.run_task_tests("task-shadow")

        self.assertEqual(verdict["verdict"], "passed")


class ManifestShapeTests(unittest.TestCase):
    """The manifest is the durable identity of a shadow, so its shape is pinned."""

    def setUp(self):
        self.host = tempfile.mkdtemp(prefix="aleth_manifest_host_")
        self.addCleanup(shutil.rmtree, self.host, ignore_errors=True)
        staging.remove_tree(staging.staging_base())
        self.addCleanup(shutil.rmtree, staging.staging_base(), ignore_errors=True)

    def test_the_manifest_is_json_with_the_run_identity(self):
        _write(self.host, "a.py", "a = 1\n")
        shadow = staging.create_staging(self.host, intent_id="i-man", plan_id="PLAN-Z")
        with open(os.path.join(shadow.path, staging.STAGING_MANIFEST), encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["staging_id"], shadow.staging_id)
        self.assertEqual(data["intent_id"], "i-man")
        self.assertEqual(data["plan_id"], "PLAN-Z")


class ExecutionRootBifurcationTests(unittest.TestCase):
    """Phase 23: the run's file reads and the frontend's file reads resolve opposite roots.

    The Phase 20 breach, formalized. ``read_source`` is the read half of a read-modify-write patch
    and resolved against the *project* directory, so it read the host while the patch landed in the
    shadow -- a patch against a stale base, and a second step that could not see the first step's
    edit. (Its writing counterpart, ``overwrite_source``, was deleted in Phase 25: the executor
    publishes through the MCP filesystem server, whose root is already the shadow.)
    """

    def setUp(self):
        from app import EngineService

        self.host = tempfile.mkdtemp(prefix="aleth_bifurcation_host_")
        self.addCleanup(shutil.rmtree, self.host, ignore_errors=True)
        _write(self.host, "pkg/mod.py", "value = 1\n")
        self._original_project = workspace.get_project_dir()
        workspace.set_project_dir(self.host)
        self.addCleanup(workspace.set_project_dir, self._original_project)
        self.addCleanup(workspace.set_execution_dir, None)
        staging.remove_tree(staging.staging_base())
        self.addCleanup(shutil.rmtree, staging.staging_base(), ignore_errors=True)
        self.service = EngineService()

    def test_a_read_resolves_the_execution_root_not_the_project(self):
        """The read half of a read-modify-write patch must come from the root the write targets.

        The write itself goes over MCP (``executor.apply_artifact``); this pins the *read* side,
        which is the half that was resolving the user's tree while the patch landed in the shadow.
        """
        with self.service._staged_execution("intent-steps") as shadow:
            _write(shadow.path, "pkg/mod.py", "value = 2\n")

            self.assertEqual(execution_io.read_source("pkg/mod.py"), "value = 2\n")

        # The host still holds the pre-run bytes: the read never left the shadow.
        self.assertEqual(_read(self.host, "pkg/mod.py"), "value = 1\n")

    def test_the_ui_reads_still_see_the_live_project(self):
        """The frontend's ground truth is the user's tree, and stays so mid-run."""
        with self.service._staged_execution("intent-ui"):
            _write(self.host, "live.py", "live = 1\n")
            paths = [entry["path"] for entry in list_workspace_files()]

        self.assertIn("live.py", paths)

    def test_an_injected_root_is_the_only_root(self):
        granted = tempfile.mkdtemp(prefix="aleth_granted_")
        self.addCleanup(shutil.rmtree, granted, ignore_errors=True)
        _write(granted, "injected.py", "x = 1\n")

        self.assertEqual(execution_io.read_source("injected.py", root=granted), "x = 1\n")
        # The project's own copy is not what answered -- the injected root is the only one.
        self.assertEqual(execution_io.read_source("injected.py"), "")

    def test_an_injected_root_cannot_be_escaped(self):
        granted = tempfile.mkdtemp(prefix="aleth_granted_")
        self.addCleanup(shutil.rmtree, granted, ignore_errors=True)

        with self.assertRaises(ValueError) as caught:
            execution_io.read_source("../../escape.py", root=granted)
        self.assertIn("Path traversal denied", str(caught.exception))

    def test_the_execution_module_never_reaches_for_the_project_root(self):
        """The bifurcation is a property of the module, not of one code path inside it.

        A future edit that resolved against ``get_project_dir`` would be the Phase 20 breach again,
        so the boundary is pinned the way the deleted tool catalog is -- and pinned on the module's
        *code* (its imports, names and attributes), not on its prose: the docstring names
        ``get_project_dir`` precisely to say it is out of scope here.
        """
        tree = ast.parse(inspect.getsource(execution_io))
        referenced = (
            {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
            | {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in node.names
            }
        )
        self.assertNotIn("get_project_dir", referenced)
        self.assertFalse(hasattr(execution_io, "get_project_dir"))
        # ...and bound to the other root, so this pins the binding and not merely an absence.
        self.assertIn("get_execution_dir", referenced)

    def test_the_orchestrator_injects_the_run_root_into_the_context(self):
        """The root is injected once, at the orchestrator seam -- not resolved by the tool."""
        from orchestration.workflow import actions_admin, runner

        class _NullSession:
            def __init__(self, root):
                self.root = root

            def __enter__(self):
                return None

            def __exit__(self, *exc):
                return False

        captured = {}
        with self.service._staged_execution("intent-ctx-root") as shadow:
            with mock.patch.object(runner, "MCPSessionContext", _NullSession), \
                    mock.patch.object(runner, "load_plan_state", return_value={}), \
                    mock.patch.object(
                        actions_admin, "analyze_action",
                        lambda ctx: captured.update(root=ctx.execution_root),
                    ):
                runner.run_agent_workflow(
                    coder_agents={},
                    stop_event=threading.Event(),
                    user_message="analyze the codebase",
                    emit_fn=lambda event: None,
                    action_type="analyze",
                )

        self.assertEqual(captured.get("root"), shadow.path)


if __name__ == "__main__":
    unittest.main()
