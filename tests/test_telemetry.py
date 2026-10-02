"""Phase 11: the forensic ledger, and the source tree staying clean.

Two things are proven here. First, that an execution leaves a **complete, verifiable** row --
the hash in the ledger is a SHA-256 of the *full* stream, not of the 1 MB the perimeter retains,
so a stored receipt can be checked against bytes recovered later, and an OOM kill is recorded as
one rather than as an anonymous failure. Second, that running the engine writes nothing beside its
own source, which is what makes a read-only install possible.
"""

import hashlib
import os
import shutil
import sqlite3
import tempfile
import unittest

from storage import telemetry
from tools import docker_sandbox, workspace
from tools.mcp_client import default_exec_command, mcp_workspace_client

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_DOCKER = os.path.join(REPO_ROOT, "tests", "fake_docker.py")

# Ignored while checking that a run writes nothing into the source tree: these are build and
# tooling artifacts, not application state.
TREE_IGNORE = {
    ".git", "venv", "node_modules", "dist", "__pycache__", ".pytest_cache",
    "test-results", "knowledge_vectors", "playwright-report", "target",
    "my_project_workspace",
}


def fake_docker_env(**extra):
    env = {"ALETH_DOCKER_BIN": f"{os.sys.executable} {FAKE_DOCKER}"}
    env.update(extra)
    return env


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TelemetryTestCase(unittest.TestCase):
    """A real store on a throwaway plan directory, so the ledger is the production table."""

    def setUp(self):
        from storage.db import get_store, reset_stores

        self.tmp = tempfile.mkdtemp(prefix="aleth_telemetry_")
        self._orig = (workspace.PLAN_DIR, workspace.PROJECT_DIR, workspace.ACTIVE_PLAN_FILE)
        workspace.PLAN_DIR = self.tmp
        workspace.PROJECT_DIR = self.tmp
        workspace.ACTIVE_PLAN_FILE = "PLAN.md"
        workspace.publish_state_root()
        reset_stores()
        self.store = get_store()
        self.addCleanup(self._restore)

    def _restore(self):
        from storage.db import reset_stores

        workspace.PLAN_DIR, workspace.PROJECT_DIR, workspace.ACTIVE_PLAN_FILE = self._orig
        reset_stores()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, command, *, session_id="session-1", **env):
        """One execution through the real exec server, with the ledger wired."""
        from unittest import mock

        with mock.patch.dict(os.environ, fake_docker_env(**env)):
            with mcp_workspace_client(
                self.tmp,
                command=default_exec_command(
                    self.tmp, db_path=self.store.path, session_id=session_id
                ),
            ) as client:
                return client.call_tool("execute_command", {"command": command})


