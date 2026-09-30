"""Phase 10: chaos engineering -- the perimeter under a hostile child.

These are the tests that prove the *mechanism* rather than the happy path: a child that floods
its pipe, a child that forks a tree and refuses to die, a child that should not be able to read
the host's credentials, and the file descriptors each execution must give back.

Everything that needs POSIX primitives (``killpg``, ``/proc``) skips elsewhere rather than
asserting something weaker: a Windows run cannot prove a process-group guarantee.
"""

import os
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools import docker_sandbox, env_sanitizer, process_control, stream_drain
from tools.mcp_client import MCPClient

IS_POSIX = os.name == "posix"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_DOCKER = os.path.join(REPO_ROOT, "tests", "fake_docker.py")


def fake_docker_env(**extra):
    env = {"ALETH_DOCKER_BIN": f"{sys.executable} {FAKE_DOCKER}"}
    env.update(extra)
    return env


def _invocations(log_path):
    """Every argv the runtime double was called with, in order."""
    if not os.path.isfile(log_path):
        return []
    with open(log_path, "r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _container_names(log_path):
    """The ``--name`` of every ``run`` the double was asked for."""
    return [
        call[call.index("--name") + 1]
        for call in _invocations(log_path)
        if call and call[0] == "run" and "--name" in call
    ]


class StreamDrainTests(unittest.TestCase):
    """The bounded window itself, without a process in the way."""

    def test_the_window_keeps_both_ends_and_drops_the_middle(self):
        buffer = stream_drain.DualBuffer()
        buffer.feed(b"H" * stream_drain.HEAD_BYTES)
        buffer.feed(b"M" * (5 * 1024 * 1024))
        buffer.feed(b"T" * stream_drain.TAIL_BYTES)

        self.assertLessEqual(buffer.retained, stream_drain.LIMIT_BYTES)
        self.assertTrue(buffer.truncated)
        self.assertEqual(buffer.dropped, 5 * 1024 * 1024)

        receipt = buffer.render()
        self.assertTrue(receipt.startswith(b"H" * 16))
        self.assertTrue(receipt.endswith(b"T" * 16))
        self.assertIn(b"TRUNCATED BY ALETH ENGINE", receipt)
        self.assertIn(str(5 * 1024 * 1024).encode(), receipt)

    def test_a_small_stream_is_kept_verbatim(self):
        buffer = stream_drain.DualBuffer()
        buffer.feed(b"hello world")
        self.assertFalse(buffer.truncated)
        self.assertEqual(buffer.render(), b"hello world")

    def test_a_single_chunk_larger_than_the_window_is_still_capped(self):
        buffer = stream_drain.DualBuffer()
        buffer.feed(b"z" * (20 * 1024 * 1024))
        self.assertLessEqual(buffer.retained, stream_drain.LIMIT_BYTES)
        self.assertTrue(buffer.truncated)


class PipeStufferTests(unittest.TestCase):
    """A child that writes far more than the OS pipe buffer must not deadlock the parent."""

    def test_pipe_stuffer_does_not_deadlock(self):
        """20 MB through stdio: the run completes, and the parent's memory stays bounded.

        Reading only *after* the child exits would block forever once the 64 KB pipe buffer
        fills; keeping everything read would grow this process by 20 MB. Both are asserted.
        """
        from tools import docker_sandbox

        root = tempfile.mkdtemp(prefix="aleth_chaos_flood_")
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))

        with mock.patch.dict(os.environ, fake_docker_env(FAKE_DOCKER_FLOOD_MB="20")):
            started = time.monotonic()
            result = docker_sandbox.run_isolated("echo ignored", cwd=root, timeout=120)
            elapsed = time.monotonic() - started

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(elapsed, 120, "the run did not complete -- the parent deadlocked")

        # The receipt has both ends and names the drop.
        self.assertIn("HEAD-MARKER", result.stdout)
        self.assertIn("TAIL-MARKER", result.stdout)
        self.assertIn("TRUNCATED BY ALETH ENGINE", result.stdout)
        # Bounded memory: 20 MB written, at most 1 MB retained (plus the marker).
        self.assertLess(len(result.stdout), stream_drain.LIMIT_BYTES + 4096)
        self.assertLess(len(result.stderr), stream_drain.LIMIT_BYTES + 4096)


