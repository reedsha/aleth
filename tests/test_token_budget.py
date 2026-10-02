"""Phases 31-32: what a run remembers, and what it costs.

Two limits that are not the same limit. The step ceiling bounds the *work*; the token budget bounds
the *bill*. And the history is bounded so that neither grows with the length of the run -- which is
the whole point, because a linear transcript reaches a hundred thousand tokens by turn fifteen and
every later call pays for it again.
"""

import os
import shutil
import sqlite3
import tempfile
import unittest

from storage.intents import IntentLedger
from tools import project_phases, token_budget


class TokenCountingTests(unittest.TestCase):
    def test_an_empty_payload_costs_nothing(self):
        self.assertEqual(token_budget.count_tokens(""), 0)

    def test_a_payload_costs_something_and_more_of_it_costs_more(self):
        small = token_budget.count_tokens("def alpha():\n    return 1\n")
        large = token_budget.count_tokens("def alpha():\n    return 1\n" * 200)
        self.assertGreater(small, 0)
        self.assertGreater(large, small)

    def test_the_estimate_over_counts_rather_than_under(self):
        """The safe direction for a budget: the fallback never *under*-counts a payload.

        Asserted with the encoder forced away, because that is the path being tested -- tiktoken,
        when it is present, is precise (and a run of one repeated character is cheaper still).
        """
        import math

        from unittest import mock

        text = "x" * 4000
        with mock.patch.object(token_budget, "_encoding", return_value=None):
            estimated = token_budget.count_tokens(text)
        self.assertEqual(estimated, math.ceil(len(text) / token_budget.CHARS_PER_TOKEN))
        self.assertGreaterEqual(estimated, 4000 // token_budget.CHARS_PER_TOKEN)

    def test_the_ceiling_is_reached_at_the_ceiling_not_past_it(self):
        limit = token_budget.max_intent_tokens()
        self.assertTrue(token_budget.budget_exceeded(limit, limit=limit))
        self.assertFalse(token_budget.budget_exceeded(limit - 1, limit=limit))

    def test_the_ceiling_is_configurable(self):
        original = os.environ.get(token_budget.MAX_TOKENS_ENV)
        self.addCleanup(self._restore, original)
        os.environ[token_budget.MAX_TOKENS_ENV] = "1234"
        self.assertEqual(token_budget.max_intent_tokens(), 1234)
        # ...and a nonsense value falls back rather than disabling the bound.
        os.environ[token_budget.MAX_TOKENS_ENV] = "not a number"
        self.assertEqual(token_budget.max_intent_tokens(), token_budget.MAX_INTENT_TOKENS)

    def _restore(self, original):
        if original is None:
            os.environ.pop(token_budget.MAX_TOKENS_ENV, None)
        else:
            os.environ[token_budget.MAX_TOKENS_ENV] = original


class IntentTokenLedgerTests(unittest.TestCase):
    """The counter the breaker reads: durable, cumulative, and one statement per increment."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aleth_tokens_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_path = os.path.join(self.tmp, "state.db")
        self.ledger = IntentLedger(self.db_path)

    def _record(self, intent_id="i1"):
        from api.intents import Intent

        intent = Intent(id=intent_id, action_type="custom", message="go",
                        action_params={}, enqueued_at=0.0)
        self.ledger.record(intent)
        return intent

    def test_a_fresh_intent_has_spent_nothing(self):
        self._record()
        self.assertEqual(self.ledger.token_totals("i1"), (0, 0))

    def test_spend_accumulates_rather_than_replacing(self):
        self._record()
        self.ledger.add_tokens("i1", prompt=100, completion=20)
        self.ledger.add_tokens("i1", prompt=50, completion=5)
        self.assertEqual(self.ledger.token_totals("i1"), (150, 25))

    def test_an_unknown_intent_has_spent_nothing(self):
        self.assertEqual(self.ledger.token_totals("never-accepted"), (0, 0))

    def test_a_zero_spend_is_not_written(self):
        """No statement for a no-op, so an idle loop does not churn the ledger."""
        self._record()
        self.ledger.add_tokens("i1", prompt=0, completion=0)
        self.assertEqual(self.ledger.token_totals("i1"), (0, 0))

    def test_a_ledger_written_before_the_token_columns_is_upgraded(self):
        # A database from an older build: the table exists without the token columns.
        legacy_path = os.path.join(self.tmp, "legacy.db")
        connection = sqlite3.connect(legacy_path)
        try:
            connection.execute(
                "CREATE TABLE intent_ledger ("
                " intent_id TEXT PRIMARY KEY, action_type TEXT NOT NULL DEFAULT '',"
                " message TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'queued',"
                " error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,"
                " updated_at REAL NOT NULL, acknowledged_at REAL NOT NULL DEFAULT 0)"
            )
            connection.execute(
                "INSERT INTO intent_ledger (intent_id, status, created_at, updated_at)"
                " VALUES ('old', 'running', 1.0, 1.0)"
            )
            connection.commit()
        finally:
            connection.close()

        ledger = IntentLedger(legacy_path)

        self.assertEqual(ledger.token_totals("old"), (0, 0))
        ledger.add_tokens("old", prompt=7, completion=3)
        self.assertEqual(ledger.token_totals("old"), (7, 3))
        # The old row survives the migration, with the columns' defaults.
        self.assertEqual(ledger.recent(1)[0]["status"], "running")


class VendoredEncodingTests(unittest.TestCase):
    """Phase 33: the counter is exact and offline.

    tiktoken downloads its BPE file on first use, and this engine cannot fetch one mid-run. The blob
    is vendored, so the count is tiktoken's own -- not a characters-per-token guess that fails on
    minified code, base64 and non-English text.
    """

    # tiktoken's published digest for ``cl100k_base``. If it changes, the vendored file changed, and
    # the count can no longer be trusted to match the model's own tokenizer.
    PUBLISHED_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"

    def setUp(self):
        # The module caches its encoding and its "already reported" flag. Reset both so this class
        # observes the load it is testing rather than a neighbour's.
        self._saved = (
            token_budget._ENCODING_CACHE,
            token_budget._ENCODING_TRIED,
            token_budget._ESTIMATE_REPORTED,
        )
        token_budget._ENCODING_CACHE = None
        token_budget._ENCODING_TRIED = False
        token_budget._ESTIMATE_REPORTED = False
        self.addCleanup(self._restore)

    def _restore(self):
        (
            token_budget._ENCODING_CACHE,
            token_budget._ENCODING_TRIED,
            token_budget._ESTIMATE_REPORTED,
        ) = self._saved

    def test_the_vendored_blob_is_present_and_is_the_published_one(self):
        import hashlib

        found = token_budget.vendor_dir()
        self.assertTrue(found, "the vendored encoding is not where the counter looks for it")
        blob = os.path.join(found, token_budget.ENCODING_CACHE_KEY)
        self.assertTrue(os.path.isfile(blob), blob)
        with open(blob, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        self.assertEqual(digest, self.PUBLISHED_SHA256)

    def test_the_count_is_tiktoken_exact_and_does_not_fall_back(self):
        import contextlib
        import io

        import tiktoken

        self.assertTrue(token_budget.install_cache_dir(), "no vendored cache directory to use")
        text = "def alpha():\n    return 1\n"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            count = token_budget.count_tokens(text)
        self.assertEqual(stderr.getvalue(), "", "the counter fell back to the estimate")
        # tiktoken's own answer, computed here so the test is not the function asserting itself.
        expected = len(tiktoken.get_encoding(token_budget.ENCODING_NAME).encode(text))
        self.assertEqual(count, expected)


class NodeSyntaxFallbackTests(unittest.TestCase):
    """The JavaScript check must not expand a whole frontend into one argv (E2BIG)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="aleth_nodecheck_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        with open(os.path.join(self.root, "package.json"), "w", encoding="utf-8") as handle:
            handle.write("{}")

    def test_it_batches_its_arguments_and_prunes_the_build_output(self):
        command = project_phases.detect_syntax_command(self.root)

        # ``-exec ... +`` batches what the OS will accept; a shell glob would not.
        self.assertIn("-exec node --check {} +", command)
        for excluded in ("node_modules", "dist", ".next", "build", "out", "coverage"):
            self.assertIn(f"*/{excluded}/*", command)
        self.assertNotIn("$(find", command, "a command substitution is the unbounded form")

    def test_a_build_script_is_preferred_when_it_exists(self):
        with open(os.path.join(self.root, "package.json"), "w", encoding="utf-8") as handle:
            handle.write('{"scripts": {"build": "next build"}}')
        self.assertIn("npm run build", project_phases.detect_syntax_command(self.root))


if __name__ == "__main__":
    unittest.main()
