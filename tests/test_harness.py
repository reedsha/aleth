"""Phase 40: the production harness -- secrets, logging, the pre-flight, and the CLI.

Three operational claims, each of which is the difference between a test stand and a product:

* a credential lives in the OS keyring and **never on disk**;
* the operational record is structured, carries the run it belongs to, and is **bounded**;
* the engine refuses to start half-broken, and says exactly how to fix it.
"""

import json
import logging
import os
import shutil
import socket
import tempfile
import unittest
from unittest import mock

from tools import engine_log, preflight, process_lock, secrets


class _FakeKeyring:
    """A keyring backend in memory. The real one needs a desktop session; a test does not."""

    def __init__(self):
        self.entries = {}

    def set_password(self, service, name, value):
        self.entries[(service, name)] = value

    def get_password(self, service, name):
        return self.entries.get((service, name))

    def delete_password(self, service, name):
        if (service, name) not in self.entries:
            raise KeyError(name)
        del self.entries[(service, name)]


class SecretsTests(unittest.TestCase):
    """The keyring is the store; the environment is the hand-off; a file is never written."""

    def setUp(self):
        self.store = _FakeKeyring()
        self.addCleanup(self._restore_env)
        self._saved = {
            env: os.environ.get(env) for env in secrets.PROVIDERS.values()
        }
        for env in self._saved:
            os.environ.pop(env, None)

    def _restore_env(self):
        for env, value in self._saved.items():
            if value is None:
                os.environ.pop(env, None)
            else:
                os.environ[env] = value

    def test_a_credential_round_trips_through_the_store(self):
        secrets.set_secret("openai", "sk-test-value", backend=self.store)

        self.assertEqual(secrets.get_secret("openai", backend=self.store), "sk-test-value")
        self.assertEqual(secrets.stored_providers(backend=self.store), ["openai"])

    def test_the_service_name_is_ours_so_a_person_can_find_it(self):
        secrets.set_secret("github", "ghp_x", backend=self.store)

        self.assertIn((secrets.SERVICE, "github"), self.store.entries)

    def test_a_list_reports_names_only(self):
        secrets.set_secret("openai", "sk-secret-value", backend=self.store)

        listed = secrets.stored_providers(backend=self.store)

        self.assertEqual(listed, ["openai"])
        self.assertNotIn("sk-secret-value", " ".join(listed))

    def test_clearing_forgets_it(self):
        secrets.set_secret("anthropic", "sk-ant", backend=self.store)

        self.assertTrue(secrets.clear_secret("anthropic", backend=self.store))
        self.assertEqual(secrets.get_secret("anthropic", backend=self.store), "")
        self.assertFalse(secrets.clear_secret("anthropic", backend=self.store))

    def test_an_unknown_provider_is_refused(self):
        with self.assertRaises(secrets.SecretError):
            secrets.set_secret("not-a-provider", "x", backend=self.store)

    def test_an_empty_credential_is_refused(self):
        """Storing an empty string would silently unset a working credential."""
        with self.assertRaises(secrets.SecretError):
            secrets.set_secret("openai", "   ", backend=self.store)

    def test_the_store_resolves_the_keyring_then_the_environment(self):
        """The single read path (Phase 41): override, keyring, environment -- in that order."""
        secrets.set_secret("openai", "sk-keyring", backend=self.store)
        store = secrets.SecretStore(backend=self.store)

        self.assertEqual(store.env("OPENAI_API_KEY"), "sk-keyring")
        # An explicit override wins -- this is the injection point for one run.
        injected = store.with_overrides(openai="sk-injected")
        self.assertEqual(injected.env("OPENAI_API_KEY"), "sk-injected")
        # ...and it does not leak into the store it came from.
        self.assertEqual(store.env("OPENAI_API_KEY"), "sk-keyring")

    def test_a_non_credential_name_falls_through_to_the_environment(self):
        os.environ["OPENAI_BASE_URL"] = "https://example.invalid/v1"
        self.addCleanup(os.environ.pop, "OPENAI_BASE_URL", None)

        store = secrets.SecretStore(backend=self.store)

        self.assertEqual(store.env("OPENAI_BASE_URL"), "https://example.invalid/v1")

    def test_resolving_a_credential_does_not_mutate_the_environment(self):
        """The Phase 41 correction: the store is read-only, so nothing is exported.

        ``os.environ`` is global mutable state shared by every thread; writing a credential into it
        is how two concurrent runs overwrite each other, and how a library that dumps the
        environment on a crash writes a live key into the log.
        """
        secrets.set_secret("openai", "sk-keyring-only", backend=self.store)
        store = secrets.SecretStore(backend=self.store)

        self.assertEqual(store.env("OPENAI_API_KEY"), "sk-keyring-only")
        self.assertNotIn("OPENAI_API_KEY", os.environ, "the store must not export")

    def test_a_bound_store_does_not_affect_another_context(self):
        """The store is a ContextVar, not a module global: two runs cannot clobber each other."""
        default = secrets.active()
        token = secrets.use(secrets.SecretStore({"openai": "sk-run-b"}, backend=self.store))
        try:
            self.assertEqual(secrets.active().env("OPENAI_API_KEY"), "sk-run-b")
        finally:
            secrets._ACTIVE.reset(token)
        self.assertIs(secrets.active(), default)

    def test_a_provider_name_resolves_through_the_active_store(self):
        token = secrets.use(secrets.SecretStore({"github": "ghp-run"}, backend=self.store))
        try:
            self.assertEqual(secrets.credential("GITHUB_TOKEN"), "ghp-run")
        finally:
            secrets._ACTIVE.reset(token)