class ZombieEradicationTests(unittest.TestCase):
    """A child's whole process group dies with it -- no orphans, no ``defunct`` entries."""

    @unittest.skipUnless(IS_POSIX, "process groups are a POSIX primitive")
    def test_zombie_group_eradication(self):
        """A shell forks five background sleeps; killing the group leaves none behind."""
        script = "for i in 1 2 3 4 5; do sleep 300 & done; sleep 300"
        process = subprocess.Popen(
            ["/bin/sh", "-c", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **process_control.spawn_kwargs(),
        )
        pgid = process.pid  # the child is the group leader, so pid == pgid
        try:
            # Let the shell fork its five children.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not self._group_size(pgid) >= 6:
                time.sleep(0.05)

            self.assertGreaterEqual(self._group_size(pgid), 2, "the shell forked nothing")

            process_control.terminate_group(process, grace=1.0)
            self.assertIsNotNone(process.poll(), "the child was not reaped")

            # The grandchildren are reparented to init, and init reaps them asynchronously, so a
            # group can look alive for a moment after the kill. Poll for the meaningful state --
            # no member left -- rather than sampling once and racing the reaper.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and self._group_size(pgid) > 0:
                time.sleep(0.05)
            self.assertEqual(
                self._group_size(pgid), 0,
                f"{self._group_size(pgid)} process(es) survived the group kill",
            )
        finally:
            process_control.kill_group(process)

    @staticmethod
    def _group_size(pgid: int) -> int:
        """How many processes are in ``pgid``. 0 when the group is gone."""
        if not IS_POSIX:
            return 0
        try:
            listing = subprocess.run(
                ["ps", "-o", "pgid=", "-g", str(pgid)],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return 0
        return len([line for line in listing.stdout.splitlines() if line.strip()])

    @unittest.skipUnless(IS_POSIX, "process groups are a POSIX primitive")
    def test_a_timed_out_run_leaves_no_process_in_the_group(self):
        """Through the real path: the timeout kill ends the client's group, not just the client.

        Asserted against *this run's* container name rather than "no double anywhere": the suite
        shares a process table, and a global assertion would report another test's leak as this
        test's failure. That leak was real when this test first ran -- killing an MCP session
        mid-command orphaned that command's runtime client, because the client is deliberately in
        its own session -- and it is closed by the exec server's signal handler
        (:func:`tools.mcp_exec_server.install_container_reaper`), which
        :class:`ContainerLifecycleTests` now proves.
        """
        from tools import docker_sandbox

        root = tempfile.mkdtemp(prefix="aleth_chaos_timeout_")
        log = os.path.join(root, "docker.log")
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))

        with mock.patch.dict(
            os.environ, fake_docker_env(FAKE_DOCKER_SLEEP="120", FAKE_DOCKER_LOG=log)
        ):
            started = time.monotonic()
            result = docker_sandbox.run_isolated("echo ignored", cwd=root, timeout=2)
            elapsed = time.monotonic() - started

        self.assertTrue(result.timed_out)
        self.assertLess(elapsed, 60, "the timeout path did not return promptly")

        names = _container_names(log)
        self.assertEqual(len(names), 1, f"expected one run, got {names!r}")
        self.assertEqual(
            self._survivors(name=names[0]), [],
            "the runtime client for this run outlived its group kill",
        )

    @staticmethod
    def _survivors(name=None):
        """Runtime-double processes still alive, optionally filtered to one container name."""
        if not IS_POSIX:
            return []
        try:
            listing = subprocess.run(
                ["ps", "-eo", "pid,args"], capture_output=True, text=True, timeout=10
            )
        except (OSError, subprocess.SubprocessError):
            return []
        alive = [
            line.strip() for line in listing.stdout.splitlines()
            if "fake_docker.py" in line and "ps -eo" not in line
        ]
        if name is None:
            return alive
        return [line for line in alive if name in line]


class EnvironmentIsolationTests(unittest.TestCase):
    """The host's credentials must not reach a child, and the MCP spawn must prove it."""

    def test_a_child_cannot_see_the_host_credentials(self):
        """Query a real child's environment: the key is undefined inside it."""
        hostile = {
            "OPENAI_API_KEY": "sk-live-secret",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "AWS_ACCESS_KEY_ID": "aws-id",
            "GITHUB_TOKEN": "gh-token",
            "PATH": os.environ.get("PATH", ""),
        }
        with mock.patch.dict(os.environ, hostile):
            completed = subprocess.run(
                [sys.executable, "-c",
                 "import os; print('OPENAI_API_KEY' in os.environ,"
                 " 'AWS_SECRET_ACCESS_KEY' in os.environ,"
                 " 'AWS_ACCESS_KEY_ID' in os.environ,"
                 " 'GITHUB_TOKEN' in os.environ)"],
                env=env_sanitizer.sanitized_environment(),
                capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(completed.stdout.strip(), "False False False False", completed.stderr)

    def test_environment_isolation(self):
        """The MCP spawn hands the child an allow-list, not ``os.environ``."""
        captured = {}

        def refuse(command, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("stop before spawning")

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-live-secret"}), \
                mock.patch("tools.mcp_client.subprocess.Popen", side_effect=refuse):
            with self.assertRaises(RuntimeError):
                MCPClient([sys.executable, "-c", "pass"]).start()

        env = captured["env"]
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertEqual(env_sanitizer.leaked_secret_names(env), [])
        # The child still gets what it needs to run at all.
        self.assertIn("PATH", env)
        self.assertEqual(env["PYTHONPATH"], REPO_ROOT)
        # And the spawn puts it in its own group.
        self.assertTrue(captured.get("start_new_session"))

    def test_the_sanitizer_names_what_it_withheld_without_naming_values(self):
        hostile = {"OPENAI_API_KEY": "sk-live-secret", "PATH": "/usr/bin"}
        dropped = env_sanitizer.stripped_names(hostile)
        self.assertIn("OPENAI_API_KEY", dropped)
        self.assertNotIn("PATH", dropped)
        self.assertNotIn("sk-live-secret", env_sanitizer.describe(hostile))

    def test_a_hand_built_environment_with_a_secret_is_refused(self):
        """The fail-loud half: a call site that forgets the sanitizer is caught."""
        with self.assertRaises(ValueError):
            env_sanitizer.ensure_sanitized({"PATH": "/usr/bin", "OPENAI_API_KEY": "sk-live"})


class FileDescriptorTests(unittest.TestCase):
    """Every execution gives its descriptors back."""

    @staticmethod
    def _open_fds() -> int:
        return len(os.listdir("/proc/self/fd"))

    @unittest.skipUnless(IS_POSIX, "/proc is a Linux interface")
    def test_file_descriptor_leakage(self):
        """100 rapid executions must not grow the descriptor table."""
        from tools import docker_sandbox

        root = tempfile.mkdtemp(prefix="aleth_chaos_fd_")
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))

        with mock.patch.dict(os.environ, fake_docker_env(FAKE_DOCKER_STDOUT="ok\n")):
            # Warm up first: the first run loads modules and opens a socket, which is not a leak.
            docker_sandbox.run_isolated("echo warm", cwd=root, timeout=60)
            before = self._open_fds()
            for _ in range(100):
                docker_sandbox.run_isolated("echo run", cwd=root, timeout=60)
            after = self._open_fds()

        self.assertEqual(after, before, f"descriptor table grew: {before} -> {after}")


