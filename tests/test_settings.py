"""The in-app settings: what may be read back, written, and cleared.

Three contracts matter here and none of them is cosmetic:

* the API key never leaves the backend, so no payload the window can read holds it;
* only the allowlisted names may be written, because the value arrives from the webview;
* writing must not damage the file it edits -- comments, order and unrelated keys survive,
  and a duplicate assignment cannot shadow the value just written.
"""

import os
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

from tools import settings


@contextmanager
def clean_config_env(keep_key=True):
    """Runs a test with none of the configurable names present in the process environment.

    The key is kept unless a test is specifically about its absence: the route defaults come
    from ``agents.model_routing``, and importing ``agents`` builds the Architect, which needs
    credentials of its own.
    """
    with mock.patch.dict(os.environ, {}, clear=False):
        for name in settings.CONFIGURABLE_NAMES:
            os.environ.pop(name, None)
        if keep_key:
            os.environ["OPENAI_API_KEY"] = "sk-test-not-a-real-key"
        yield


class SettingsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="aleth_settings_")
        self.env_path = os.path.join(self._tmp.name, ".env")
        self.addCleanup(self._tmp.cleanup)

    def write_env(self, text):
        with open(self.env_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def read_env(self):
        with open(self.env_path, "r", encoding="utf-8") as f:
            return f.read()

    def field(self, result, name):
        return next(f for f in result["fields"] if f["name"] == name)


class ReadSettingsTests(SettingsTestCase):
    def test_an_absent_file_is_an_ordinary_answer(self):
        with clean_config_env():
            result = settings.read_settings(self.env_path)
        self.assertTrue(result["success"])
        self.assertFalse(result["found"])

    def test_a_non_secret_name_falls_back_to_its_default(self):
        with clean_config_env():
            result = settings.read_settings(self.env_path)
        entry = self.field(result, "ALETH_ARCHITECT_MODEL")
        self.assertFalse(entry["set"])
        self.assertEqual(entry["value"], "")
        self.assertEqual(entry["effective"], entry["default"])
        self.assertTrue(entry["default"], "a route needs a default to fall back to")

    def test_the_key_is_described_but_never_returned(self):
        with clean_config_env():
            os.environ["OPENAI_API_KEY"] = "sk-the-secret"
            result = settings.read_settings(self.env_path)
        entry = self.field(result, "OPENAI_API_KEY")
        self.assertTrue(entry["secret"])
        self.assertTrue(entry["set"])
        self.assertEqual(entry["length"], len("sk-the-secret"))
        self.assertEqual(entry["value"], "")
        self.assertEqual(entry["effective"], "")
        self.assertEqual(entry["default"], "")

    def test_a_set_route_reports_the_value_in_force(self):
        with clean_config_env():
            os.environ["ALETH_CODER_DEEP_MODEL"] = "openai:some/route"
            result = settings.read_settings(self.env_path)
        entry = self.field(result, "ALETH_CODER_DEEP_MODEL")
        self.assertEqual(entry["value"], "openai:some/route")
        self.assertEqual(entry["effective"], "openai:some/route")


class SaveSettingsTests(SettingsTestCase):
    def test_it_creates_the_file_when_there_is_none(self):
        with clean_config_env():
            result = settings.save_settings({"OPENAI_BASE_URL": "https://x.example/v1"}, self.env_path)
        self.assertTrue(result["success"])
        self.assertIn("OPENAI_BASE_URL=https://x.example/v1", self.read_env())
        self.assertEqual(result["saved"], ["OPENAI_BASE_URL"])

    def test_it_updates_in_place_and_keeps_comments_and_order(self):
        self.write_env(
            "# my keys\n"
            "OPENAI_API_KEY=old\n"
            "\n"
            "OPENAI_BASE_URL=https://old.example/v1\n"
            "SOMETHING_ELSE=keep-me\n"
        )
        with clean_config_env():
            settings.save_settings({"OPENAI_BASE_URL": "https://new.example/v1"}, self.env_path)
        text = self.read_env()
        self.assertIn("# my keys", text)
        self.assertIn("SOMETHING_ELSE=keep-me", text)
        self.assertIn("OPENAI_BASE_URL=https://new.example/v1", text)
        self.assertNotIn("old.example", text)
        self.assertLess(text.index("OPENAI_API_KEY"), text.index("OPENAI_BASE_URL"))

    def test_an_absent_name_is_appended(self):
        self.write_env("OPENAI_API_KEY=old\n")
        with clean_config_env():
            settings.save_settings({"ALETH_CODER_DEEP_MODEL": "openai:x/y"}, self.env_path)
        self.assertIn("ALETH_CODER_DEEP_MODEL=openai:x/y", self.read_env())

    def test_a_duplicate_assignment_is_collapsed(self):
        # dotenv takes the last assignment, so a stale later line would silently win.
        self.write_env("OPENAI_BASE_URL=https://first\nOPENAI_BASE_URL=https://second\n")
        with clean_config_env():
            settings.save_settings({"OPENAI_BASE_URL": "https://written"}, self.env_path)
        text = self.read_env()
        self.assertEqual(text.count("OPENAI_BASE_URL"), 1)
        self.assertIn("https://written", text)

    def test_an_export_line_stays_an_export_line(self):
        self.write_env("export OPENAI_BASE_URL=https://old\n")
        with clean_config_env():
            settings.save_settings({"OPENAI_BASE_URL": "https://new"}, self.env_path)
        self.assertIn("export OPENAI_BASE_URL=https://new", self.read_env())

    def test_a_value_with_spaces_is_quoted_and_survives_a_read(self):
        with clean_config_env():
            settings.save_settings({"ALETH_ARCHITECT_MODEL": "openai:my route"}, self.env_path)
        self.assertIn('ALETH_ARCHITECT_MODEL="openai:my route"', self.read_env())
        with clean_config_env():
            os.environ["ALETH_ARCHITECT_MODEL"] = "openai:my route"
            result = settings.read_settings(self.env_path)
        self.assertEqual(self.field(result, "ALETH_ARCHITECT_MODEL")["value"], "openai:my route")

    def test_a_blank_value_clears_the_name(self):
        self.write_env("ALETH_CODER_DEEP_MODEL=openai:x/y\n")
        with clean_config_env():
            os.environ["ALETH_CODER_DEEP_MODEL"] = "openai:x/y"
            settings.save_settings({"ALETH_CODER_DEEP_MODEL": ""}, self.env_path)
            self.assertNotIn("ALETH_CODER_DEEP_MODEL", os.environ)
            result = settings.read_settings(self.env_path)
        self.assertFalse(self.field(result, "ALETH_CODER_DEEP_MODEL")["set"])
        self.assertIn("ALETH_CODER_DEEP_MODEL=\n", self.read_env())

    def test_a_name_outside_the_allowlist_is_ignored(self):
        with clean_config_env():
            result = settings.save_settings({"PATH": "/evil", "LD_PRELOAD": "x.so"}, self.env_path)
        self.assertEqual(result["saved"], [])
        self.assertCountEqual(result["ignored"], ["PATH", "LD_PRELOAD"])
        self.assertFalse(os.path.isfile(self.env_path), "nothing allowed was sent, so nothing is written")

    def test_the_key_is_written_but_only_ever_described_back(self):
        with clean_config_env():
            result = settings.save_settings({"OPENAI_API_KEY": "sk-new"}, self.env_path)
        self.assertIn("OPENAI_API_KEY=sk-new", self.read_env())
        entry = self.field(result, "OPENAI_API_KEY")
        self.assertTrue(entry["set"])
        self.assertEqual(entry["value"], "")
        self.assertNotIn("sk-new", str(entry))

    def test_the_change_takes_effect_without_a_reload(self):
        # The route readers consult os.environ per call, which is why saving needs no restart.
        # Imported before the environment is cleared: importing ``agents`` builds the
        # Architect, which needs credentials of its own.
        try:
            from agents.model_routing import coder_model
        except Exception as exc:  # pragma: no cover - environmental, not behavioural
            self.skipTest(f"the agents package needs credentials to import: {exc}")

        with clean_config_env():
            settings.save_settings(
                {"ALETH_CODER_DEEP_MODEL": "openai:policy/live-route"}, self.env_path
            )
            self.assertEqual(coder_model("coder-deep"), "openai:policy/live-route")


if __name__ == "__main__":
    unittest.main()