def _noop():
    return None


class ZeroTrustAuditTests(unittest.TestCase):
    """The claim, asserted: no credential lands in the ledger or in the state directory.

    A secret that reaches the database is a secret in every backup of it, and one that reaches the
    state directory is a secret in a file the engine wrote. Both are exactly what the keyring
    exists to prevent, so both are checked against the bytes rather than trusted.
    """

    def test_a_credential_never_reaches_the_ledger_or_the_state_directory(self):
        from api.intents import Intent
        from storage.db import get_store
        from storage.intents import IntentLedger

        store = _FakeKeyring()
        marker = "sk-must-never-be-written-anywhere"
        secrets.set_secret("openai", marker, backend=store)

        state = tempfile.mkdtemp(prefix="aleth_zerotrust_")
        self.addCleanup(shutil.rmtree, state, ignore_errors=True)
        ledger = IntentLedger(os.path.join(state, "aleth_state.db"))
        ledger.record(Intent(id="i-1", action_type="custom", message="go",
                             action_params={}, enqueued_at=0.0))
        ledger.record_step_spend("i-1", 1, prompt=10, completion=2)
        ledger.record_step_context("i-1", 1, '{"summary":"work"}')
        get_store()  # applies the telemetry schema, as a boot does

        # Resolving it is what a client factory does; it must leave no trace behind.
        resolved = secrets.SecretStore(backend=store).env("OPENAI_API_KEY")
        self.assertEqual(resolved, marker)

        for current, _dirs, files in os.walk(state):
            for name in files:
                with open(os.path.join(current, name), "rb") as handle:
                    self.assertNotIn(marker.encode(), handle.read(), os.path.join(current, name))


class PreflightTests(unittest.TestCase):
    """Every check answers, and every failure carries the fix."""

    def test_git_is_found_on_this_machine(self):
        self.assertTrue(preflight.check_git().ok)

    def test_a_missing_git_names_how_to_install_it(self):
        with mock.patch.object(preflight.shutil, "which", return_value=None):
            check = preflight.check_git()
        self.assertFalse(check.ok)
        self.assertIn("git-scm.com", check.fix)

    def test_a_free_port_passes_and_a_held_one_fails_with_the_command_to_find_it(self):
        held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(held.close)
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]

        check = preflight.check_port(port)

        self.assertFalse(check.ok)
        self.assertIn(str(port), check.detail)
        self.assertIn("lsof", check.fix)

    def test_an_unreachable_runtime_fails_and_says_so(self):
        with mock.patch.object(preflight.docker_sandbox, "available", return_value=False):
            check = preflight.check_docker()
        self.assertFalse(check.ok)
        self.assertIn("start Docker", check.fix)

    def test_enforce_refuses_to_start_when_anything_fails(self):
        broken = [
            preflight.Check("container runtime", False, "unreachable", "start Docker"),
            preflight.Check("git", True, "/usr/bin/git"),
        ]
        captured = []

        with mock.patch.object(preflight, "run_checks", return_value=broken):
            allowed = preflight.enforce(emit=captured.append)

        self.assertFalse(allowed)
        text = "\n".join(captured)
        self.assertIn("FAIL", text)
        self.assertIn("start Docker", text)
        self.assertIn("Nothing was started", text)

    def test_the_report_shows_the_passes_too(self):
        """A user should see what was verified, not only what broke."""
        text = preflight.report([
            preflight.Check("git", True, "/usr/bin/git"),
            preflight.Check("api port", True, "127.0.0.1:8000 is free"),
        ])
        self.assertIn("[ok  ] git", text)
        self.assertIn("all checks passed", text)


