"""Tests for the container perimeter (``tools/docker_sandbox.py``).

These drive the real code path: :func:`~tools.docker_sandbox.run_isolated` builds the full
``docker run`` argv and spawns a real child process. On a machine with no Docker daemon the
runtime at the far end is ``tests/fake_docker.py`` -- a double that runs the payload in the bind
mount's source directory and hands back its streams and exit status, exactly as a container
would. So the argv contract, the output/exit plumbing, the timeout-and-force-remove and the
fail-closed refusal are all exercised for real; only the container itself is emulated.

The tests that need an actual container are skipped unless a daemon answers *and* the configured
image is already present locally (so the suite never silently pulls over the network). They are
expected to run in the same environment as the daemon -- see the module docstring of
``tools/docker_sandbox.py`` for why the perimeter refuses to bridge an OS boundary.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import docker_sandbox
from tools.mcp_exec_server import run_workspace_command

IS_WINDOWS = sys.platform == "win32"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_DOCKER = os.path.join(REPO_ROOT, "tests", "fake_docker.py")


def fake_docker_env(**extra):
    """The environment that points the perimeter at the in-repo double."""
    env = {"ALETH_DOCKER_BIN": f"{sys.executable} {FAKE_DOCKER}"}
    env.update(extra)
    return env


def _invocations(log_path):
    """Every argv the double was called with, in order."""
    if not os.path.isfile(log_path):
        return []
    with open(log_path, "r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _daemon_available():
    """Whether a real container can be run here. Evaluated once, at import."""
    return docker_sandbox.available()


def _image_present():
    """Whether the configured image is already local, so a test never triggers a pull."""
    if not _daemon_available():
        return False
    try:
        completed = subprocess.run(
            [*docker_sandbox.docker_bin(), "image", "inspect", docker_sandbox.image()],
            capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


class CommandContractTests(unittest.TestCase):
    """The argv *is* the isolation contract, and it is asserted without any daemon."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="docker_sandbox_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        # Pin the runtime so the contract is asserted against a known argv, not whatever client
        # this machine happens to have.
        env_patcher = mock.patch.dict(os.environ, fake_docker_env())
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        docker_sandbox.reset_caches()

    def _argv(self, command="echo hi", **kwargs):
        return docker_sandbox.build_command(command, root=self.root, name="probe", **kwargs)

    @staticmethod
    def _flag(argv, flag):
        return argv[argv.index(flag) + 1]

    def test_the_network_is_denied(self):
        self.assertIn("--network=none", self._argv())

    def test_the_payload_runs_as_the_host_user_not_root(self):
        argv = self._argv()
        uid, gid = docker_sandbox.host_identity()
        self.assertEqual(self._flag(argv, "--user"), f"{uid}:{gid}")
        self.assertNotEqual(uid, "0")
        self.assertNotEqual(gid, "0")

    def test_the_workspace_is_the_only_bind_mount_and_the_working_directory(self):
        argv = self._argv()
        resolved = str(Path(self.root).resolve())
        self.assertEqual(self._flag(argv, "--workdir"), docker_sandbox.WORKSPACE_MOUNT)
        self.assertEqual(
            self._flag(argv, "--mount"),
            f"type=bind,source={resolved},target={docker_sandbox.WORKSPACE_MOUNT}",
        )
        # Exactly one host path is mounted; nothing else from the host is visible.
        self.assertEqual(argv.count("--mount"), 1)

    def test_the_workspace_path_is_used_as_written(self):
        """No translation: the path is a path in the daemon's own filesystem."""
        self.assertEqual(
            self._flag(self._argv(), "--mount"),
            f"type=bind,source={str(Path(self.root).resolve())},"
            f"target={docker_sandbox.WORKSPACE_MOUNT}",
        )

    def test_memory_cpu_and_the_process_tree_are_capped(self):
        argv = self._argv()
        self.assertEqual(self._flag(argv, "--memory"), f"{docker_sandbox.DEFAULT_MEMORY_MB}m")
        self.assertEqual(self._flag(argv, "--memory-swap"), f"{docker_sandbox.DEFAULT_MEMORY_MB}m")
        self.assertEqual(self._flag(argv, "--cpus"), str(docker_sandbox.DEFAULT_CPUS))
        self.assertEqual(self._flag(argv, "--pids-limit"), str(docker_sandbox.DEFAULT_MAX_PROCESSES))
        self.assertEqual(self._flag(argv, "--cap-drop"), "ALL")
        self.assertEqual(self._flag(argv, "--security-opt"), "no-new-privileges")
        self.assertIn("--rm", argv)
        self.assertEqual(self._flag(argv, "--name"), "probe")
        # The default budget is small on purpose: a bound that is never hit costs nothing, and a
        # bound that is too generous is the vulnerability.
        self.assertLessEqual(docker_sandbox.DEFAULT_MEMORY_MB, 512)

    def test_the_budget_cannot_be_widened_past_the_ceiling(self):
        """No caller -- and nothing a model can say -- asks for an unbounded container."""
        argv = self._argv(memory_mb=10 ** 9, cpus=10 ** 3)
        self.assertEqual(self._flag(argv, "--memory"), f"{docker_sandbox.MAX_MEMORY_MB}m")
        self.assertEqual(self._flag(argv, "--memory-swap"), f"{docker_sandbox.MAX_MEMORY_MB}m")
        self.assertEqual(self._flag(argv, "--cpus"), str(docker_sandbox.MAX_CPUS))

    def test_every_profile_is_a_bounded_budget(self):
        for name, budget in docker_sandbox.RESOURCE_PROFILES.items():
            self.assertEqual(docker_sandbox.resource_limits(name), budget, name)
            self.assertLessEqual(budget[0], docker_sandbox.MAX_MEMORY_MB, name)
            self.assertLessEqual(budget[1], docker_sandbox.MAX_CPUS, name)
        # An unknown name is the baseline, never an unbounded one.
        self.assertEqual(
            docker_sandbox.resource_limits("does-not-exist"),
            (docker_sandbox.DEFAULT_MEMORY_MB, docker_sandbox.DEFAULT_CPUS),
        )

    def test_no_host_environment_reaches_the_container(self):
        argv = self._argv()
        for flag in ("-e", "--env", "--env-file"):
            self.assertNotIn(flag, argv, f"{flag} would leak host environment into the payload")

    def test_the_payload_is_the_container_command(self):
        self.assertEqual(self._argv("echo hi")[-3:], ["/bin/sh", "-c", "echo hi"])

    def test_the_image_is_configurable(self):
        with mock.patch.dict(os.environ, {docker_sandbox.IMAGE_ENV: "busybox:1.36"}):
            argv = self._argv()
            self.assertEqual(docker_sandbox.image(), "busybox:1.36")
        self.assertEqual(argv[-4], "busybox:1.36")


