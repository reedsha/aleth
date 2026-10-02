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

import json
import os
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
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    -- Phase 33: the user's steering payload, as a JSON array of corrections. Written
                    -- by the interrupt API while the run is paused, drained by the loop when it
                    -- resumes -- so a correction cannot be lost between the two.
                    pending_input     TEXT NOT NULL DEFAULT '[]',
                    -- Phase 35: the step a paused run should be rewound to when it resumes. Written
                    -- by the rollback API (which also restores the shadow), drained by the loop
                    -- exactly once. 0 means "no rollback queued".
                    rollback_step     INTEGER NOT NULL DEFAULT 0
                );

                -- Phase 35: what each *step* cost, so a rollback can truncate the bill as well as
                -- the files. The cumulative columns above are the breaker's fast read; this is the
                -- ledger behind them, and it is what makes "forget steps 13-15" expressible.
                CREATE TABLE IF NOT EXISTS intent_step_spend (
                    intent_id         TEXT NOT NULL,
                    step              INTEGER NOT NULL,
                    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    recorded_at       REAL NOT NULL,
                    PRIMARY KEY (intent_id, step)
                );

                CREATE INDEX IF NOT EXISTS idx_intent_status
                    ON intent_ledger(status);
                CREATE INDEX IF NOT EXISTS idx_intent_created
                    ON intent_ledger(created_at);
                CREATE INDEX IF NOT EXISTS idx_intent_step
                    ON intent_step_spend(intent_id, step);
