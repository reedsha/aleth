"""Phase 20: the shadow workspace, the delta, and the merge boundary.

These tests hold the file-system boundary to the claim that makes it worth having: an agent can
write anything it likes and the user's live tree is byte-identical afterwards, until a person
merges the delta. The unit tests exercise ``tools.staging`` directly; the service tests drive
the same machinery through ``EngineService`` -- the execution root the run loop sets, the
review read, and the gate that refuses to merge a plan that is not finished.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from tools import staging, workspace

FAKE_DOCKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_docker.py")


def fake_docker_env(**extra):
    """The environment that points the perimeter at the in-repo double."""
    env = {"ALETH_DOCKER_BIN": f"{sys.executable} {FAKE_DOCKER}"}
    env.update(extra)
    return env


def _write(root: str, name: str, text: str) -> None:
    path = os.path.join(root, name)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
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
        shutil.rmtree(staging.staging_base(), ignore_errors=True)
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

    def test_find_staging_resolves_by_intent_and_by_plan(self):
        self._host_with_files()
        shadow = staging.create_staging(self.host, intent_id="i-find", plan_id="PLAN-X")
        self.assertEqual(staging.find_staging(intent_id="i-find").staging_id, shadow.staging_id)
        self.assertEqual(staging.find_staging(plan_id="PLAN-X").staging_id, shadow.staging_id)
        self.assertEqual(
            staging.find_staging(staging_id=shadow.staging_id).staging_id, shadow.staging_id
        )
        self.assertIsNone(staging.find_staging(intent_id="never-created"))

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


class ExecutionRootTests(unittest.TestCase):
    """The process-wide root that selects the shadow during a run."""

    def setUp(self):
        self._original = workspace.get_project_dir()
        self.addCleanup(workspace.set_project_dir, self._original)
        self.addCleanup(workspace.set_execution_dir, None)
        self.addCleanup(shutil.rmtree, staging.staging_base(), ignore_errors=True)
        shutil.rmtree(staging.staging_base(), ignore_errors=True)

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
        shutil.rmtree(staging.staging_base(), ignore_errors=True)
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
        shutil.rmtree(staging.staging_base(), ignore_errors=True)
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
        shutil.rmtree(staging.staging_base(), ignore_errors=True)
        self.addCleanup(shutil.rmtree, staging.staging_base(), ignore_errors=True)

    def test_the_manifest_is_json_with_the_run_identity(self):
        _write(self.host, "a.py", "a = 1\n")
        shadow = staging.create_staging(self.host, intent_id="i-man", plan_id="PLAN-Z")
        with open(os.path.join(shadow.path, staging.STAGING_MANIFEST), encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["staging_id"], shadow.staging_id)
        self.assertEqual(data["intent_id"], "i-man")
        self.assertEqual(data["plan_id"], "PLAN-Z")


if __name__ == "__main__":
    unittest.main()
