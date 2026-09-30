"""Tests for ``tools.code_metrics`` -- the Analyze dashboard's real source figures.

The dashboard used to carry no complexity or security widgets because nothing measured the
source. These tests hold the two claims that make the numbers trustworthy: the complexity
count is per function (a nested function's branches are not charged twice), and the security
review flags shaped calls, not names that merely contain a dangerous word.
"""

import os
import shutil
import tempfile
import unittest

from tools import code_metrics


class ComplexityTests(unittest.TestCase):
    def test_straight_line_function_is_one(self):
        report = code_metrics.analyze_source("def f():\n    return 1\n")
        self.assertEqual(report["functions"][0]["complexity"], 1)

    def test_each_branch_adds_one(self):
        source = "def f(a):\n    if a:\n        return 1\n    return 2\n"
        self.assertEqual(code_metrics.analyze_source(source)["functions"][0]["complexity"], 2)

    def test_a_boolean_chain_adds_one_per_extra_operand(self):
        source = "def f():\n    return a and b and c\n"
        self.assertEqual(code_metrics.analyze_source(source)["functions"][0]["complexity"], 3)

    def test_a_nested_function_is_counted_separately_not_twice(self):
        source = (
            "def outer():\n"
            "    def inner(x):\n"
            "        if x:\n"
            "            return 1\n"
            "        return 0\n"
            "    return inner\n"
        )
        by_name = {f["name"]: f["complexity"] for f in code_metrics.analyze_source(source)["functions"]}
        self.assertEqual(by_name["outer"], 1)
        self.assertEqual(by_name["inner"], 2)


class SecurityFlagTests(unittest.TestCase):
    def _flags(self, source):
        return [flag["kind"] for flag in code_metrics.analyze_source(source)["security_flags"]]

    def test_eval_and_exec_are_flagged(self):
        self.assertIn("eval", self._flags("eval(user_input)\n"))
        self.assertIn("exec", self._flags("exec(payload)\n"))

    def test_os_system_is_flagged(self):
        self.assertIn("os.system", self._flags("import os\nos.system(cmd)\n"))

    def test_subprocess_with_shell_true_is_flagged(self):
        self.assertIn(
            "subprocess.run shell=True",
            self._flags("import subprocess\nsubprocess.run(cmd, shell=True)\n"),
        )

    def test_subprocess_without_a_shell_is_not_flagged(self):
        self.assertEqual(self._flags("import subprocess\nsubprocess.run([\"ls\", \"-l\"])\n"), [])

    def test_a_method_that_shares_a_dangerous_name_is_not_flagged(self):
        # `df.eval()` is a dataframe method, not `eval`: the flag is a property of the call.
        self.assertEqual(self._flags("df.eval(\"x + 1\")\n"), [])


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aleth_metrics_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, text):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_aggregate_counts_modules_functions_and_hotspots(self):
        self._write("plain.py", "def f():\n    return 1\n")
        self._write("busy.py", "def g(x):\n    if x:\n        return 1\n    return 2\n")
        report = code_metrics.analyze_workspace_metrics(self.tmp)
        self.assertEqual(report["modules"], 2)
        self.assertEqual(report["functions"], 2)
        self.assertEqual(report["average_complexity"], 1.5)
        self.assertEqual(report["hotspot_count"], 0)

    def test_a_hotspot_is_reported_with_its_file_and_line(self):
        body = "".join(f"    if x == {i}:\n        return {i}\n" for i in range(code_metrics.HOTSPOT_THRESHOLD))
        self._write("hot.py", f"def busy(x):\n{body}    return -1\n")
        report = code_metrics.analyze_workspace_metrics(self.tmp)
        self.assertEqual(report["hotspot_count"], 1)
        self.assertEqual(report["hotspots"][0]["file"], "hot.py")
        self.assertEqual(report["hotspots"][0]["name"], "busy")
        self.assertGreaterEqual(report["hotspots"][0]["complexity"], code_metrics.HOTSPOT_THRESHOLD)

    def test_a_module_that_does_not_parse_lowers_coverage_instead_of_raising(self):
        self._write("ok.py", "def f():\n    return 1\n")
        self._write("broken.py", "def f(:\n")
        report = code_metrics.analyze_workspace_metrics(self.tmp)
        self.assertEqual(report["modules"], 1)
        self.assertEqual(report["unparsed"], ["broken.py"])
        self.assertEqual(report["coverage"], 0.5)

    def test_an_empty_workspace_is_a_clean_zero_not_a_division_error(self):
        report = code_metrics.analyze_workspace_metrics(self.tmp)
        self.assertEqual(report["modules"], 0)
        self.assertEqual(report["average_complexity"], 0.0)
        self.assertEqual(report["coverage"], 1.0)


if __name__ == "__main__":
    unittest.main()