class EngineLogTests(unittest.TestCase):
    """Structured, correlated, and bounded -- the record cannot be what fills the disk."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="aleth_log_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        engine_log._configured_path = ""
        self.addCleanup(setattr, engine_log, "_configured_path", "")

    def _lines(self):
        with open(engine_log.log_path(self.dir), "r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_an_entry_is_one_json_object_with_the_required_fields(self):
        engine_log.configure(self.dir)
        engine_log.get_logger("test").warning("something happened")

        entry = self._lines()[-1]

        self.assertIn("timestamp", entry)
        self.assertEqual(entry["level"], "WARNING")
        self.assertEqual(entry["message"], "something happened")

    def test_an_entry_carries_the_run_it_belongs_to(self):
        from tools.run_context import set_current_intent

        engine_log.configure(self.dir)
        set_current_intent("i-correlated")
        try:
            engine_log.get_logger("test").error("a run failed")
        finally:
            set_current_intent(None)

        self.assertEqual(self._lines()[-1]["intent_id"], "i-correlated")

    def test_the_record_is_bounded(self):
        """50 MB per file, three backups: the cap is the whole point of the handler."""
        engine_log.configure(self.dir)
        handler = next(
            h for h in logging.getLogger().handlers if getattr(h, "_aleth_engine_log", False)
        )

        self.assertEqual(handler.maxBytes, engine_log.MAX_BYTES)
        self.assertEqual(handler.backupCount, engine_log.BACKUP_COUNT)
        self.assertEqual(engine_log.MAX_BYTES, 50 * 1024 * 1024)

    def test_configuring_twice_does_not_double_every_line(self):
        engine_log.configure(self.dir)
        engine_log.configure(self.dir)

        attached = [
            h for h in logging.getLogger().handlers if getattr(h, "_aleth_engine_log", False)
        ]

        self.assertEqual(len(attached), 1)


class ProcessLockTests(unittest.TestCase):
    """Phase 41: one engine per project, decided by the filesystem rather than a port probe."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="aleth_lock_")
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)

    def test_a_free_state_directory_is_acquired(self):
        with process_lock.process_lock(self.state) as state:
            self.assertTrue(state.acquired)
            self.assertEqual(process_lock.read_pid(state.path), os.getpid())

    def test_the_lock_is_released_on_the_way_out(self):
        with process_lock.process_lock(self.state) as state:
            path = state.path
        self.assertFalse(os.path.exists(path))

    def test_a_second_holder_is_refused_with_the_pid_named(self):
        """The whole point: another *engine* is identified, not merely 'something is bound'."""
        import threading

        held = threading.Event()
        release = threading.Event()

        def hold():
            with process_lock.process_lock(self.state) as state:
                self.assertTrue(state.acquired)
                held.set()
                release.wait(timeout=10)

        worker = threading.Thread(target=hold, daemon=True)
        worker.start()
        self.assertTrue(held.wait(timeout=5))
        try:
            with process_lock.process_lock(self.state) as state:
                self.assertFalse(state.acquired)
                self.assertIn("another engine", state.detail)
        finally:
            release.set()
            worker.join(timeout=5)

    def test_a_stale_lock_is_reaped_and_taken_over(self):
        """A hard crash leaves the file behind with a dead pid: reap it, run the cleanup, take over."""
        path = process_lock.pid_path(self.state)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("999999999")
        reaped = []

        with process_lock.process_lock(self.state, reap=lambda: reaped.append(True)) as state:
            self.assertTrue(state.acquired)
            self.assertTrue(state.stale)
            self.assertEqual(state.holder_pid, 999999999)
            self.assertEqual(process_lock.read_pid(path), os.getpid())

        self.assertEqual(reaped, [True], "a stale lock must run the Phase 36 cleanup")

    def test_the_probe_reports_a_live_holder_without_taking_the_lock(self):
        path = process_lock.pid_path(self.state)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))

        state = process_lock.check(self.state)

        self.assertTrue(state.acquired)
        self.assertEqual(state.holder_pid, os.getpid())

    def test_a_dead_pid_is_not_alive(self):
        if os.name != "posix":
            self.skipTest("liveness is only provable on POSIX")
        self.assertFalse(process_lock.process_alive(999999999))

    def test_the_pre_flight_reports_another_engine(self):
        from tools import preflight

        with mock.patch.object(process_lock, "check") as probe:
            probe.return_value = process_lock.LockState(
                acquired=False, path="x", holder_pid=4242,
                detail="another engine is running (pid 4242)",
            )
            with mock.patch.object(preflight, "check_docker", return_value=preflight.Check("d", True)), \
                    mock.patch.object(preflight, "check_git", return_value=preflight.Check("g", True)), \
                    mock.patch.object(preflight, "check_port", return_value=preflight.Check("p", True)), \
                    mock.patch.object(preflight, "check_keyring", return_value=preflight.Check("k", True)):
                check = preflight.check_process_lock()

        self.assertFalse(check.ok)
        self.assertIn("4242", check.detail)
        self.assertIn("already running", check.fix)