def _real_runtime_available() -> bool:
    """A live daemon reached through the real client (no test double).

    These tests assert on the container *table*, so a double cannot stand in: they are about the
    lifecycle the daemon owns.
    """
    if os.environ.get("ALETH_DOCKER_BIN"):
        return False
    try:
        from tools import docker_sandbox

        return docker_sandbox.available()
    except Exception:
        return False


@unittest.skipUnless(_real_runtime_available(), "no live Docker daemon")
class ContainerLifecycleTests(unittest.TestCase):
    """A container is accountable: it is labelled, and its server takes it with it."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="aleth_chaos_lifecycle_")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        # A leftover from a failed assertion must not become the next test's pollution.
        self.addCleanup(lambda: docker_sandbox.purge_orphaned_containers())

    def test_the_container_carries_the_management_labels(self):
        argv = docker_sandbox.build_command(
            "echo hi", root=self.root, name="probe", execution_id="abc123"
        )
        labels = [argv[index + 1] for index, token in enumerate(argv) if token == "--label"]
        self.assertIn("aleth.managed=true", labels)
        self.assertIn("aleth.execution_id=abc123", labels)
        self.assertTrue(
            any(label.startswith("aleth.owner_pid=") for label in labels),
            f"no owner label: {labels}",
        )

    def test_mcp_server_sigterm_cleans_running_container(self):
        """A SIGTERM to the exec server removes the container it has in flight.

        Without the handler the container outlives its server: the server was the only thing that
        knew its name, so nothing else will ever remove it.
        """
        import threading

        from tools.mcp_client import MCPClient, default_exec_command

        client = MCPClient(default_exec_command(self.root), timeout=30)
        client.start()
        client.initialize()
        try:
            def _call():
                try:
                    client.call_tool("execute_command", {"command": "sleep 100"})
                except Exception:
                    pass  # the server is killed underneath it; that is the point

            threading.Thread(target=_call, daemon=True).start()

            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not docker_sandbox.managed_containers():
                time.sleep(0.2)
            self.assertTrue(
                docker_sandbox.managed_containers(), "no managed container appeared"
            )

            # SIGTERM first (then SIGKILL after the grace period): the handler's one chance.
            client.close()

            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and docker_sandbox.managed_containers():
                time.sleep(0.1)
            self.assertEqual(
                docker_sandbox.managed_containers(), [],
                "the container outlived the server that started it",
            )
        finally:
            client.close()

    @unittest.skipUnless(IS_POSIX, "/proc is a Linux interface")
    def test_the_sweeper_removes_a_container_whose_owner_is_gone(self):
        """The boot/teardown sweep: a dead owner is the definition of an orphan."""
        name = "aleth-chaos-orphan"
        created = subprocess.run(
            [*docker_sandbox.docker_bin(), "run", "-d", "--rm", "--name", name,
             "--label", "aleth.managed=true",
             "--label", "aleth.owner_pid=999999999",  # a pid that cannot exist
             docker_sandbox.image(), "/bin/sh", "-c", "sleep 300"],
            capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        self.addCleanup(lambda: docker_sandbox.remove_container(name))

        removed = docker_sandbox.purge_orphaned_containers()
        self.assertTrue(removed, "the orphan was not swept")
        self.assertEqual(self._by_name(name), [], "the orphan is still in the container table")

    @staticmethod
    def _by_name(name: str):
        listing = subprocess.run(
            [*docker_sandbox.docker_bin(), "ps", "-a", "--filter", f"name={name}",
             "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=60,
        )
        return [line for line in listing.stdout.splitlines() if line.strip()]


if __name__ == "__main__":
    unittest.main()