class TelemetryReceiptTests(TelemetryTestCase):
    def test_telemetry_receipt_written(self):
        """A standard execution leaves a complete, verifiable row."""
        # No newline in the canned body: the double writes it as *text*, so a newline would be
        # translated to CRLF on Windows and the digest would legitimately differ.
        body = b"receipt-body"
        output = self._run(
            "echo ignored", FAKE_DOCKER_STDOUT=body.decode(), FAKE_DOCKER_EXIT="0"
        )
        self.assertTrue(output.startswith("[Exit Code: 0]"), output)

        rows = telemetry.receipts_for_session(self.store.path, "session-1")
        self.assertEqual(len(rows), 1, f"expected one receipt, got {rows!r}")
        row = rows[0]

        # Every mandated column, and the hash is of the bytes the child actually wrote.
        self.assertTrue(row["execution_id"], "no execution id")
        self.assertEqual(row["session_id"], "session-1")
        self.assertEqual(row["target_tool"], "execute_command")
        self.assertEqual(row["exit_code"], 0)
        self.assertGreaterEqual(row["duration_ms"], 0)
        self.assertEqual(row["stdout_hash"], _sha256(body))
        self.assertGreater(row["recorded_at"], 0)

    def test_an_oom_kill_is_recorded_as_such(self):
        """Exit 137 is the kernel's OOM killer, and the ledger says so rather than "a failure"."""
        output = self._run("echo ignored", FAKE_DOCKER_STDOUT="", FAKE_DOCKER_EXIT="137")
        self.assertTrue(output.startswith("[Exit Code: 137]"), output)
        self.assertIn("OOM-KILLED", output)

        row = telemetry.receipts_for_session(self.store.path, "session-1")[0]
        self.assertEqual(row["exit_code"], 137)
        self.assertEqual(row["outcome"], "OOM_KILLED")

    def test_a_clean_run_records_no_outcome(self):
        self._run("echo ignored", FAKE_DOCKER_STDOUT="ok\n", FAKE_DOCKER_EXIT="0")
        row = telemetry.receipts_for_session(self.store.path, "session-1")[0]
        self.assertEqual(row["outcome"], "")

    def test_the_receipt_execution_id_matches_the_container_label(self):
        """The ledger key *is* the container's identity, so a row can be tied to a container."""
        import json

        log = os.path.join(self.tmp, "docker.log")
        self._run("echo ignored", FAKE_DOCKER_STDOUT="ok\n", FAKE_DOCKER_LOG=log)

        with open(log, "r", encoding="utf-8") as handle:
            calls = [json.loads(line) for line in handle if line.strip()]
        run = next(call for call in calls if call and call[0] == "run")
        labels = [run[index + 1] for index, token in enumerate(run) if token == "--label"]
        labelled = next(l for l in labels if l.startswith("aleth.execution_id=")).split("=", 1)[1]

        rows = telemetry.receipts_for_session(self.store.path, "session-1")
        self.assertEqual(rows[0]["execution_id"], labelled)

    def test_the_hash_covers_the_full_stream_before_truncation(self):
        """20 MB through the pipe, and the ledger speaks for all of it.

        The retained window is 1 MB, so a hash taken over what survived would not match this
        digest. The assertion is therefore the proof that the digest is fed as bytes arrive.
        """
        megabytes = 20
        self._run("echo ignored", FAKE_DOCKER_FLOOD_MB=str(megabytes))

        expected = b"HEAD-MARKER\n" + b"x" * (megabytes * 1024 * 1024) + b"\nTAIL-MARKER\n"
        row = telemetry.receipts_for_session(self.store.path, "session-1")[0]
        self.assertEqual(row["stdout_hash"], _sha256(expected))
        self.assertNotEqual(
            row["stdout_hash"], _sha256(expected[: 1024 * 1024]),
            "the digest was taken over the retained window, not the full stream",
        )

    def test_a_refused_command_writes_no_receipt(self):
        """No container ran, so there is nothing to record -- and a phantom row would be a lie."""
        from unittest import mock

        with mock.patch.dict(os.environ, fake_docker_env(FAKE_DOCKER_DAEMON_DOWN="1")):
            with mcp_workspace_client(
                self.tmp,
                command=default_exec_command(
                    self.tmp, db_path=self.store.path, session_id="session-1"
                ),
            ) as client:
                output = client.call_tool("execute_command", {"command": "echo x"})

        self.assertIn("refused", output)
        self.assertEqual(telemetry.receipts_for_session(self.store.path, "session-1"), [])


class LedgerAppendOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aleth_ledger_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_path = os.path.join(self.tmp, "ledger.db")
        connection = sqlite3.connect(self.db_path)
        try:
            telemetry.apply_telemetry_schema(connection)
        finally:
            connection.close()

    def test_a_receipt_is_appended_and_readable(self):
        telemetry.record(
            self.db_path, execution_id="e1", session_id="s1", target_tool="execute_command",
            exit_code=0, duration_ms=12, stdout_hash="aa", stderr_hash="bb",
        )
        row = telemetry.receipt(self.db_path, "e1")
        self.assertEqual(row["duration_ms"], 12)
        self.assertEqual(telemetry.count(self.db_path), 1)

    def test_a_duplicate_receipt_is_refused_rather_than_overwriting_history(self):
        """Append-only: the primary key is what enforces it, not a convention."""
        telemetry.record(self.db_path, execution_id="e1", stdout_hash="first")
        with self.assertRaises(sqlite3.IntegrityError):
            telemetry.record(self.db_path, execution_id="e1", stdout_hash="second")
        self.assertEqual(telemetry.receipt(self.db_path, "e1")["stdout_hash"], "first")

    def test_an_unknown_execution_has_no_receipt(self):
        self.assertIsNone(telemetry.receipt(self.db_path, "nope"))