class IdentityTests(unittest.TestCase):
    def test_the_identity_is_the_hosts_own_when_the_platform_has_one(self):
        if not hasattr(os, "getuid"):
            self.skipTest("no POSIX identity on this platform")
        with mock.patch.dict(os.environ, {docker_sandbox.UID_ENV: "4242"}):
            # A real uid/gid wins over the override: it is the host's own identity that matters.
            self.assertEqual(docker_sandbox.host_identity(), (str(os.getuid()), str(os.getgid())))

    @unittest.skipUnless(IS_WINDOWS, "Windows has no POSIX identity")
    def test_a_platform_without_a_posix_identity_uses_the_configured_one(self):
        with mock.patch.dict(
            os.environ, {docker_sandbox.UID_ENV: "1234", docker_sandbox.GID_ENV: "5678"}
        ):
            self.assertEqual(docker_sandbox.host_identity(), ("1234", "5678"))

    @unittest.skipUnless(IS_WINDOWS, "Windows has no POSIX identity")
    def test_the_default_identity_is_unprivileged(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(docker_sandbox.host_identity(), ("1000", "1000"))


class RuntimeCommandTests(unittest.TestCase):
    def setUp(self):
        docker_sandbox.reset_caches()
        self.root = tempfile.mkdtemp(prefix="docker_sandbox_runtime_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_docker_on_the_path_is_the_runtime(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("shutil.which", return_value="/usr/bin/docker"):
            self.assertEqual(docker_sandbox.docker_bin(), ["docker"])

    def test_a_missing_client_is_a_fatal_environment_error(self):
        """The perimeter does not proxy across an OS boundary or fall back to the host."""
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("shutil.which", return_value=None):
            with self.assertRaises(EnvironmentError) as caught:
                docker_sandbox.docker_bin()
        self.assertIn("not on PATH", str(caught.exception))

    def test_a_missing_client_refuses_the_command_at_the_tool_boundary(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("shutil.which", return_value=None):
            with self.assertRaises(docker_sandbox.SandboxError) as caught:
                docker_sandbox.run_isolated("echo hi", cwd=self.root, timeout=5)
        self.assertIn("not on PATH", str(caught.exception))

    def test_a_missing_client_is_reported_as_no_containment(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("shutil.which", return_value=None):
            self.assertFalse(docker_sandbox.available())
            self.assertFalse(docker_sandbox.capabilities().containment)

    def test_the_runtime_is_overridable(self):
        with mock.patch.dict(os.environ, {docker_sandbox.DOCKER_BIN_ENV: "podman"}):
            self.assertEqual(docker_sandbox.docker_bin(), ["podman"])

    def test_a_command_line_override_survives_the_split(self):
        value = f"{sys.executable} {FAKE_DOCKER}"
        with mock.patch.dict(os.environ, {docker_sandbox.DOCKER_BIN_ENV: value}):
            self.assertEqual(docker_sandbox.docker_bin(), [sys.executable, FAKE_DOCKER])

    @unittest.skipUnless(IS_WINDOWS, "the Windows path rules")
    def test_a_quoted_windows_path_with_spaces_stays_one_token(self):
        tokens = docker_sandbox._split_command('"C:\\Program Files\\py.exe" "C:\\x\\fake.py"')
        self.assertEqual(tokens, ["C:\\Program Files\\py.exe", "C:\\x\\fake.py"])

    @unittest.skipUnless(IS_WINDOWS, "the Windows path rules")
    def test_an_unquoted_windows_path_is_not_mangled(self):
        tokens = docker_sandbox._split_command(f"{sys.executable} {FAKE_DOCKER}")
        self.assertEqual(tokens, [sys.executable, FAKE_DOCKER])


class AvailabilityTests(unittest.TestCase):
    def test_availability_requires_an_answer_not_just_a_zero_status(self):
        """A down daemon can exit 0 with the diagnostic on stderr; that is not available."""
        with mock.patch.dict(os.environ, fake_docker_env()):
            self.assertTrue(docker_sandbox.available())
        with mock.patch.dict(os.environ, fake_docker_env(FAKE_DOCKER_INFO_EMPTY="1")):
            self.assertFalse(docker_sandbox.available())

    def test_availability_is_false_when_the_runtime_cannot_be_started(self):
        with mock.patch.object(
            docker_sandbox, "docker_bin", return_value=["definitely-not-a-real-runtime-xyz"]
        ):
            self.assertFalse(docker_sandbox.available())

    def test_capabilities_report_the_container_perimeter(self):
        with mock.patch.dict(os.environ, fake_docker_env()):
            caps = docker_sandbox.capabilities()
        self.assertTrue(caps.containment)
        # The container really does deny the network, unlike the old in-process perimeter.
        self.assertTrue(caps.network_denied)
        self.assertTrue(caps.uid_mapping)
        self.assertIn("docker", caps.describe())

    def test_the_image_contract_is_reported(self):
        with mock.patch.dict(os.environ, fake_docker_env()):
            self.assertTrue(docker_sandbox.image_present(docker_sandbox.image()))
            self.assertTrue(docker_sandbox.capabilities().image_present)

    def test_an_absent_image_is_reported_as_absent(self):
        docker_sandbox.reset_caches()
        with mock.patch.dict(os.environ, fake_docker_env(FAKE_DOCKER_IMAGE_MISSING="1")):
            self.assertFalse(docker_sandbox.image_present("nothing:here"))
        docker_sandbox.reset_caches()


class IsolatedRunTests(unittest.TestCase):
    """The full path, against the double: argv out, streams and status back."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="docker_sandbox_run_")
        self.log = os.path.join(self.root, "docker.log")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _run(self, command, timeout=30, **env):
        with mock.patch.dict(os.environ, fake_docker_env(FAKE_DOCKER_LOG=self.log, **env)):
            return docker_sandbox.run_isolated(command, cwd=self.root, timeout=timeout)

    def test_a_command_runs_and_reports_its_output(self):
        result = self._run("echo isolated-ok")
        self.assertTrue(result.sandboxed)
        self.assertEqual(result.returncode, 0)
        self.assertIn("isolated-ok", result.stdout)

    def test_a_nonzero_exit_code_is_propagated(self):
        self.assertEqual(self._run("exit 3").returncode, 3)

    def test_the_isolation_flags_reach_the_runtime(self):
        self._run("echo hi")
        # The runtime is asked about the image first, so the log holds more than the run.
        runs = [call for call in _invocations(self.log) if call and call[0] == "run"]
        self.assertEqual(len(runs), 1)
        argv = runs[0]
        self.assertIn("--network=none", argv)
        self.assertIn("--user", argv)
        self.assertEqual(argv[-3:], ["/bin/sh", "-c", "echo hi"])

    def test_a_timeout_force_removes_the_container(self):
        """The container owns its process tree, so killing the client is not enough."""
        result = self._run("echo hi", timeout=1, FAKE_DOCKER_SLEEP="5")
        self.assertTrue(result.timed_out)

        calls = _invocations(self.log)
        run = next(call for call in calls if call and call[0] == "run")
        name = run[run.index("--name") + 1]
        removals = [call for call in calls if call[:2] == ["rm", "-f"]]
        self.assertEqual(removals, [["rm", "-f", name]], "the container was not force-removed")

    def test_a_daemon_that_cannot_be_reached_is_refused(self):
        with self.assertRaises(docker_sandbox.SandboxError) as caught:
            self._run("echo should-not-run", FAKE_DOCKER_DAEMON_DOWN="1")
        self.assertIn("could not be reached", str(caught.exception))

    def test_a_missing_runtime_is_refused_rather_than_run_natively(self):
        with mock.patch.object(
            docker_sandbox, "docker_bin", return_value=["definitely-not-a-real-runtime-xyz"]
        ):
            with self.assertRaises(docker_sandbox.SandboxError):
                docker_sandbox.run_isolated("echo hi", cwd=self.root, timeout=5)

    def test_a_missing_workspace_is_refused(self):
        with self.assertRaises(docker_sandbox.SandboxError):
            docker_sandbox.run_isolated("echo hi", cwd=os.path.join(self.root, "nope"), timeout=5)


class ExecServerRefusalTests(unittest.TestCase):
    """The exec server's tool contract: refuse, never degrade to the host."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="docker_sandbox_exec_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_the_command_runs_inside_the_container(self):
        with mock.patch.dict(os.environ, fake_docker_env()):
            output = run_workspace_command("echo in-container", root=self.root)
        self.assertTrue(output.startswith("[Exit Code: 0]"), output)
        self.assertIn("in-container", output)

    def test_a_refusal_is_reported_instead_of_run(self):
        with mock.patch.dict(os.environ, fake_docker_env(FAKE_DOCKER_DAEMON_DOWN="1")):
            output = run_workspace_command("echo should-not-run", root=self.root)
        self.assertIn("refused", output)
        self.assertIn("container isolation is required", output)
        self.assertNotIn("[Exit Code:", output)

    def test_the_refusal_offers_no_native_escape_hatch(self):
        with mock.patch.dict(os.environ, fake_docker_env(FAKE_DOCKER_DAEMON_DOWN="1")):
            output = run_workspace_command("echo x", root=self.root)
        self.assertNotIn("ALETH_SANDBOX=auto", output)
        self.assertNotIn("ALETH_SANDBOX=off", output)
        self.assertIn("never run on the host", output)


class LegacyPerimeterRemovedTests(unittest.TestCase):
    """The batch's deletion gate, made durable: the native perimeter must not come back."""

    def test_the_in_process_sandbox_module_is_gone(self):
        self.assertFalse(
            os.path.isfile(os.path.join(REPO_ROOT, "tools", "sandbox.py")),
            "tools/sandbox.py was replaced by the container perimeter",
        )

    def test_the_exec_server_no_longer_reaches_for_it(self):
        with open(
            os.path.join(REPO_ROOT, "tools", "mcp_exec_server.py"), "r", encoding="utf-8"
        ) as handle:
            source = handle.read()
        self.assertIn("docker_sandbox.run_isolated", source)
        self.assertNotIn("from tools import sandbox", source)

    def test_the_perimeter_does_not_bridge_an_os_boundary(self):
        """No proxy, no path translation: the orchestrator runs where the daemon runs.

        The module *documents* why (it names WSL to explain the refusal), so this asserts the
        absence of the machinery -- no bridge helper, no ``wslpath`` call -- and the presence of
        the fatal error.
        """
        with open(
            os.path.join(REPO_ROOT, "tools", "docker_sandbox.py"), "r", encoding="utf-8"
        ) as handle:
            source = handle.read()
        self.assertNotIn("def _wsl", source)
        self.assertNotIn("wslpath", source)
        self.assertNotIn("ALETH_DOCKER_WSL_DISTRO", source)
        self.assertIn("raise EnvironmentError", source)


class ImageContractTests(unittest.TestCase):
    """The image contract: the sandbox tag, and a build only for that tag."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="docker_sandbox_image_")
        self.log = os.path.join(self.root, "docker.log")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        docker_sandbox.reset_caches()
        self.addCleanup(docker_sandbox.reset_caches)

    def test_the_default_image_is_the_sandbox_tag(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(docker_sandbox.image(), "aleth-sandbox:latest")

    def test_a_present_image_is_used_as_it_is(self):
        with mock.patch.dict(os.environ, fake_docker_env()):
            self.assertEqual(docker_sandbox.ensure_image(), "aleth-sandbox:latest")

    def test_an_absent_sandbox_image_is_built_once(self):
        with mock.patch.dict(
            os.environ,
            fake_docker_env(FAKE_DOCKER_IMAGE_MISSING="1", FAKE_DOCKER_LOG=self.log),
        ):
            self.assertEqual(docker_sandbox.ensure_image(), "aleth-sandbox:latest")

        builds = [call for call in _invocations(self.log) if call and call[0] == "build"]
        self.assertEqual(len(builds), 1, "the sandbox image was not built exactly once")
        self.assertIn("aleth-sandbox:latest", builds[0])
        # The build is driven from the repo's Dockerfile, not from a guessed context.
        self.assertIn("-f", builds[0])
        self.assertIn(str(docker_sandbox.DOCKERFILE), builds[0])

    def test_a_failed_build_is_a_refusal(self):
        with mock.patch.dict(
            os.environ,
            fake_docker_env(FAKE_DOCKER_IMAGE_MISSING="1", FAKE_DOCKER_BUILD_FAIL="1"),
        ):
            with self.assertRaises(docker_sandbox.SandboxError) as caught:
                docker_sandbox.ensure_image()
        self.assertIn("could not be built", str(caught.exception))

    def test_an_absent_custom_image_is_an_error_not_a_build(self):
        """The repo's Dockerfile builds the sandbox image and nothing else."""
        env = {**fake_docker_env(FAKE_DOCKER_IMAGE_MISSING="1"),
               docker_sandbox.IMAGE_ENV: "mine:1"}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(docker_sandbox.SandboxError) as caught:
                docker_sandbox.ensure_image()
        self.assertIn("mine:1", str(caught.exception))

    def test_a_run_refuses_when_the_image_cannot_be_provided(self):
        """The refusal reaches the command path: a missing image is never a host run."""
        with mock.patch.dict(
            os.environ,
            fake_docker_env(FAKE_DOCKER_IMAGE_MISSING="1", FAKE_DOCKER_BUILD_FAIL="1"),
        ):
            with self.assertRaises(docker_sandbox.SandboxError):
                docker_sandbox.run_isolated("echo hi", cwd=self.root, timeout=5)


@unittest.skipUnless(_image_present(), "no Docker daemon or image present")
class RealContainerTests(unittest.TestCase):
    """The real thing, end to end: a live daemon, the built image, a real bind mount.

    Skipped unless a daemon answers and the image is present. These run where the daemon runs --
    the perimeter refuses to bridge an OS boundary, so there is no remote variant to fall back
    to.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="docker_sandbox_real_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_a_command_runs_in_a_container_with_the_workspace_mounted(self):
        result = docker_sandbox.run_isolated("pwd", cwd=self.root, timeout=120)
        self.assertTrue(result.sandboxed)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(docker_sandbox.WORKSPACE_MOUNT, result.stdout)

    def test_the_container_does_not_run_as_root(self):
        result = docker_sandbox.run_isolated("id -u", cwd=self.root, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        uid, _ = docker_sandbox.host_identity()
        self.assertEqual(result.stdout.strip(), uid)
        self.assertNotEqual(result.stdout.strip(), "0")

    def test_the_network_is_denied_inside_the_container(self):
        # A container with no network cannot resolve anything; the failure is the proof.
        result = docker_sandbox.run_isolated(
            "getent hosts example.com || exit 9", cwd=self.root, timeout=120
        )
        self.assertNotEqual(result.returncode, 0)

    def test_a_file_written_in_the_container_lands_in_the_workspace(self):
        result = docker_sandbox.run_isolated(
            "printf written > from-container.txt", cwd=self.root, timeout=120
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        written = Path(self.root) / "from-container.txt"
        self.assertTrue(written.is_file(), "the bind mount did not reach the host")
        self.assertEqual(written.read_text(encoding="utf-8"), "written")

        # Zero permission corruption: the host process must still control what the container
        # wrote. Rewriting the file from the host is the proof that nothing was mangled.
        written.write_text(written.read_text(encoding="utf-8") + "!", encoding="utf-8")
        self.assertEqual(written.read_text(encoding="utf-8"), "written!")

    def test_the_written_file_is_owned_by_the_mapped_identity(self):
        """``--user <uid>:<gid>`` reaches the host filesystem, not just ``id`` inside."""
        if not hasattr(os, "getuid"):
            self.skipTest("no POSIX identity on this platform")

        result = docker_sandbox.run_isolated(
            "printf owned > owned.txt", cwd=self.root, timeout=120
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        info = os.stat(Path(self.root) / "owned.txt")
        self.assertEqual(
            (info.st_uid, info.st_gid), (os.getuid(), os.getgid()),
            "the file the container wrote is not owned by the mapped identity",
        )


class ContainerRemovalContractTests(unittest.TestCase):
    """How a container is removed, asserted without any daemon.

    The signal handler's removal is the one case where the process doing the removing is about to
    be killed, so *how* the runtime is invoked is the guarantee -- not how long it took.
    """

    def _kwargs_for(self, *, detached):
        """The kwargs the removal hands the runtime, with the runtime pinned to the double.

        ``remove_container`` resolves the client through ``docker_bin()`` first, and this machine
        has no docker client on PATH, so the env pin is what lets the call reach the spawn at all.
        """
        captured = {}

        def _run(argv, **kwargs):
            captured.update(kwargs)
            return mock.Mock(returncode=0)

        with mock.patch.dict(os.environ, fake_docker_env()), \
                mock.patch.object(docker_sandbox.subprocess, "run", _run):
            self.assertTrue(docker_sandbox.remove_container("aleth-exec-x", detached=detached))
        return captured

    def test_a_plain_removal_is_awaited_and_captured(self):
        kwargs = self._kwargs_for(detached=False)
        self.assertTrue(kwargs.get("capture_output"))
        self.assertNotIn("start_new_session", kwargs)

    def test_the_signal_paths_removal_is_detached(self):
        """It must survive the SIGKILL aimed at its parent's process group.

        Without its own session the removal is in the group the client is about to kill, so a
        removal slower than the grace period is killed mid-flight and the container outlives the
        server that was supposed to reap it.
        """
        kwargs = self._kwargs_for(detached=True)
        self.assertTrue(kwargs.get("start_new_session"))
        # No pipe: a detached child must not hold a pipe whose reader is about to die.
        self.assertEqual(kwargs.get("stdout"), subprocess.DEVNULL)
        self.assertEqual(kwargs.get("stderr"), subprocess.DEVNULL)

    def test_the_purge_passes_the_mode_through(self):
        with mock.patch.object(docker_sandbox, "_ACTIVE", {"aleth-exec-x": "eid"}), \
                mock.patch.object(docker_sandbox, "remove_container") as remove:
            remove.return_value = True
            removed = docker_sandbox.purge_active_containers(detached=True)
        remove.assert_called_once_with("aleth-exec-x", detached=True)
        self.assertEqual(removed, ["aleth-exec-x"])


if __name__ == "__main__":
    unittest.main()
