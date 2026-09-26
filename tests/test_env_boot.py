"""The ``.env`` bootstrap, pinned.

The bug this file exists for: ``load_dotenv()`` does not override, so a stale exported
``OPENAI_API_KEY`` silently beat the one in ``.env`` and every request failed as if the
key were simply invalid. These tests fix both halves of the contract -- ``.env`` wins, and
the launcher is told which names it replaced.
"""

import os
import pathlib
import tempfile
import unittest

from env_boot import load_environment

STALE = "sk-stale-from-the-shell"
REAL = "rqsty-sk-the-one-in-the-dot-env"


class LoadEnvironmentTests(unittest.TestCase):
    """``load_environment`` is exercised against a temporary file, never the real one."""

    def setUp(self):
        self._saved: dict = {}
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _env_file(self, text: str) -> str:
        path = pathlib.Path(self._tmp.name, ".env")
        path.write_text(text, encoding="utf-8")
        return str(path)

    def _export(self, name: str, value: str) -> None:
        """Exports a name for the duration of the test only."""
        self._saved[name] = os.environ.get(name)
        os.environ[name] = value

    def tearDown(self):
        for name in (self._saved or {}):
            if self._saved[name] is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = self._saved[name]

    def test_the_file_wins_over_an_exported_variable(self):
        # The regression: the shell held a stale key, the file held the real one.
        self._export("OPENAI_API_KEY", STALE)
        replaced = load_environment(self._env_file(f"OPENAI_API_KEY={REAL}\n"))
        self.assertEqual(replaced, ["OPENAI_API_KEY"])
        self.assertEqual(os.environ["OPENAI_API_KEY"], REAL)

    def test_an_identical_export_is_not_reported(self):
        self._export("OPENAI_API_KEY", REAL)
        replaced = load_environment(self._env_file(f"OPENAI_API_KEY={REAL}\n"))
        self.assertEqual(replaced, [])
        self.assertEqual(os.environ["OPENAI_API_KEY"], REAL)

    def test_an_unset_name_is_not_reported_but_is_loaded(self):
        os.environ.pop("DEEPAGENTS_TEST_ONLY", None)
        replaced = load_environment(self._env_file("DEEPAGENTS_TEST_ONLY=loaded\n"))
        self.assertEqual(replaced, [])
        self.assertEqual(os.environ["DEEPAGENTS_TEST_ONLY"], "loaded")
        os.environ.pop("DEEPAGENTS_TEST_ONLY", None)

    def test_every_shadowed_name_is_reported_in_file_order(self):
        self._export("OPENAI_API_KEY", STALE)
        self._export("OPENAI_BASE_URL", "https://stale.example/v1")
        replaced = load_environment(
            self._env_file(f"OPENAI_API_KEY={REAL}\nOPENAI_BASE_URL={REAL}/v1\n")
        )
        self.assertEqual(replaced, ["OPENAI_API_KEY", "OPENAI_BASE_URL"])

    def test_an_export_that_is_present_but_empty_is_replaced(self):
        # An empty export is still a shadow: the file's value is the one that should win.
        self._export("OPENAI_API_KEY", "")
        replaced = load_environment(self._env_file(f"OPENAI_API_KEY={REAL}\n"))
        self.assertEqual(replaced, ["OPENAI_API_KEY"])
        self.assertEqual(os.environ["OPENAI_API_KEY"], REAL)

    def test_a_missing_file_is_an_ordinary_start(self):
        missing = str(pathlib.Path(self._tmp.name, "nope.env"))
        self.assertEqual(load_environment(missing), [])

    def test_a_name_without_a_value_is_not_a_shadow(self):
        # dotenv reports a bare ``NAME`` line as None; there is no value to install.
        self._export("DEEPAGENTS_BARE", "already-set")
        replaced = load_environment(self._env_file("DEEPAGENTS_BARE\n"))
        self.assertNotIn("DEEPAGENTS_BARE", replaced)
        self.assertEqual(os.environ["DEEPAGENTS_BARE"], "already-set")


class RealConfigTests(unittest.TestCase):
    def test_the_default_path_is_the_app_root(self):
        # The sidebar panel resolves ``.env`` the same way, so the two cannot disagree.
        from env_boot import ENV_PATH
        from tools.workspace import PROJECT_ROOT

        self.assertEqual(os.path.dirname(ENV_PATH), os.path.abspath(PROJECT_ROOT))
        self.assertEqual(os.path.basename(ENV_PATH), ".env")


if __name__ == "__main__":
    unittest.main()
