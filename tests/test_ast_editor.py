"""Exact-AST-node editing: a symbol splice, not a whole-file rewrite.

    .\\venv\\Scripts\\python.exe -m pytest tests/test_ast_editor.py -n0 -q
"""

import os
import shutil
import tempfile
import unittest

from tools import ast_editor

SOURCE = '''\
def alpha():
    return 1


class Greeter:
    def hi(self):
        return "hi"

    def bye(self):
        return "bye"


def omega():
    return 2
'''


class EditSymbolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ast_edit_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "mod.py")
        self._write(self.path, SOURCE)

    def _write(self, path, text):
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)

    def _read(self, path):
        with open(path, "r", encoding="utf-8", newline="") as handle:
            return handle.read()

    def test_symbols_in_file_lists_every_unit_with_spans(self):
        listing = ast_editor.symbols_in_file(self.path)
        self.assertEqual(
            [unit.qualified_name for unit in listing.symbols],
            ["alpha", "Greeter", "Greeter.hi", "Greeter.bye", "omega"],
        )
        self.assertTrue(all(unit.byte_end > unit.byte_start for unit in listing.symbols))

    def test_editing_a_function_replaces_exactly_its_bytes(self):
        result = ast_editor.edit_symbol(
            self.path, "omega", "def omega():\n    return 42\n", workspace_dir=self.tmp
        )
        self.assertTrue(result.success, result.error)
        text = self._read(self.path)
        self.assertIn("return 42", text)
        # Everything else is untouched -- the whole point of a node splice.
        self.assertIn("def alpha():", text)
        self.assertIn('return "hi"', text)
        self.assertIn('return "bye"', text)

    def test_editing_a_method_uses_the_qualified_name(self):
        result = ast_editor.edit_symbol(
            self.path, "Greeter.bye", 'def bye(self):\n        return "later"\n', workspace_dir=self.tmp
        )
        self.assertTrue(result.success, result.error)
        text = self._read(self.path)
        self.assertIn('return "later"', text)
        self.assertIn('return "hi"', text)

    def test_an_unknown_symbol_is_refused_not_guessed(self):
        result = ast_editor.edit_symbol(self.path, "nope", "x = 1\n", workspace_dir=self.tmp)
        self.assertFalse(result.success)
        self.assertIn("no symbol", result.error)

    def test_a_path_outside_the_workspace_is_refused(self):
        outside = os.path.join(tempfile.mkdtemp(prefix="ast_outside_"), "mod.py")
        self.addCleanup(shutil.rmtree, os.path.dirname(outside), ignore_errors=True)
        self._write(outside, SOURCE)
        result = ast_editor.edit_symbol(outside, "omega", "x = 1\n", workspace_dir=self.tmp)
        self.assertFalse(result.success)
        self.assertIn("outside the workspace", result.error)
        self.assertEqual(self._read(outside), SOURCE)  # untouched

    def test_a_multi_byte_document_splices_at_the_right_place(self):
        path = os.path.join(self.tmp, "uni.py")
        self._write(path, 'def a():\n    return "café ☕"\n\n\ndef b():\n    return 2\n')
        result = ast_editor.edit_symbol(path, "b", "def b():\n    return 3\n", workspace_dir=self.tmp)
        self.assertTrue(result.success, result.error)
        text = self._read(path)
        self.assertIn('return "café ☕"', text)
        self.assertIn("return 3", text)

    def test_an_edit_that_breaks_the_file_is_rejected_before_it_is_written(self):
        # A replacement that leaves an unclosed class is refused, so a broken splice never
        # reaches the disk.
        result = ast_editor.edit_symbol(
            self.path, "Greeter", "class Greeter(:\n", workspace_dir=self.tmp
        )
        self.assertFalse(result.success)
        self.assertEqual(self._read(self.path), SOURCE)

    def test_splice_bytes_refuses_a_range_that_does_not_fit(self):
        with self.assertRaises(ValueError):
            ast_editor.splice_bytes("short", 0, 999, "x")


if __name__ == "__main__":
    unittest.main()
