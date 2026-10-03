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

from tools import engine_log, preflight, secrets


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
        secrets.set_secret("openai", "sk-test-value", store=self.store)

        self.assertEqual(secrets.get_secret("openai", store=self.store), "sk-test-value")
        self.assertEqual(secrets.stored_providers(store=self.store), ["openai"])

    def test_the_service_name_is_ours_so_a_person_can_find_it(self):
        secrets.set_secret("github", "ghp_x", store=self.store)

        self.assertIn((secrets.SERVICE, "github"), self.store.entries)

    def test_a_list_reports_names_only(self):
        secrets.set_secret("openai", "sk-secret-value", store=self.store)

        listed = secrets.stored_providers(store=self.store)

        self.assertEqual(listed, ["openai"])
        self.assertNotIn("sk-secret-value", " ".join(listed))

    def test_clearing_forgets_it(self):
        secrets.set_secret("anthropic", "sk-ant", store=self.store)

        self.assertTrue(secrets.clear_secret("anthropic", store=self.store))
        self.assertEqual(secrets.get_secret("anthropic", store=self.store), "")
        self.assertFalse(secrets.clear_secret("anthropic", store=self.store))

    def test_an_unknown_provider_is_refused(self):
        with self.assertRaises(secrets.SecretError):
            secrets.set_secret("not-a-provider", "x", store=self.store)

    def test_an_empty_credential_is_refused(self):
        """Storing an empty string would silently unset a working credential."""
        with self.assertRaises(secrets.SecretError):
            secrets.set_secret("openai", "   ", store=self.store)

    def test_no_backend_is_a_reported_failure_not_a_silent_one(self):
        """Without a backend the CLI must say so; a silent no-op leaves the user believing it stored."""
        with mock.patch.object(secrets, "_store", return_value=None):
            with self.assertRaises(secrets.SecretError) as caught:
                secrets.set_secret("openai", "x")
        self.assertIn("keyring", str(caught.exception))
        # A *read* never raises: it is not a place to fail.
        self.assertEqual(secrets.get_secret("openai", store=None), "")

    def test_the_environment_is_loaded_from_the_store(self):
        secrets.set_secret("openai", "sk-from-keyring", store=self.store)

        loaded = secrets.install_into_environment(store=self.store)

        self.assertEqual(loaded, ["openai"])
        self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-from-keyring")

    def test_an_exported_name_wins_over_the_store(self):
        """The operator set it on purpose; silently replacing it would be un-debuggable."""
        os.environ["OPENAI_API_KEY"] = "sk-exported"
        secrets.set_secret("openai", "sk-keyring", store=self.store)

        loaded = secrets.install_into_environment(store=self.store)

        self.assertEqual(loaded, [])
        self.assertEqual(os.environ["OPENAI_API_KEY"], "sk-exported")

    def test_loading_a_credential_writes_no_file(self):
        """The zero-trust claim, asserted rather than promised."""
        root = tempfile.mkdtemp(prefix="aleth_secrets_")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        secrets.set_secret("openai", "sk-must-not-land-on-disk", store=self.store)

        cwd = os.getcwd()
        os.chdir(root)
        try:
            secrets.install_into_environment(store=self.store)
        finally:
            os.chdir(cwd)

        self.assertEqual(os.listdir(root), [], "loading a credential must not write a file")


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
        secrets.set_secret("openai", marker, store=store)

        state = tempfile.mkdtemp(prefix="aleth_zerotrust_")
        self.addCleanup(shutil.rmtree, state, ignore_errors=True)
        ledger = IntentLedger(os.path.join(state, "aleth_state.db"))
        ledger.record(Intent(id="i-1", action_type="custom", message="go",
                             action_params={}, enqueued_at=0.0))
        ledger.record_step_spend("i-1", 1, prompt=10, completion=2)
        ledger.record_step_context("i-1", 1, '{"summary":"work"}')
        get_store()  # applies the telemetry schema, as a boot does

        secrets.install_into_environment(store=store)

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

        with mock.patch.object(cli.secrets, "_store", return_value=self.store), \
                mock.patch.object(cli.getpass, "getpass", return_value="sk-prompted"):
            status = cli.main(["keys", "set", "openai"])

        self.assertEqual(status, 0)
        self.assertEqual(secrets.get_secret("openai", store=self.store), "sk-prompted")

    def test_keys_set_refuses_an_unknown_provider(self):
        import cli

        self.assertEqual(cli.main(["keys", "set", "nope"]), 2)

    def test_keys_list_names_providers_without_values(self):
        import cli

        secrets.set_secret("github", "ghp-secret", store=self.store)
        with mock.patch.object(cli.secrets, "_store", return_value=self.store), \
                mock.patch("sys.stdout") as out:
            status = cli.main(["keys", "list"])

        self.assertEqual(status, 0)
        printed = " ".join(str(call) for call in out.write.call_args_list)
        self.assertIn("github", printed)
        self.assertNotIn("ghp-secret", printed)

    def test_keys_clear_removes_them(self):
        import cli

        secrets.set_secret("openai", "sk-x", store=self.store)
        with mock.patch.object(cli.secrets, "_store", return_value=self.store):
            self.assertEqual(cli.main(["keys", "clear"]), 0)

        self.assertEqual(secrets.stored_providers(store=self.store), [])

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
