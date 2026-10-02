"""The intent ledger: what the user asked the engine to do, and how it ended.

An intent is a *user request to run*, and this table is its record. It exists because the run's
outcome is not the same question as "did the thread finish":

* the orchestrator can die mid-run -- OOM-killed, powered off, killed by the OS -- and then no
  process is left to write anything. The intent is left ``running`` in the ledger, and the *next
  boot* reconciles it: :meth:`IntentLedger.reconcile` marks every unfinished intent ``failed``
  with the reason. That is the only deterministic answer available, and it is why the state is
  written *before* the run rather than after it.
* a run that ended ``error`` is a failure, and a failure is **terminal**. It is never re-queued,
  never silently retried: the user asked for one run and got one run's answer. Continuing without
  being told would be the engine deciding to spend the user's time on its own initiative.

The ledger is also the thing the UI reads to decide whether it may accept new work. A failed
intent that has not been acknowledged is a gate: the user must see it and say they have.

Raw SQL on purpose -- the same reasoning as ``storage.telemetry``. This is a handful of columns
and two indexed reads, and it is written from the API process while the workflow writes task
state to the same file, so it is short transactions and WAL, not an ORM.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, FrozenSet, List, Optional

# A receipt is written while the orchestrator may be writing task state to the same file. WAL plus
# a bounded wait is what keeps a short append from failing under that contention.
BUSY_TIMEOUT_SECONDS = 10.0

INTENT_DDL = """
                CREATE TABLE IF NOT EXISTS intent_ledger (
                    intent_id       TEXT PRIMARY KEY,
                    action_type     TEXT NOT NULL DEFAULT '',
                    message         TEXT NOT NULL DEFAULT '',
                    status          TEXT NOT NULL DEFAULT 'queued',
                    error           TEXT NOT NULL DEFAULT '',
                    created_at      REAL NOT NULL,
                    updated_at      REAL NOT NULL,
                    acknowledged_at REAL NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_intent_status
                    ON intent_ledger(status);
                CREATE INDEX IF NOT EXISTS idx_intent_created
                    ON intent_ledger(created_at);
"""

# The terminal states. `failed` is the one that gates new work; `stopped` is terminal but was the
# user's own doing, so it is acknowledged by the act of stopping.
TERMINAL = ("completed", "stopped", "failed")


def _connect(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_SECONDS)
    connection.row_factory = sqlite3.Row
    return connection


def apply_intent_schema(connection: sqlite3.Connection) -> None:
    """Create the ledger on a caller's connection, so a test can build the real table."""
    connection.executescript(INTENT_DDL)


class IntentLedger:
    """The durable record of every intent, keyed by the id the API answered with."""

    def __init__(self, db_path: str):
        self.path = str(db_path)
        self.ensure_schema()

    def _write(self, statement: str, parameters: tuple) -> None:
        connection = _connect(self.path)
        try:
            with connection:
                connection.execute(statement, parameters)
        finally:
            connection.close()

    def ensure_schema(self) -> None:
        connection = _connect(self.path)
        try:
            with connection:
                apply_intent_schema(connection)
        finally:
            connection.close()

    def record(self, intent: Any) -> None:
        """Write a newly accepted intent as ``queued``, before anything runs it.

        Before, not after: a process that dies mid-run cannot write its own epitaph, and an
        intent that was never written is one the next boot cannot reconcile.
        """
        now = time.time()
        self._write(
            "INSERT OR REPLACE INTO intent_ledger"
            " (intent_id, action_type, message, status, error, created_at, updated_at, acknowledged_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (
                str(intent.id), str(intent.action_type), str(intent.message), "queued",
                "", float(intent.enqueued_at or now), now,
            ),
        )

    def mark_running(self, intent: Any) -> None:
        """The intent has been taken off the queue and handed to the engine."""
        self._write(
            "UPDATE intent_ledger SET status = 'running', updated_at = ? WHERE intent_id = ?",
            (time.time(), str(intent.id)),
        )

    def settle(self, intent: Any) -> None:
        """Write the terminal state. ``failed`` carries the reason and gates new work."""
        self._write(
            "UPDATE intent_ledger SET status = ?, error = ?, updated_at = ? WHERE intent_id = ?",
            (str(intent.status), str(intent.error or ""), time.time(), str(intent.id)),
        )

    def reconcile(self, reason: str) -> int:
        """Fail every intent that never finished, and say how many.

        Called at boot. An intent left ``queued`` or ``running`` is one whose engine stopped --
        crashed, was OOM-killed, or was powered off -- because a live engine always writes a
        terminal state. Marking them failed is what stops a dead run from looking like a live one
        forever, and it is deliberately *not* a retry: the user is told, and decides.
        """
        connection = _connect(self.path)
        try:
            with connection:
                cursor = connection.execute(
                    "UPDATE intent_ledger SET status = 'failed', error = ?, updated_at = ?"
                    " WHERE status IN ('queued', 'running')",
                    (str(reason), time.time()),
                )
                return int(cursor.rowcount or 0)
        finally:
            connection.close()

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        """The newest intents first, bounded. Uses ``idx_intent_created``."""
        connection = _connect(self.path)
        try:
            rows = connection.execute(
                "SELECT * FROM intent_ledger ORDER BY created_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def ids_with_status(self, *statuses: str) -> FrozenSet[str]:
        """The intent ids currently in one of ``statuses``. One indexed read.

        The staging sweep asks this to learn which shadows a run still owns (Phase 22): the
        question "may this execution's copy of the workspace be deleted?" is answered by the
        ledger, not by the shadow, so the query is served by ``idx_intent_status`` and never
        becomes a scan of every intent ever accepted.
        """
        wanted = tuple(str(status) for status in statuses if str(status))
        if not wanted:
            return frozenset()
        placeholders = ", ".join("?" for _ in wanted)
        connection = _connect(self.path)
        try:
            rows = connection.execute(
                f"SELECT intent_id FROM intent_ledger WHERE status IN ({placeholders})",
                wanted,
            ).fetchall()
            return frozenset(str(row["intent_id"]) for row in rows)
        finally:
            connection.close()

    def pending_failure(self) -> Optional[Dict[str, Any]]:
        """The newest failed intent the user has not acknowledged, or ``None``.

        This is the gate. While it answers a row, the UI must not accept a new intent: the user
        has not yet been told that the last one did not do what they asked.
        """
        connection = _connect(self.path)
        try:
            row = connection.execute(
                "SELECT * FROM intent_ledger WHERE status = 'failed' AND acknowledged_at = 0"
                " ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def acknowledge(self, intent_id: Optional[str] = None) -> int:
        """Mark a failure seen. Returns how many rows were acknowledged.

        With no id, every unacknowledged failure is acknowledged -- the UI's button clears the
        gate it is showing, and there is only ever one such gate.
        """
        connection = _connect(self.path)
        try:
            with connection:
                if intent_id:
                    cursor = connection.execute(
                        "UPDATE intent_ledger SET acknowledged_at = ?"
                        " WHERE intent_id = ? AND acknowledged_at = 0",
                        (time.time(), str(intent_id)),
                    )
                else:
                    cursor = connection.execute(
                        "UPDATE intent_ledger SET acknowledged_at = ?"
                        " WHERE status = 'failed' AND acknowledged_at = 0",
                        (time.time(),),
                    )
                return int(cursor.rowcount or 0)
        finally:
            connection.close()
