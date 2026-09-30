"""Semantic index over AST chunks (Phase 2 of the context rebuild).

These tests pin the retrieval contract: a query returns *whole* Phase-1 units (a class /
function, never a sliced window) ranked by cosine similarity, with every metadata field --
byte range included -- surviving the round trip through the vector store.

The suite runs on :class:`HashingEmbedder`, a deterministic offline embedder, so it needs
no model download and no network. The real ``all-MiniLM-L6-v2`` path is exercised by
``MiniLmTests``, which is skipped unless ``DEEPAGENTS_TEST_MINILM=1`` is set:

    set DEEPAGENTS_TEST_MINILM=1
    .\\venv\\Scripts\\python.exe -m unittest tests.test_semantic_index -v
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import numpy as np
from pydantic import ValidationError

from tools.ast_chunker import CodeUnit, chunk_source
from tools.semantic_index import (
    CodeUnitIndex,
    Embedder,
    HashingEmbedder,
    MiniLmEmbedder,
    SearchHit,
    build_index_from_paths,
    retrieve,
)

# Each unit is given a token no other unit contains, so the offline (lexical) embedder's
# ranking is unambiguous: a query for that token can only point at that unit.
SOURCE = '''
def alpha(unicorn):
    return unicorn + 1


def beta(volcano):
    return volcano - 1


def gamma(glacier):
    return glacier * 2


class Omega:
    marker = 1
'''


def _units():
    return chunk_source(SOURCE, "a.py")


class HashingEmbedderTests(unittest.TestCase):
    def test_vectors_are_unit_length(self):
        vectors = HashingEmbedder(dim=64).encode(["hello world", "fetch temperature city"])
        norms = np.linalg.norm(vectors, axis=1)
        for norm in norms:
            self.assertAlmostEqual(float(norm), 1.0, places=5)

    def test_encoding_is_deterministic(self):
        embedder = HashingEmbedder(dim=64)
        first = embedder.encode(["fetch temperature for a city"])
        second = HashingEmbedder(dim=64).encode(["fetch temperature for a city"])
        self.assertTrue(np.array_equal(first, second))

    def test_shared_vocabulary_is_closer_than_disjoint_vocabulary(self):
        embedder = HashingEmbedder(dim=256)
        vectors = embedder.encode(
            ["fetch temperature city", "fetch temperature town", "add numbers integer"]
        )
        related = float(np.dot(vectors[0], vectors[1]))
        unrelated = float(np.dot(vectors[0], vectors[2]))
        self.assertGreater(related, unrelated)

    def test_empty_input_has_the_declared_shape(self):
        vectors = HashingEmbedder(dim=32).encode([])
        self.assertEqual(vectors.shape, (0, 32))

    def test_non_positive_dim_is_rejected(self):
        with self.assertRaises(ValueError):
            HashingEmbedder(dim=0)


class IndexingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.index = CodeUnitIndex(self._tmp.name, HashingEmbedder(dim=128))

    def test_count_reflects_what_was_indexed(self):
        self.assertEqual(self.index.count(), 0)
        self.assertEqual(self.index.index_units(_units()), 4)
        self.assertEqual(self.index.count(), 4)

    def test_indexing_again_rebuilds_rather_than_appends(self):
        self.index.index_units(_units())
        self.index.index_units(_units()[:1])
        self.assertEqual(self.index.count(), 1)

    def test_empty_index_returns_no_hits(self):
        self.assertEqual(self.index.search("anything"), [])
        self.index.index_units([])
        self.assertEqual(self.index.count(), 0)
        self.assertEqual(self.index.search("anything"), [])

    def test_maintains_an_index_across_reopen(self):
        self.index.index_units(_units())
        reopened = CodeUnitIndex(self._tmp.name, HashingEmbedder(dim=128))
        self.assertEqual(reopened.count(), 4)


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.index = CodeUnitIndex(self._tmp.name, HashingEmbedder(dim=256))
        self.original = _units()
        self.index.index_units(self.original)

    def test_query_returns_the_matching_unit_first(self):
        hits = retrieve(self.index, "unicorn", k=3)
        self.assertTrue(hits)
        self.assertEqual(hits[0].unit.qualified_name, "alpha")

    def test_hits_are_ranked_by_descending_score(self):
        hits = self.index.search("unicorn volcano glacier marker", k=5)
        scores = [hit.score for hit in hits]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_scores_are_within_the_cosine_range(self):
        for hit in self.index.search("glacier", k=5):
            self.assertGreaterEqual(hit.score, -1.0)
            self.assertLessEqual(hit.score, 1.0)

    def test_an_exact_content_match_scores_one(self):
        alpha = next(u for u in self.original if u.qualified_name == "alpha")
        hits = self.index.search(alpha.content, k=5)
        self.assertAlmostEqual(hits[0].score, 1.0, places=5)
        self.assertEqual(hits[0].unit.qualified_name, "alpha")

    def test_k_limits_the_number_of_hits(self):
        self.assertEqual(len(self.index.search("glacier", k=2)), 2)

    def test_k_must_be_positive(self):
        with self.assertRaises(ValueError):
            self.index.search("glacier", k=0)

    def test_every_returned_unit_is_a_whole_reconstructable_unit(self):
        for hit in self.index.search("unicorn volcano glacier marker", k=4):
            self.assertIsInstance(hit.unit, CodeUnit)
            self.assertIn(hit.unit, self.original)
            # The byte range still reconstructs the exact source span of the original
            # file -- the retrieved unit is not a re-derived approximation.
            self.assertEqual(
                SOURCE.encode("utf-8")[hit.unit.byte_start:hit.unit.byte_end].decode("utf-8"),
                hit.unit.content,
            )

    def test_results_carry_the_original_metadata(self):
        hit = self.index.search("glacier", k=1)[0]
        original = next(u for u in self.original if u.qualified_name == "gamma")
        self.assertEqual(hit.unit, original)

    def test_language_filter_excludes_other_languages(self):
        mixed = CodeUnitIndex(self._tmp.name, HashingEmbedder(dim=256), table_name="mixed")
        java = chunk_source("public class Greeter { public void hi() {} }", "a.java")
        mixed.index_units(java)
        self.assertTrue(all(h.unit.language == "java" for h in mixed.search("greeter", k=5, language="java")))
        self.assertEqual(mixed.search("greeter", k=5, language="python"), [])

    def test_filepath_filter_narrows_before_ranking(self):
        hits = self.index.search("glacier", k=5, filepath="a.py")
        self.assertTrue(hits)
        self.assertTrue(all(hit.unit.filepath == "a.py" for hit in hits))
        self.assertEqual(self.index.search("glacier", k=5, filepath="other.py"), [])


class SearchHitBoundaryTests(unittest.TestCase):
    def test_a_hit_refuses_an_unknown_field(self):
        unit = _units()[0]
        with self.assertRaises(ValidationError):
            SearchHit(score=1.0, unit=unit, invented=True)

    def test_a_hit_requires_a_unit_and_a_score(self):
        unit = _units()[0]
        with self.assertRaises(ValidationError):
            SearchHit(score=1.0)


class EmbedderContractTests(unittest.TestCase):
    class _WrongShapeEmbedder(Embedder):
        @property
        def dim(self):
            return 8

        def encode(self, texts):
            return np.zeros((len(texts), 4), dtype=np.float32)

    def test_a_mismatched_embedder_shape_is_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            index = CodeUnitIndex(work, self._WrongShapeEmbedder())
            with self.assertRaises(ValueError):
                index.index_units(_units())


class BuildFromPathsTests(unittest.TestCase):
    def test_chunks_supported_files_and_skips_the_rest(self):
        with tempfile.TemporaryDirectory() as work:
            py_path = os.path.join(work, "mod.py")
            with open(py_path, "w", encoding="utf-8") as handle:
                handle.write("def solo():\n    return 1\n")
            txt_path = os.path.join(work, "notes.txt")
            with open(txt_path, "w", encoding="utf-8") as handle:
                handle.write("not code")
            index = build_index_from_paths(
                [py_path, txt_path], os.path.join(work, "db"), HashingEmbedder(dim=64)
            )
            self.assertEqual(index.count(), 1)
            self.assertEqual(index.search("solo", k=1)[0].unit.qualified_name, "solo")


@unittest.skipUnless(
    os.environ.get("DEEPAGENTS_TEST_MINILM") == "1",
    "set DEEPAGENTS_TEST_MINILM=1 to run the real-model test",
)
class MiniLmTests(unittest.TestCase):
    def test_real_model_indexes_and_retrieves(self):
        with tempfile.TemporaryDirectory() as work:
            index = CodeUnitIndex(work, MiniLmEmbedder())
            index.index_units(_units())
            hits = retrieve(index, "multiply a glacier by two", k=2)
            self.assertEqual(hits[0].unit.qualified_name, "gamma")


class MiniLmLoadLockTests(unittest.TestCase):
    """Phase 8.1: the weights load under a cross-process lock.

    The cache the weights land in is machine-wide, so the load is serialised with a file lock:
    the first process downloads and loads, the rest block and then find the cache populated.
    These tests stub ``torch``/``transformers`` so nothing is downloaded -- the *lock* is what is
    under test, not the model.
    """

    class _RecordingLock:
        def __init__(self, entered):
            self._entered = entered

        def __enter__(self):
            self._entered.append(True)
            return self

        def __exit__(self, *_exc):
            return False

    def _stub_model_stack(self, calls):
        """Fake ``torch``/``transformers`` so the load happens without touching the network."""
        import types

        class _Auto:
            @staticmethod
            def from_pretrained(name):
                calls.append(name)
                return types.SimpleNamespace(eval=lambda: None)

        patcher = mock.patch.dict(
            sys.modules,
            {"torch": types.SimpleNamespace(), "transformers": types.SimpleNamespace(AutoModel=_Auto, AutoTokenizer=_Auto)},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patch_lock(self, entered):
        from tools import semantic_index

        patcher = mock.patch.object(
            semantic_index, "_model_lock", lambda _name: self._RecordingLock(entered)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_load_is_taken_under_the_cross_process_lock(self):
        calls = []
        entered = []
        self._stub_model_stack(calls)
        self._patch_lock(entered)

        MiniLmEmbedder()._ensure_loaded()

        self.assertEqual(len(entered), 1, "the weights must be loaded inside the lock")
        self.assertTrue(calls, "the model was loaded")

    def test_a_second_load_neither_relocks_nor_reloads(self):
        calls = []
        entered = []
        self._stub_model_stack(calls)
        self._patch_lock(entered)

        embedder = MiniLmEmbedder()
        embedder._ensure_loaded()
        loaded = len(calls)
        embedder._ensure_loaded()

        self.assertEqual(len(entered), 1)
        self.assertEqual(len(calls), loaded)

    def test_the_lock_path_is_stable_and_per_model(self):
        from tools import semantic_index

        first = semantic_index._lock_path("sentence-transformers/all-MiniLM-L6-v2")
        self.assertEqual(first, semantic_index._lock_path("sentence-transformers/all-MiniLM-L6-v2"))
        self.assertNotEqual(first, semantic_index._lock_path("other/model"))
        self.assertTrue(first.endswith(".lock"))
        # The path is a filename, not the model name: a slash would have created a directory.
        self.assertNotIn("/", os.path.basename(first))

    def test_one_lock_object_per_model(self):
        from tools import semantic_index

        self.assertIs(semantic_index._model_lock("lock-test-a"), semantic_index._model_lock("lock-test-a"))
        self.assertIsNot(semantic_index._model_lock("lock-test-a"), semantic_index._model_lock("lock-test-b"))

    def test_the_lock_blocks_a_second_process_until_the_first_releases(self):
        """The lock is a real OS lock across processes, not a thread lock."""
        from tools import semantic_index

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(semantic_index.__file__)))
        script = (
            "import time\n"
            "from tools.semantic_index import _model_lock\n"
            "with _model_lock('cross-process-probe'):\n"
            "    print('held', flush=True)\n"
            "    time.sleep(1.5)\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")
        child = subprocess.Popen(
            [sys.executable, "-c", script], cwd=repo_root, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        stdout, stderr = child.stdout, child.stderr
        if stdout is None or stderr is None:  # pragma: no cover - PIPE always yields both
            self.fail("the probe process has no pipes")
        try:
            # Read only the child's readiness line. Its stderr is read *after* it exits -- reading
            # it here would block until EOF, i.e. until the child had already released the lock,
            # and the wait this test measures would never be observed.
            line = stdout.readline().strip()
            started = time.time()
            with semantic_index._model_lock("cross-process-probe"):
                waited = time.time() - started
        finally:
            child.wait(timeout=60)

        self.assertEqual(line, "held", f"the child never took the lock (said {line!r}): {stderr.read()}")
        self.assertGreaterEqual(waited, 0.5, f"the second acquirer did not wait ({waited:.2f}s)")


if __name__ == "__main__":
    unittest.main()