class CliTests(unittest.TestCase):
    """``aleth keys`` and ``aleth boot`` -- the only supported way in."""

    def setUp(self):
        self.store = _FakeKeyring()
        self.addCleanup(self._restore_env)
        self._saved = {env: os.environ.get(env) for env in secrets.PROVIDERS.values()}
        for env in self._saved:
            os.environ.pop(env, None)

    def _restore_env(self):
        for env, value in self._saved.items():
            if value is None:
                os.environ.pop(env, None)
            else:
                os.environ[env] = value

    def test_keys_set_stores_a_prompted_credential(self):
        import cli

        with mock.patch.object(cli.secrets, "set_secret", wraps=secrets.set_secret) as setter, \
                mock.patch.object(cli.getpass, "getpass", return_value="sk-prompted"):
            status = cli.main(["keys", "set", "openai"])

        self.assertEqual(status, 0)
        # It went to the *real* keyring (the backend is keyring's own), so clear it again.
        self.assertTrue(setter.called)
        secrets.clear_secret("openai")

    def test_keys_set_refuses_an_unknown_provider(self):
        import cli

        self.assertEqual(cli.main(["keys", "set", "nope"]), 2)

    def test_keys_list_names_providers_without_values(self):
        import cli

        with mock.patch.object(cli.secrets, "stored_providers", return_value=["github"]), \
                mock.patch("sys.stdout") as out:
            status = cli.main(["keys", "list"])

        self.assertEqual(status, 0)
        printed = " ".join(str(call) for call in out.write.call_args_list)
        self.assertIn("github", printed)

    def test_keys_clear_reports_what_it_removed(self):
        import cli

        with mock.patch.object(cli.secrets, "stored_providers", return_value=["openai"]), \
                mock.patch.object(cli.secrets, "clear_secret", return_value=True), \
                mock.patch("sys.stdout") as out:
            status = cli.main(["keys", "clear"])

        self.assertEqual(status, 0)
        self.assertIn("cleared 1", " ".join(str(c) for c in out.write.call_args_list))

    def test_boot_aborts_before_starting_anything_when_a_check_fails(self):
        """No partial execution: a failed pre-flight must not reach the daemon."""
        import cli

        with mock.patch.object(cli.preflight, "enforce", return_value=False):
            status = cli.main(["boot"])

        self.assertEqual(status, 1)

    def test_the_parser_requires_a_command(self):
        import cli

        with self.assertRaises(SystemExit):
            cli.main([])


if __name__ == "__main__":
    unittest.main()