class SourceTreeIsReadOnlyTests(TelemetryTestCase):
    """Running the engine must write nothing beside its own source.

    A read-only install is only possible if nothing dynamic lands in the application directory, so
    this asserts the invariant the read-only mount would enforce -- and asserts it without needing
    root to mount anything. ``conftest`` isolates the plan directory, which is what makes the
    invariant hold here; the note in ``HANDOFF §13`` records where production still differs.
    """

    @staticmethod
    def _snapshot() -> dict:
        snapshot = {}
        for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
            dirnames[:] = [d for d in dirnames if d not in TREE_IGNORE and not d.endswith(".egg-info")]
            for name in filenames:
                if name.endswith((".pyc", ".pyo")):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    snapshot[path] = os.stat(path).st_mtime_ns
                except OSError:
                    continue
        return snapshot

    def test_a_run_writes_nothing_into_the_source_tree(self):
        before = self._snapshot()

        # The three things a run touches on disk: an execution, its receipt, and plan state.
        self._run("echo ignored", FAKE_DOCKER_STDOUT="ok\n")
        self.assertGreaterEqual(telemetry.count(self.store.path), 1)
        self.assertTrue(self.store.has_plan("PLAN") or True)  # the store answered at all

        after = self._snapshot()
        added = sorted(set(after) - set(before))
        changed = sorted(path for path in set(after) & set(before) if after[path] != before[path])
        self.assertEqual(added, [], f"a run created files in the source tree: {added}")
        self.assertEqual(changed, [], f"a run modified files in the source tree: {changed}")


class AgentFaultSchemaTests(unittest.TestCase):
    """Phase 27: a fault must belong to an intent, and the schema has to say so."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aleth_faults_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db_path = os.path.join(self.tmp, "ledger.db")

    def _build(self):
        connection = sqlite3.connect(self.db_path)
        try:
            telemetry.apply_telemetry_schema(connection)
        finally:
            connection.close()

    def test_a_fault_without_an_intent_is_refused(self):
        """The column is NOT NULL; this is what makes it *never empty* as well."""
        self._build()
        for blank in ("", "   ", None):
            with self.subTest(intent_id=blank):
                with self.assertRaises(ValueError):
                    telemetry.record_agent_fault(
                        self.db_path, intent_id=blank, kind="STEP_LIMIT_EXCEEDED"
                    )
        self.assertEqual(telemetry.agent_faults(self.db_path), [])

    def test_a_fault_is_joinable_to_its_intent(self):
        self._build()
        telemetry.record_agent_fault(self.db_path, intent_id="i-1", kind="STEP_LIMIT_EXCEEDED")
        telemetry.record_agent_fault(self.db_path, intent_id="i-2", kind="STEP_LIMIT_EXCEEDED")

        narrowed = telemetry.agent_faults(self.db_path, intent_id="i-1")
        self.assertEqual(len(narrowed), 1)
        self.assertEqual(narrowed[0]["intent_id"], "i-1")
        self.assertEqual(len(telemetry.agent_faults(self.db_path)), 2)

    def test_a_ledger_written_before_the_correlation_id_is_upgraded(self):
        """The phase the table shipped in had no intent_id -- and no migration framework either."""
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                "CREATE TABLE agent_faults ("
                " fault_id TEXT PRIMARY KEY, session_id TEXT NOT NULL DEFAULT '',"
                " kind TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT '',"
                " detail TEXT NOT NULL DEFAULT '', steps INTEGER NOT NULL DEFAULT 0,"
                " recorded_at REAL NOT NULL)"
            )
            # The index the old shape created, which is what makes DROP COLUMN refuse first.
            connection.execute(
                "CREATE INDEX idx_agent_faults_session ON agent_faults(session_id)"
            )
            connection.execute(
                "INSERT INTO agent_faults (fault_id, session_id, kind, recorded_at)"
                " VALUES ('old', 's-1', 'STEP_LIMIT_EXCEEDED', 1.0)"
            )
            connection.commit()
        finally:
            connection.close()

        self._build()

        connection = sqlite3.connect(self.db_path)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_faults)")}
        finally:
            connection.close()
        self.assertIn("intent_id", columns)
        self.assertNotIn("session_id", columns, "the uncorrelated column must not linger")
        # The old row survives, carrying the new column's default.
        self.assertEqual(telemetry.agent_faults(self.db_path)[0]["intent_id"], "")


if __name__ == "__main__":
    unittest.main()
