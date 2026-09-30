"""AST + vector context assembly: whole units with exact byte spans, not sliced text.

    .\\venv\\Scripts\\python.exe -m pytest tests/test_ast_context.py -n0 -q
"""

import os
import shutil
import tempfile
import unittest

from tools import ast_context
from tools.semantic_index import HashingEmbedder


class ContextAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ast_ctx_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._write("auth.py", 'def verify_token(token):\n    """Validate a JWT access token."""\n    return bool(token)\n')
        self._write("models.py", "class User:\n    def __init__(self, name):\n        self.name = name\n")
        self._write("notes.txt", "this is not source code")
        # A deterministic, offline embedder so the suite needs no model download.
        self.embedder = HashingEmbedder(dim=256)

    def _write(self, name, text):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_only_parseable_files_are_indexed(self):
        files = [os.path.basename(path) for path in ast_context.iter_source_files(self.tmp)]
        self.assertEqual(files, ["auth.py", "models.py"])

    def test_retrieval_returns_whole_units_with_exact_byte_ranges(self):
        ast_context.build_index(self.tmp, embedder=self.embedder)
        hits = ast_context.retrieve(self.tmp, "verify jwt access token", k=2, embedder=self.embedder)
        self.assertTrue(hits)
        for hit in hits:
            unit = hit.unit
            self.assertGreater(unit.byte_end, unit.byte_start)
            # The span really is the node: the file's bytes at that range are the content.
            with open(unit.filepath, "rb") as handle:
                raw = handle.read()
            self.assertEqual(raw[unit.byte_start:unit.byte_end].decode("utf-8"), unit.content)

    def test_the_rendered_context_labels_each_unit_with_its_span(self):
        ast_context.build_index(self.tmp, embedder=self.embedder)
        rendered = ast_context.context_for_query(self.tmp, "verify token", k=2, embedder=self.embedder)
        self.assertIn("bytes ", rendered)
        self.assertIn("verify_token", rendered)
        self.assertIn("```python", rendered)

    def test_an_unindexable_workspace_degrades_to_no_context(self):
        empty = tempfile.mkdtemp(prefix="ast_ctx_empty_")
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        self.assertEqual(ast_context.retrieve(empty, "anything", embedder=self.embedder), [])
        self.assertEqual(ast_context.context_for_query(empty, "anything", embedder=self.embedder), "")

    def test_a_blank_query_returns_nothing(self):
        ast_context.build_index(self.tmp, embedder=self.embedder)
        self.assertEqual(ast_context.retrieve(self.tmp, "   ", embedder=self.embedder), [])


if __name__ == "__main__":
    unittest.main()
