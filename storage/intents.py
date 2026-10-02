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

**It is also the project's execution mutex (Phase 25).** One intent may be ``running`` per project
at a time, and that is enforced here rather than in the queue, because the queue is in memory and
dies with the process while the ledger is shared by every engine on the project. The claim is a
single conditional ``UPDATE`` -- see :meth:`IntentLedger.claim` -- and it needs no project column
and no second lock file, because this database is *already* per project: its path is
``<state_dir>/<project_id>/aleth_state.db``. "No other intent in this ledger is running" therefore
*is* "no other intent of this project is running".

Raw SQL on purpose -- the same reasoning as ``storage.telemetry``. This is a handful of columns
and two indexed reads, and it is written from the API process while the workflow writes task
state to the same file, so it is short transactions and WAL, not an ORM.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

# The queue and the ledger share one file with the workflow's task writes, so the connection is
# configured centrally (``storage.connection``: WAL, ``synchronous=NORMAL``, a bounded wait).
from storage.connection import connect as _sqlite_connect

INTENT_DDL = """
                CREATE TABLE IF NOT EXISTS intent_ledger (
                    intent_id       TEXT PRIMARY KEY,
                    action_type     TEXT NOT NULL DEFAULT '',
                    message         TEXT NOT NULL DEFAULT '',
                    status          TEXT NOT NULL DEFAULT 'queued',
                    error           TEXT NOT NULL DEFAULT '',
                    created_at      REAL NOT NULL,
                    updated_at      REAL NOT NULL,
                    acknowledged_at REAL NOT NULL DEFAULT 0,
                    -- Phase 32: what the run has *spent*, cumulative. Here rather than in the loop's
                    -- memory because the loop is one process of several -- a swarm child spends
                    -- tokens too -- and a budget that only one of them can see is not a budget.
                    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_intent_status
                    ON intent_ledger(status);
                CREATE INDEX IF NOT EXISTS idx_intent_created
                    ON intent_ledger(created_at);
"""

