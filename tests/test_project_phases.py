"""Phase 29: the deterministic phases around the model loop.

Two properties, and they pull in opposite directions on purpose:

* **Setup may have egress** -- a project's dependencies cannot be installed without it, and an agent
  that cannot install them cannot test its own work. The model is not involved: the command comes
  from the project's manifests, and it runs before the loop starts.
* **Verification may not** -- it executes the workspace's own code, which is the code the model just
  wrote, so it is the last place egress belongs.

The command *detection* is a fixed table plus a human's declaration; nothing here reads a file the
model wrote to decide what to execute.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from tools import docker_sandbox, project_phases

FAKE_DOCKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_docker.py")


def fake_docker_env(**extra):
    """The environment that points the perimeter at the in-repo double."""
    env = {"ALETH_DOCKER_BIN": f"{sys.executable} {FAKE_DOCKER}"}
    env.update(extra)
    return env


class PhaseDetectionTests(unittest.TestCase):
    """What each phase would run, decided from the project's own manifests."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="aleth_phases_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _write(self, name, text):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path) or self.root, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return path

    def test_a_project_with_no_manifests_declares_nothing(self):
        self.assertEqual(project_phases.detect_setup_command(self.root), "")
        self.assertEqual(project_phases.detect_verify_command(self.root), "")

    def test_the_manifests_decide_the_setup_command(self):
        self._write("requirements.txt", "requests\n")
        self.assertIn("pip install", project_phases.detect_setup_command(self.root))

        os.remove(os.path.join(self.root, "requirements.txt"))
        self._write("package.json", "{}")
        self.assertIn("npm install", project_phases.detect_setup_command(self.root))

    def test_a_test_script_is_what_makes_a_javascript_suite_a_suite(self):
        self._write("package.json", json.dumps({"name": "x"}))
        self.assertEqual(project_phases.detect_verify_command(self.root), "")

        self._write("package.json", json.dumps({"name": "x", "scripts": {"test": "jest"}}))
        self.assertIn("npm test", project_phases.detect_verify_command(self.root))

    def test_a_python_project_verifies_with_pytest(self):
        self._write("pytest.ini", "[pytest]\n")
        self.assertIn("pytest", project_phases.detect_verify_command(self.root))

    def test_the_declaration_wins_over_the_table(self):
        """A human's file decides, because the table is a guess and this is not."""
        self._write("requirements.txt", "requests\n")
        self._write(project_phases.DECLARATION_FILE, json.dumps({
            "setup": "make deps",
            "verify": "make check",
        }))
        self.assertEqual(project_phases.detect_setup_command(self.root), "make deps")
        self.assertEqual(project_phases.detect_verify_command(self.root), "make check")

    def test_a_malformed_declaration_falls_back_rather_than_raising(self):
        self._write(project_phases.DECLARATION_FILE, "{ not json")
        self._write("requirements.txt", "requests\n")
        self.assertIn("pip install", project_phases.detect_setup_command(self.root))

    def test_a_project_with_nothing_to_run_returns_none(self):
        self.assertIsNone(project_phases.run_setup(self.root))
        self.assertIsNone(project_phases.run_verification(self.root))


class PhaseNetworkTests(unittest.TestCase):
    """The two phases must not share a network boundary.

    Asserted on the *argv*, through the in-repo runtime double, so the contract is checked without a
    daemon and the container's own behaviour is checked on the legs that have one.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="aleth_phases_net_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.log = os.path.join(self.root, "docker.log")
        self._write("requirements.txt", "requests\n")
        self._write("pytest.ini", "[pytest]\n")

    def _write(self, name, text):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)

    def _argv(self):
        """The ``docker run`` argv the double was called with, or ``[]``.

        The log holds every invocation, and the first is the image probe ``ensure_image`` makes
        before the run itself.
        """
        if not os.path.isfile(self.log):
            return []
        with open(self.log, "r", encoding="utf-8") as handle:
            # The double writes one argv list per invocation.
            runs = [json.loads(line) for line in handle if line.strip()]
        return next((argv for argv in runs if "run" in argv), [])

    def test_setup_runs_with_egress(self):
        with mock.patch.dict(os.environ, fake_docker_env(FAKE_DOCKER_LOG=self.log)):
            project_phases.run_setup(self.root)
        argv = self._argv()
        self.assertIn(f"--network={docker_sandbox.SETUP_NETWORK}", argv)
        self.assertIn("--network=bridge", argv)

    def test_verification_runs_airgapped(self):
        with mock.patch.dict(os.environ, fake_docker_env(FAKE_DOCKER_LOG=self.log)):
            project_phases.run_verification(self.root)
        argv = self._argv()
        self.assertIn("--network=none", argv)
        self.assertNotIn("--network=bridge", argv)

    def test_the_model_facing_runner_cannot_ask_for_egress(self):
        """The boundary the whole design rests on: the *tool* surface has no network argument.

        ``build_command`` accepts one -- the setup phase needs it -- so the guarantee is not "no
        parameter exists" but "the code the model reaches never passes it". That is asserted here,
        and behaviourally in ``test_chaos`` against a live daemon.
        """
        import inspect

        from tools import mcp_exec_server

        for fn in (mcp_exec_server.run_workspace_command,
                   mcp_exec_server.run_workspace_command_result,
                   mcp_exec_server.ExecServer.__init__):
            self.assertNotIn("network", inspect.signature(fn).parameters, fn.__name__)
        # ...and the exec server's tool path really produces a sealed argv.
        with mock.patch.dict(os.environ, fake_docker_env(FAKE_DOCKER_LOG=self.log)):
            mcp_exec_server.run_workspace_command("echo hi", root=self.root)
        self.assertIn("--network=none", self._argv())

    def test_an_unknown_network_is_refused(self):
        with mock.patch.dict(os.environ, fake_docker_env()):
            with self.assertRaises(ValueError):
                docker_sandbox.build_command(
                    "echo hi", root=self.root, name="probe", network="host"
                )


if __name__ == "__main__":
    unittest.main()
