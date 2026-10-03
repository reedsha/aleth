"""Phase 38: the extraction gate. Staged work reaches the host through one path, and it is safe.

Two properties, and they are the two the staging boundary was built for:

* **verified** -- only a shadow the project's own suite (or its syntax check) actually passed may be
  applied, because an unchecked diff on a person's tree is what the boundary exists to prevent;
* **collision-gated** -- a path the *agent* changed that the *human* also changed refuses the whole
  egress rather than overwriting their work. The engine applies the agent's work and never a
  person's.

The baseline -- the host as the run found it -- is what makes the second one answerable, so these
tests pin it as much as the apply.
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from tools import staging, workspace


def _write(root, name, text):
    path = os.path.join(root, name)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return path


def _read(root, name):
    with open(os.path.join(root, name), "r", encoding="utf-8", newline="") as handle:
        return handle.read()


class EgressPlanTests(unittest.TestCase):
    """The delta and the collision, both measured against the baseline."""

    def setUp(self):
        self.host = tempfile.mkdtemp(prefix="aleth_egress_host_")
        self.addCleanup(shutil.rmtree, self.host, ignore_errors=True)
        _write(self.host, "pkg/mod.py", "value = 1\n")
        _write(self.host, "pkg/other.py", "other = 1\n")
        staging.remove_tree(staging.staging_base())
        self.addCleanup(staging.remove_tree, staging.staging_base())

    def test_the_baseline_is_recorded_when_the_shadow_is_made(self):
        shadow = staging.create_staging(self.host, intent_id="i-1")

        baseline = staging.host_baseline(shadow)
        self.assertIn("pkg/mod.py", baseline)
        self.assertIn("pkg/other.py", baseline)
        # ...and it is a *point in time*, not a live reading: editing the host afterwards must not
        # change it, or the collision check would have nothing to compare against.
        _write(self.host, "pkg/mod.py", "value = 99\n")
        self.assertEqual(staging.host_baseline(shadow), baseline)

    def test_the_agents_delta_is_measured_against_the_baseline(self):
        shadow = staging.create_staging(self.host, intent_id="i-1")
        _write(shadow.path, "pkg/mod.py", "value = 2\n")
        _write(shadow.path, "pkg/new.py", "new = True\n")
        os.remove(os.path.join(shadow.path, "pkg/other.py"))

        plan = staging.egress_plan(shadow)

        self.assertTrue(plan["baseline_recorded"])
        self.assertEqual(plan["added"], ["pkg/new.py"])
        self.assertEqual(plan["modified"], ["pkg/mod.py"])
        self.assertEqual(plan["deleted"], ["pkg/other.py"])
        self.assertFalse(plan["conflict"])
        self.assertEqual(plan["collisions"], [])

    def test_a_human_edit_to_a_file_the_agent_also_changed_is_a_collision(self):
        shadow = staging.create_staging(self.host, intent_id="i-1")
        _write(shadow.path, "pkg/mod.py", "value = 'agent'\n")
        # The person saved the same file while the agent was working.
        _write(self.host, "pkg/mod.py", "value = 'human'\n")

        plan = staging.egress_plan(shadow)

        self.assertTrue(plan["conflict"])
        self.assertEqual(plan["collisions"], ["pkg/mod.py"])

    def test_a_human_edit_the_agent_did_not_touch_is_not_a_collision(self):
        """The egress only writes what the agent changed, so an unrelated edit is not its business."""
        shadow = staging.create_staging(self.host, intent_id="i-1")
        _write(shadow.path, "pkg/mod.py", "value = 'agent'\n")
        _write(self.host, "pkg/other.py", "other = 'human'\n")

        plan = staging.egress_plan(shadow)

        self.assertFalse(plan["conflict"])
        self.assertEqual(plan["modified"], ["pkg/mod.py"])

    def test_a_human_deleting_a_file_the_agent_modified_is_a_collision(self):
        shadow = staging.create_staging(self.host, intent_id="i-1")
        _write(shadow.path, "pkg/mod.py", "value = 'agent'\n")
        os.remove(os.path.join(self.host, "pkg/mod.py"))

        plan = staging.egress_plan(shadow)

        self.assertTrue(plan["conflict"])
        self.assertIn("pkg/mod.py", plan["collisions"])


class EgressApplyTests(unittest.TestCase):
    """The apply itself: all or nothing, and never over a human's work."""

    def setUp(self):
        self.host = tempfile.mkdtemp(prefix="aleth_egress_apply_")
        self.addCleanup(shutil.rmtree, self.host, ignore_errors=True)
        _write(self.host, "pkg/mod.py", "value = 1\n")
        _write(self.host, "pkg/gone.py", "gone = True\n")
        staging.remove_tree(staging.staging_base())
        self.addCleanup(staging.remove_tree, staging.staging_base())

    def _shadow(self, intent_id="i-1"):
        shadow = staging.create_staging(self.host, intent_id=intent_id)
        _write(shadow.path, "pkg/mod.py", "value = 2\n")
        _write(shadow.path, "pkg/new.py", "new = True\n")
        os.remove(os.path.join(shadow.path, "pkg/gone.py"))
        return shadow

    def test_an_egress_applies_additions_modifications_and_deletions(self):
        shadow = self._shadow()

        result = staging.apply_egress(shadow)

        self.assertTrue(result["success"], result)
        self.assertEqual(result["applied"], {"added": 1, "modified": 1, "deleted": 1})
        self.assertEqual(_read(self.host, "pkg/mod.py"), "value = 2\n")
        self.assertEqual(_read(self.host, "pkg/new.py"), "new = True\n")
        self.assertFalse(os.path.exists(os.path.join(self.host, "pkg/gone.py")))

    def test_a_collision_refuses_the_whole_egress_and_touches_nothing(self):
        shadow = self._shadow()
        _write(self.host, "pkg/mod.py", "value = 'human'\n")

        with self.assertRaises(staging.EgressConflict):
            staging.apply_egress(shadow)

        # The person's bytes are still the person's bytes, and nothing else landed either.
        self.assertEqual(_read(self.host, "pkg/mod.py"), "value = 'human'\n")
        self.assertFalse(os.path.exists(os.path.join(self.host, "pkg/new.py")))
        self.assertTrue(os.path.exists(os.path.join(self.host, "pkg/gone.py")))

    def test_a_failure_half_way_rolls_the_host_back(self):
        """Atomic in the sense that matters: the host is never left half-applied."""
        shadow = self._shadow()
        real = staging.atomic_io.copy_file_atomic
        calls = {"n": 0}

        def flaky(source, target):
            calls["n"] += 1
            if calls["n"] > 1:
                raise OSError("the disk is full")
            return real(source, target)

        with mock.patch.object(staging.atomic_io, "copy_file_atomic", flaky):
            with self.assertRaises(staging.StagingError):
                staging.apply_egress(shadow)

        # Everything the failed apply touched is back where it was.
        self.assertEqual(_read(self.host, "pkg/mod.py"), "value = 1\n")
        self.assertTrue(os.path.exists(os.path.join(self.host, "pkg/gone.py")))

    def test_a_clean_egress_leaves_no_recovery_patch_behind(self):
        shadow = self._shadow()

        staging.apply_egress(shadow)

        self.assertFalse(os.path.exists(os.path.join(self.host, staging.EGRESS_PATCH_NAME)))

    def test_a_failed_egress_keeps_the_recovery_patch_and_says_where(self):
        """The artifact is the one guarantee that survives a crash: an in-process rollback cannot."""
        shadow = self._shadow()

        def broken(source, target):
            raise OSError("the disk is full")

        with mock.patch.object(staging.atomic_io, "copy_file_atomic", broken):
            with self.assertRaises(staging.StagingError) as caught:
                staging.apply_egress(shadow)

        self.assertIn(staging.EGRESS_PATCH_NAME, str(caught.exception))
        patch = os.path.join(self.host, staging.EGRESS_PATCH_NAME)
        self.assertTrue(os.path.isfile(patch))
        body = _read(self.host, staging.EGRESS_PATCH_NAME)
        self.assertIn("a/pkg/mod.py", body)
        self.assertIn("b/pkg/new.py", body)

    def test_the_recovery_patch_is_a_real_patch(self):
        """A recovery artifact nobody can apply is not one: the framing is what ``git apply`` wants."""
        shadow = self._shadow()

        def broken(source, target):
            raise OSError("the disk is full")

        with mock.patch.object(staging.atomic_io, "copy_file_atomic", broken):
            with self.assertRaises(staging.StagingError):
                staging.apply_egress(shadow)

        import subprocess

        completed = subprocess.run(
            ["git", "apply", "--check", "-p1", staging.EGRESS_PATCH_NAME],
            cwd=self.host, capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


class EgressServiceTests(unittest.TestCase):
    """``EngineService.egress_intent``: the gate, through the surface the UI calls."""

    def setUp(self):
        from app import EngineService

        self.host = tempfile.mkdtemp(prefix="aleth_egress_svc_")
        self.addCleanup(shutil.rmtree, self.host, ignore_errors=True)
        _write(self.host, "pkg/mod.py", "value = 1\n")
        self._original_project = workspace.get_project_dir()
        workspace.set_project_dir(self.host)
        self.addCleanup(workspace.set_project_dir, self._original_project)
        self.addCleanup(workspace.set_execution_dir, None)
        staging.remove_tree(staging.staging_base())
        self.addCleanup(staging.remove_tree, staging.staging_base())
        self.service = EngineService()

    @staticmethod
    def _finished():
        from orchestration import autonomy

        return autonomy.PlanProgress(
            total=1, completed=1, failed=0, planned=0, in_progress=0, pending=0, runnable=0
        )

    def _egress(self, intent_id):
        from orchestration import autonomy

        with mock.patch.object(autonomy, "plan_progress", return_value=self._finished()):
            return self.service.egress_intent(intent_id=intent_id)

    def test_an_egress_needs_an_intent_id(self):
        self.assertFalse(self.service.egress_intent(intent_id="")["success"])

    def test_an_unverified_shadow_is_refused(self):
        with self.service._staged_execution("intent-unverified") as shadow:
            _write(shadow.path, "pkg/mod.py", "value = 2\n")
        staging.record_verification(shadow, False, "1 failed")

        result = self._egress("intent-unverified")

        self.assertFalse(result["success"])
        self.assertIn("not verified", result["error"])
        self.assertEqual(_read(self.host, "pkg/mod.py"), "value = 1\n")

    def test_a_verified_shadow_is_applied_and_purged(self):
        with self.service._staged_execution("intent-ok") as shadow:
            _write(shadow.path, "pkg/mod.py", "value = 7\n")
        staging.record_verification(shadow, True)

        result = self._egress("intent-ok")

        self.assertTrue(result["success"], result)
        self.assertEqual(_read(self.host, "pkg/mod.py"), "value = 7\n")
        self.assertFalse(os.path.exists(shadow.path), "an applied shadow is purged")

    def test_a_collision_is_a_refusal_not_an_overwrite(self):
        with self.service._staged_execution("intent-clash") as shadow:
            _write(shadow.path, "pkg/mod.py", "value = 'agent'\n")
        staging.record_verification(shadow, True)
        _write(self.host, "pkg/mod.py", "value = 'human'\n")

        result = self._egress("intent-clash")

        self.assertFalse(result["success"])
        self.assertTrue(result.get("conflict"))
        self.assertIn("pkg/mod.py", result["collisions"])
        self.assertEqual(_read(self.host, "pkg/mod.py"), "value = 'human'\n")
        # The shadow is kept: the work is not lost, it just needs a person to reconcile it.
        self.assertTrue(os.path.exists(shadow.path))

    def test_the_older_merge_approve_path_is_the_same_gate(self):
        """``workspace_merge(approve=True)`` delegates, so it cannot be a second, ungated way in."""
        with self.service._staged_execution("intent-delegated") as shadow:
            _write(shadow.path, "pkg/mod.py", "value = 'agent'\n")
        staging.record_verification(shadow, True)
        _write(self.host, "pkg/mod.py", "value = 'human'\n")

        result = self._merge("intent-delegated")

        self.assertFalse(result["success"])
        self.assertTrue(result.get("conflict"))
        self.assertEqual(_read(self.host, "pkg/mod.py"), "value = 'human'\n")

    def _merge(self, intent_id):
        from orchestration import autonomy

        with mock.patch.object(autonomy, "plan_progress", return_value=self._finished()):
            return self.service.workspace_merge(intent_id=intent_id)

    def test_a_rejection_still_discards_without_applying(self):
        with self.service._staged_execution("intent-reject") as shadow:
            _write(shadow.path, "pkg/mod.py", "value = 'agent'\n")
        staging.record_verification(shadow, False, "boom")

        result = self.service.workspace_merge(intent_id="intent-reject", approve=False)

        self.assertTrue(result["success"])
        self.assertTrue(result["rejected"])
        self.assertEqual(_read(self.host, "pkg/mod.py"), "value = 1\n")
        self.assertFalse(os.path.exists(shadow.path))


if __name__ == "__main__":
    unittest.main()