"""

# New columns go here *and* in the DDL above -- ``CREATE TABLE IF NOT EXISTS`` never adds a column
# to a table that already exists.
INTENT_MIGRATIONS = (
    ("prompt_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("completion_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("pending_input", "TEXT NOT NULL DEFAULT '[]'"),
    ("rollback_step", "INTEGER NOT NULL DEFAULT 0"),
)

# The status a run takes when the user interrupts it (Phase 33). Not terminal: the run is *held*, and
# resumes when the user says what to change. Distinct from ``failed`` on purpose -- nothing went
# wrong, a person intervened.
PAUSED = "paused_awaiting_input"

# The status the engine writes when *it* is what stopped the run (Phase 36): a SIGTERM, a SIGINT or
# a crash, written by the shutdown hook before the process leaves. Distinct from ``failed`` for the
# same reason ``PAUSED`` is: nothing the agent did went wrong, the machine did. It is terminal and
# it gates new work, because the user has to be told their run was cut off.
ABORTED_BY_SYSTEM = "aborted_by_system"

# The terminal states. `failed` is the one that gates new work; `stopped` is terminal but was the
# user's own doing, so it is acknowledged by the act of stopping.
TERMINAL = ("completed", "stopped", "failed", ABORTED_BY_SYSTEM)


def _loads_input(raw: Any) -> List[str]:
    """The queued corrections, from the JSON column. A malformed value is an empty queue."""
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


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


# The statuses that *gate* new work until the user acknowledges them. ``failed`` is the agent's
# failure; ``aborted_by_system`` is the engine's own (Phase 36). Both are things the user asked for
# and did not get, so both have to be seen before another intent is accepted.
GATING_STATUSES = ("failed", ABORTED_BY_SYSTEM)


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

    def interrupt(self, intent_id: str, note: str = "") -> bool:
        """Hold a *running* intent for the user. ``True`` when it took.

        Phase 33's hard interruption: the loop checks this state before every LLM call and every
        tool execution, exactly as it checks the abort, and *pauses* rather than failing -- the run
        is not wrong, a person wants to steer it. Conditional on ``running`` so it cannot hold an
        intent that already settled.
        """
        resolved = str(intent_id or "").strip()
        if not resolved:
            return False
        connection = _connect(self.path)
        try:
            with connection:
                cursor = connection.execute(
                    "UPDATE intent_ledger SET status = ?, error = ?, updated_at = ?"
                    " WHERE intent_id = ? AND status = 'running'",
                    (PAUSED, str(note or "interrupted by the user"), time.time(), resolved),
                )
                return int(cursor.rowcount or 0) == 1
        finally:
            connection.close()

    def resume(self, intent_id: str, correction: str = "") -> bool:
        """Release a paused intent, carrying the user's correction. ``True`` when it took.

        The correction is **queued in the ledger**, not handed to the loop directly: the two are
        different processes of the same run (the API thread and the run thread), and the ledger is
        the only thing they share. :meth:`drain_input` is the loop's half of the handoff.
        """
        resolved = str(intent_id or "").strip()
        if not resolved:
            return False
        text = str(correction or "").strip()
        connection = _connect(self.path)
        try:
            with connection:
                row = connection.execute(
                    "SELECT pending_input FROM intent_ledger WHERE intent_id = ? AND status = ?",
                    (resolved, PAUSED),
                ).fetchone()
                if row is None:
                    return False
                queued = _loads_input(row["pending_input"])
                if text:
                    queued.append(text)
                cursor = connection.execute(
                    "UPDATE intent_ledger SET status = 'running', error = '', pending_input = ?,"
                    " updated_at = ? WHERE intent_id = ? AND status = ?",
                    (json.dumps(queued), time.time(), resolved, PAUSED),
                )
                return int(cursor.rowcount or 0) == 1
        finally:
            connection.close()

    def drain_input(self, intent_id: str) -> List[str]:
        """Take the queued corrections for an intent, clearing them. One transaction.

        Read-and-clear together, so a correction is delivered exactly once even if the loop is
        paused again while it is being drained. The loop injects what it gets as ``[user]`` messages
        into the high-fidelity window -- that is the steering wheel.
        """
        resolved = str(intent_id or "").strip()
        if not resolved:
            return []
        connection = _connect(self.path)
        try:
            with connection:
                row = connection.execute(
                    "SELECT pending_input FROM intent_ledger WHERE intent_id = ?", (resolved,)
                ).fetchone()
                if row is None:
                    return []
                queued = _loads_input(row["pending_input"])
                if queued:
                    connection.execute(
                        "UPDATE intent_ledger SET pending_input = '[]', updated_at = ?"
                        " WHERE intent_id = ?",
                        (time.time(), resolved),
                    )
                return queued
        finally:
            connection.close()

    def record_step_spend(self, intent_id: str, step: int, *, prompt: int = 0, completion: int = 0) -> None:
        """Record what one *step* cost, and add it to the intent's cumulative total.

        Two writes, on purpose. The cumulative columns are what the breaker reads before every
        call -- one indexed read, no aggregation -- while the per-step rows are the ledger that
        makes a rollback's truncation expressible (Phase 35). Both are upserts, so a retried step
        adds to its own row rather than replacing it.
        """
        resolved = str(intent_id or "").strip()
        if not resolved:
            return
        prompt_tokens = max(0, int(prompt))
        completion_tokens = max(0, int(completion))
        if prompt_tokens == 0 and completion_tokens == 0:
            return
        connection = _connect(self.path)
        try:
            with connection:
                connection.execute(
                    "INSERT INTO intent_step_spend"
                    " (intent_id, step, prompt_tokens, completion_tokens, recorded_at)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(intent_id, step) DO UPDATE SET"
                    " prompt_tokens = prompt_tokens + excluded.prompt_tokens,"
                    " completion_tokens = completion_tokens + excluded.completion_tokens,"
                    " recorded_at = excluded.recorded_at",
                    (resolved, int(step), prompt_tokens, completion_tokens, time.time()),
                )
        finally:
            connection.close()
        self.add_tokens(resolved, prompt=prompt_tokens, completion=completion_tokens)

    def truncate_to_step(self, intent_id: str, step: int) -> Tuple[int, int]:
        """Forget every step's spend after ``step`` and recompute the cumulative total.

        The bill follows the files: a run rewound to step 12 must not keep paying for steps 13-15.
        Returns the recomputed ``(prompt_tokens, completion_tokens)``.

        An intent with no per-step rows at all is left alone rather than zeroed: the rows are the
        authority for the truncation, and inventing a total from an empty ledger would *under*-count
        a real bill. The current cumulative is returned unchanged in that case.
        """
        resolved = str(intent_id or "").strip()
        if not resolved:
            return (0, 0)
        connection = _connect(self.path)
        try:
            with connection:
                counted = connection.execute(
                    "SELECT COUNT(*) FROM intent_step_spend WHERE intent_id = ?", (resolved,)
                ).fetchone()
                if not counted or int(counted[0] or 0) == 0:
                    row = connection.execute(
                        "SELECT prompt_tokens, completion_tokens FROM intent_ledger"
                        " WHERE intent_id = ?",
                        (resolved,),
                    ).fetchone()
                    return (int(row[0] or 0), int(row[1] or 0)) if row is not None else (0, 0)
                connection.execute(
                    "DELETE FROM intent_step_spend WHERE intent_id = ? AND step > ?",
                    (resolved, int(step)),
                )
                row = connection.execute(
                    "SELECT COALESCE(SUM(prompt_tokens), 0), COALESCE(SUM(completion_tokens), 0)"
                    " FROM intent_step_spend WHERE intent_id = ?",
                    (resolved,),
                ).fetchone()
                prompt_tokens = int(row[0] or 0)
                completion_tokens = int(row[1] or 0)
                connection.execute(
                    "UPDATE intent_ledger SET prompt_tokens = ?, completion_tokens = ?,"
                    " updated_at = ? WHERE intent_id = ?",
                    (prompt_tokens, completion_tokens, time.time(), resolved),
                )
                return (prompt_tokens, completion_tokens)
        finally:
            connection.close()

    def request_rollback(self, intent_id: str, step: int) -> bool:
        """Queue a rewind to ``step`` for a *paused* run. ``True`` when it took.

        Conditional on ``paused_awaiting_input``: a rewind is an operator decision made at a hold,
        and the files are restored by the caller in the same breath. The loop drains it on resume
        (Phase 35), so the context window and the bill are rewound with the files.
        """
        resolved = str(intent_id or "").strip()
        if not resolved:
            return False
        connection = _connect(self.path)
        try:
            with connection:
                cursor = connection.execute(
                    "UPDATE intent_ledger SET rollback_step = ?, updated_at = ?"
                    " WHERE intent_id = ? AND status = ?",
                    (max(0, int(step)), time.time(), resolved, PAUSED),
                )
                return int(cursor.rowcount or 0) == 1
        finally:
            connection.close()

    def drain_rollback(self, intent_id: str) -> int:
        """Take the queued rewind target, clearing it. ``0`` means none was queued.

        Read-and-clear together, like :meth:`drain_input`: a rewind is applied exactly once even if
        the loop is paused again while it is being drained.
        """
        resolved = str(intent_id or "").strip()
        if not resolved:
            return 0
        connection = _connect(self.path)
        try:
            with connection:
                row = connection.execute(
                    "SELECT rollback_step FROM intent_ledger WHERE intent_id = ?", (resolved,)
                ).fetchone()
                if row is None:
                    return 0
                target = int(row[0] or 0)
                if target > 0:
                    connection.execute(
                        "UPDATE intent_ledger SET rollback_step = 0, updated_at = ?"
                        " WHERE intent_id = ?",
                        (time.time(), resolved),
                    )
                return target
        finally:
            connection.close()

    def abort_running(self, reason: str, status: str = ABORTED_BY_SYSTEM) -> int:
        """Mark every ``running`` intent terminal from outside its own run. Returns how many.

        What the engine's shutdown hook calls (Phase 36): a daemon that is dying must not leave an
        intent ``running`` in the ledger for the next boot to puzzle over -- and the user must be
        told the run was cut off by the machine, not by the agent. Conditional on ``running`` so it
        cannot rewrite an intent that already settled: the engine's own terminal write wins.
        """
        connection = _connect(self.path)
        try:
            with connection:
                cursor = connection.execute(
                    "UPDATE intent_ledger SET status = ?, error = ?, updated_at = ?"
                    " WHERE status = 'running'",
                    (str(status), str(reason or ""), time.time()),
                )
                return int(cursor.rowcount or 0)
        finally:
            connection.close()

    def status_of(self, intent_id: str) -> str:
        """The intent's current status, or ``""`` when it is unknown. One indexed read."""
        resolved = str(intent_id or "").strip()
        if not resolved:
            return ""
        connection = _connect(self.path)
        try:
            row = connection.execute(
                "SELECT status FROM intent_ledger WHERE intent_id = ?", (resolved,)
            ).fetchone()
            return str(row["status"]) if row is not None else ""
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
        """The newest terminal failure the user has not acknowledged, or ``None``.

        This is the gate. While it answers a row, the UI must not accept a new intent: the user has
        not yet been told that the last one did not do what they asked. It covers the engine's own
        abort as well as the agent's failure (Phase 36) -- a run cut off by a SIGTERM is just as
        much a run the user is owed an explanation for.
        """
        placeholders = ", ".join("?" for _ in GATING_STATUSES)
        connection = _connect(self.path)
        try:
            row = connection.execute(
                f"SELECT * FROM intent_ledger WHERE status IN ({placeholders})"
                " AND acknowledged_at = 0 ORDER BY updated_at DESC LIMIT 1",
                GATING_STATUSES,
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
                    placeholders = ", ".join("?" for _ in GATING_STATUSES)
                    cursor = connection.execute(
                        "UPDATE intent_ledger SET acknowledged_at = ?"
                        f" WHERE status IN ({placeholders}) AND acknowledged_at = 0",
                        (time.time(), *GATING_STATUSES),
                    )
                return int(cursor.rowcount or 0)
        finally:
            connection.close()


def abort_running_intents(reason: str) -> int:
    """Mark every ``running`` intent in this project's ledger terminal. Never raises.

    What the engine's shutdown hook calls (Phase 36). A daemon that is dying must not leave an
    intent ``running`` for the next boot to puzzle over -- and the user has to be told their run was
    cut off by the *machine*, not by the agent, which is why the status is its own.

    Best effort by contract: a shutdown that cannot tidy is still a shutdown, and an exception on a
    signal path would replace the sender's exit status with a traceback. A project that has never
    run has no ledger, and that is not a fault.
    """
    try:
        # Imported lazily: ``storage.db`` builds the state paths, and this module is imported from
        # inside it on some paths -- a module-level import here would close that loop.
        from storage.db import default_db_path

        path = default_db_path()
        if not os.path.isfile(path):
            return 0
        return IntentLedger(path).abort_running(str(reason or ""))
    except Exception as error:
        print(
            f"[State] the running intents could not be marked aborted: "
            f"{type(error).__name__}: {error}",
            flush=True,
        )
        return 0