# New columns go here *and* in the DDL above -- ``CREATE TABLE IF NOT EXISTS`` never adds a column
# to a table that already exists.
INTENT_MIGRATIONS = (
    ("prompt_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("completion_tokens", "INTEGER NOT NULL DEFAULT 0"),
)

# The terminal states. `failed` is the one that gates new work; `stopped` is terminal but was the
# user's own doing, so it is acknowledged by the act of stopping.
TERMINAL = ("completed", "stopped", "failed")


def _connect(db_path: str) -> sqlite3.Connection:
    # One configured connection: WAL, ``synchronous=NORMAL``, a bounded wait and foreign keys
    # (``storage.connection``).
    return _sqlite_connect(db_path)


def apply_intent_schema(connection: sqlite3.Connection) -> None:
    """Create the ledger on a caller's connection, so a test can build the real table."""
    connection.executescript(INTENT_DDL)
    # ``PRAGMA table_info`` is read positionally on purpose: this is called with a bare connection
    # (no ``row_factory``), and the column name is index 1.
    present = {row[1] for row in connection.execute("PRAGMA table_info(intent_ledger)")}
    for name, ddl in INTENT_MIGRATIONS:
        if name not in present:
            connection.execute(f"ALTER TABLE intent_ledger ADD COLUMN {name} {ddl}")


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

    def claim(self, intent: Any) -> bool:
        """Take the project's execution slot for ``intent``. ``True`` when it now owns it.

        The mutex, and it is one statement. This ledger is **per project** -- its path is
        ``<state_dir>/<project_id>/aleth_state.db``, and the state directory is keyed by the
        project id -- so "no other intent in this ledger is running" *is* "no other intent of this
        project is running". No project column, and no second lock file to keep in step with it.

        Atomic on purpose: the ``NOT EXISTS`` and the ``UPDATE`` are one statement inside one
        transaction, so two engines draining the same project cannot both observe "nothing is
        running" and both proceed. The subquery is served by ``idx_intent_status``. Conditional on
        ``status = 'queued'`` so a repeated claim cannot take the slot twice.

        A ``False`` is not a failure -- it is "wait your turn", and the caller leaves the intent
        queued rather than settling it.
        """
        connection = _connect(self.path)
        try:
            with connection:
                cursor = connection.execute(
                    "UPDATE intent_ledger SET status = 'running', updated_at = ?"
                    " WHERE intent_id = ? AND status = 'queued'"
                    " AND NOT EXISTS (SELECT 1 FROM intent_ledger"
                    "                 WHERE status = 'running' AND intent_id <> ?)",
                    (time.time(), str(intent.id), str(intent.id)),
                )
                return int(cursor.rowcount or 0) == 1
        finally:
            connection.close()

    def release(self, intent: Any) -> None:
        """Give the project's execution slot back **without** settling the intent.

        Only the claim is undone: the row returns to ``queued`` so this engine -- or another one --
        may take it later. Used when a run could not be started at all, because a concurrency
        condition is not an outcome: recording one would fail an intent the user never asked to
        fail, and would gate the UI on it.
        """
        self._write(
            "UPDATE intent_ledger SET status = 'queued', updated_at = ?"
            " WHERE intent_id = ? AND status = 'running'",
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

    def is_live(self, intent_id: str) -> bool:
        """Whether ``intent_id`` is still ``running``. One indexed read (``idx_intent_status``).

        The workflow layer's liveness gate (Phase 27). Before it mutates a file or records a fault it
        asks whether the intent it is working for has been aborted underneath it -- a stop the user
        asked for, a breaker that tripped, a supervisor killing the run. A blank id answers ``False``:
        an operation that cannot name its intent has nothing to be live *for*.
        """
        resolved = str(intent_id or "").strip()
        if not resolved:
            return False
        connection = _connect(self.path)
        try:
            row = connection.execute(
                "SELECT 1 FROM intent_ledger WHERE intent_id = ? AND status = 'running'",
                (resolved,),
            ).fetchone()
            return row is not None
        finally:
            connection.close()

    def abort(self, intent_id: str, reason: str = "") -> bool:
        """Mark a *running* intent failed from outside its own run. ``True`` when it took.

        What a parallel abort calls, so an in-flight pass can **see** it: the workflow layer's
        liveness gate reads this row, and a run that has been aborted stops instead of finishing work
        whose result nobody will accept. Conditional on ``running``, so it cannot rewrite an intent
        that already settled -- the engine's own terminal write always wins.
        """
        resolved = str(intent_id or "").strip()
        if not resolved:
            return False
        connection = _connect(self.path)
        try:
            with connection:
                cursor = connection.execute(
                    "UPDATE intent_ledger SET status = 'failed', error = ?, updated_at = ?"
                    " WHERE intent_id = ? AND status = 'running'",
                    (str(reason or ""), time.time(), resolved),
                )
                return int(cursor.rowcount or 0) == 1
        finally:
            connection.close()

    def add_tokens(self, intent_id: str, *, prompt: int = 0, completion: int = 0) -> None:
        """Add what one call spent to this intent's cumulative total. One statement.

        An ``UPDATE`` with ``+=`` rather than a read-modify-write: several processes can be spending
        against the same intent (the swarm's children plan too), and a read-then-write would lose
        whichever increment landed in between.
        """
        resolved = str(intent_id or "").strip()
        if not resolved or (int(prompt) == 0 and int(completion) == 0):
            return
        self._write(
            "UPDATE intent_ledger SET prompt_tokens = prompt_tokens + ?,"
            " completion_tokens = completion_tokens + ?, updated_at = ? WHERE intent_id = ?",
            (max(0, int(prompt)), max(0, int(completion)), time.time(), resolved),
        )

    def token_totals(self, intent_id: str) -> Tuple[int, int]:
        """``(prompt_tokens, completion_tokens)`` spent so far, or ``(0, 0)``. One indexed read.

        The authority the loop's breaker consults before every call and every tool execution: the
        count is durable, so it survives the process that spent it and covers a run split across
        the parent and its swarm children.
        """
        resolved = str(intent_id or "").strip()
        if not resolved:
            return (0, 0)
        connection = _connect(self.path)
        try:
            row = connection.execute(
                "SELECT prompt_tokens, completion_tokens FROM intent_ledger WHERE intent_id = ?",
                (resolved,),
            ).fetchone()
            if row is None:
                return (0, 0)
            return (int(row["prompt_tokens"] or 0), int(row["completion_tokens"] or 0))
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
