"""Security-boundary and cold-start regression tests.

Three boundaries this file pins, all of which were previously *claims* rather than
guarantees:

* the Architect's "restricted" shell really refuses inline code, interpreter-driven
  package installs, and command chaining -- the old first-token allow-list waved all
  three through because it matched only the leading ``python`` prefix;
* importing the zero-token markdown parser does not load ``langchain_core`` or the
  ``orchestration`` package, so a cold plan parse stays in the milliseconds;
* the tool manifest is the live MCP servers' ``tools/list`` and never a Python catalog,
  and no production module shells out to a command interpreter. Both are the durable form
  of Phase 8's "legacy catalog grep at zero" / "no native execution path" gates.
"""

import ast
import os
import re
import subprocess
import sys
import unittest
from unittest import mock

from tools.mcp_exec_server import ExecServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_MOCK_OK = "[Exit Code: 0]\nMOCKED"


def _run(command: str) -> str:
    """The Architect's verification shell, as the run reaches it: the exec server's tool."""
    return ExecServer(REPO_ROOT).execute_restricted_command(command)


class RestrictedShellBoundaryTests(unittest.TestCase):
    """The restricted shell must not be a one-line route to arbitrary execution/mutation.

    The policy moved from the deleted legacy shell module into the exec server, beside the
    containment it belongs with -- so these assertions still guard the same boundary, they
    just guard it where it now lives.
    """

    def setUp(self):
        # The command must never actually reach a shell in these tests. The seam is the runner
        # that returns both the formatted block and the isolated result, since the exec server
        # needs the result to write its telemetry receipt.
        patcher = mock.patch(
            "tools.mcp_exec_server.run_workspace_command_result",
            return_value=(_MOCK_OK, None),
        )
        self.run_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def assert_runs(self, command: str) -> None:
        self.assertEqual(_run(command), _MOCK_OK)
        self.run_mock.assert_called_once()
        self.run_mock.reset_mock()

    def assert_denied(self, command: str, fragment: str = "") -> None:
        result = _run(command)
        self.assertTrue(result.startswith("Permission Denied:"), result)
        if fragment:
            self.assertIn(fragment, result)
        self.run_mock.assert_not_called()
        self.run_mock.reset_mock()

    # --- allowed verification actions -------------------------------------------------

    def test_py_compile_verification_is_allowed(self):
        # The real workflow verifies with exactly this command.
        self.assert_runs("python -m py_compile main.py")

    def test_verification_script_is_allowed(self):
        self.assert_runs("python verify_weather.py")

    def test_pytest_is_allowed(self):
        self.assert_runs("pytest -q")

    def test_inspection_is_allowed(self):
        self.assert_runs("git status")

    # --- refused escalations ----------------------------------------------------------

    def test_inline_code_is_refused(self):
        self.assert_denied('python -c "import shutil"', "inline code")

    def test_stdin_code_is_refused(self):
        self.assert_denied("python -", "inline code")

    def test_module_install_via_interpreter_is_refused(self):
        self.assert_denied("python -m pip install requests", "mutation")

    def test_unknown_interpreter_module_is_refused(self):
        self.assert_denied("python -m http.server", "whitelist")

    def test_command_chaining_is_refused(self):
        self.assert_denied("pytest ; pip install requests", "chains")

    def test_mutation_command_is_refused(self):
        self.assert_denied("rm -rf build", "mutation")


class ColdStartImportTests(unittest.TestCase):
    """A zero-token markdown parse must not drag in an agent SDK or the agent catalogue."""

    def _run_python(self, code: str) -> str:
        out = subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def test_importing_the_parser_does_not_load_langchain(self):
        stdout = self._run_python(
            "import sys, tools.plan_parser; print('langchain_core' in sys.modules)"
        )
        self.assertEqual(stdout, "False")

    def test_building_the_vocabulary_does_not_import_orchestration(self):
        stdout = self._run_python(
            "import sys, tools.plan_parser as p; p._vocabulary_json(); "
            "print('orchestration' in sys.modules)"
        )
        self.assertEqual(stdout, "False")


class NoImportTimeToolCatalogTests(unittest.TestCase):
    """The tool manifest is the live servers' ``tools/list``, never a Python catalog.

    This is the durable form of the batch's two deletion gates. The ``@tool`` catalogs and the
    modules that held them were removed; a bare ``subprocess(..., shell=True)`` was the native
    execution shape the container perimeter replaced. Neither may quietly return.
    """

    _PACKAGE_DIRS = ("agents", "api", "core", "orchestration", "storage", "tools")
    _TOP_LEVEL_MODULES = ("app.py", "bridge_bus.py", "env_boot.py", "main.py", "registry.py")

    def _production_sources(self):
        for name in self._TOP_LEVEL_MODULES:
            path = os.path.join(REPO_ROOT, name)
            if os.path.isfile(path):
                yield path
        for package in self._PACKAGE_DIRS:
            for dirpath, dirnames, filenames in os.walk(os.path.join(REPO_ROOT, package)):
                dirnames[:] = [name for name in dirnames if name != "__pycache__"]
                for filename in sorted(filenames):
                    if filename.endswith(".py"):
                        yield os.path.join(dirpath, filename)

    def _read(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_no_tool_decorator_catalog_remains(self):
        pattern = re.compile(r"^\s*@tool\b", re.MULTILINE)
        offenders = [
            os.path.relpath(path, REPO_ROOT)
            for path in self._production_sources()
            if pattern.search(self._read(path))
        ]
        self.assertEqual(offenders, [], "a static @tool catalog reappeared")

    def test_the_deleted_catalog_and_perimeter_modules_stay_deleted(self):
        for name in ("file_ops.py", "shell_tools.py", "sandbox.py"):
            self.assertFalse(
                os.path.isfile(os.path.join(REPO_ROOT, "tools", name)),
                f"tools/{name} must not come back",
            )

    def test_no_production_module_shells_out(self):
        """``subprocess(..., shell=True)`` is the native-execution shape; a list argv is not."""
        offenders = []
        for path in self._production_sources():
            tree = ast.parse(self._read(path), filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and any(
                    keyword.arg == "shell"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in node.keywords
                ):
                    offenders.append(f"{os.path.relpath(path, REPO_ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [], "a native shell execution path reappeared")

    def test_the_verdict_runner_executes_in_the_container(self):
        """pytest imports and runs the workspace's own code, so it must never run on the host.

        A model-authored ``conftest.py`` or test module is arbitrary code: executing it with a
        host Python process is host-level arbitrary code execution, whatever the caller is
        called. The runner goes through the same perimeter as every other command.
        """
        source = self._read(os.path.join(REPO_ROOT, "tools", "test_runner.py"))
        self.assertIn("docker_sandbox.run_isolated", source)
        self.assertNotIn("import subprocess", source)
        self.assertNotIn("subprocess.", source)


if __name__ == "__main__":
    unittest.main()
