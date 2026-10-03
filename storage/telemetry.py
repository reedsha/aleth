"""The forensic ledger: one append-only row per container execution.

Raw SQL on purpose. Seven columns and an index do not want an ORM, and -- more to the point -- this
table is written by a **child process**: the exec server records its own receipt, and it should
not have to construct a store (and open the whole schema) to append one row. The store owns the
DDL; this module owns the two statements.

APPEND-ONLY. There is no UPDATE and no DELETE here. A ledger that can be rewritten is not a
ledger, and the ``execution_id`` primary key is what enforces it: re-recording an execution is
refused by SQLite rather than silently replacing the evidence of the first one.

Why a hash and not the output: the streams are truncated to 1 MB by the perimeter
(``tools.stream_drain``), so the ledger stores a SHA-256 of the *full* stream. That is enough to
prove two executions produced identical output, and to prove a stored receipt matches the bytes a
later investigation recovers -- without making the database grow with the output.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

from storage.connection import connect as _sqlite_connect

# A receipt is written by a child while the orchestrator may be writing task state to the same
# file. WAL plus a bounded wait is what keeps a short append from failing under that contention.
# (Both now live in ``storage.connection``, applied to every connection in the engine.)

# The three telemetry ledgers' schema lives in ``storage/migrations/`` (Phase 45). It used to be
# three DDL strings plus guarded ``ALTER``s in this module; the versioned runner now owns creation
# and evolution, so a column added here reaches every database that predates it.


def _connect(db_path: str) -> sqlite3.Connection:
    # One configured connection: WAL, ``synchronous=NORMAL``, a bounded wait and foreign keys
    # (``storage.connection``). A receipt is written while the orchestrator may be writing task
    # state to the same file, so the journal mode is what keeps a short append from failing.
    return _sqlite_connect(db_path)


def apply_telemetry_schema(connection: sqlite3.Connection) -> None:
    """Bring a caller's connection up to the current schema (Phase 45).

    Delegates to the migration runner: the telemetry ledgers are part of the one state database,
    and the runner is now the only thing that creates or evolves it.
    """
    from storage import migrations

    migrations.migrate(connection)


def record(
    db_path: str,
    *,
    execution_id: str,
    session_id: str = "",
    target_tool: str = "",
    exit_code: int = 0,
    duration_ms: int = 0,
    stdout_hash: str = "",
    stderr_hash: str = "",
    outcome: str = "",
) -> None:
    """Append one receipt. **Raises** on failure: the caller decides what a lost receipt means.

    Swallowing here would make a lost receipt indistinguishable from a clean run, which is the
    one thing a forensic ledger must never be.
    """
    connection = _connect(db_path)
    try:
        with connection:
            connection.execute(
                "INSERT INTO execution_telemetry"
                " (execution_id, session_id, target_tool, exit_code, duration_ms,"
                "  stdout_hash, stderr_hash, outcome, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(execution_id), str(session_id), str(target_tool), int(exit_code),
                    int(duration_ms), str(stdout_hash), str(stderr_hash), str(outcome), time.time(),
                ),
            )
    finally:
        connection.close()


def record_routing_decision(
    db_path: str,
    *,
    route: str,
    complexity: str = "",
    intent: str = "",
    domain: str = "",
    confidence: float = 0.0,
    fault: str = "",
    engine: str = "",
    evidence: Optional[List[str]] = None,
    session_id: str = "",
    plan_id: str = "",
    task_id: str = "",
) -> str:
    """Append one routing decision, returning its id. **Raises** on failure, like :func:`record`.

    A decision the engine cannot write down is a decision it cannot claim to have made; the
    caller sees the failure rather than a plan that silently ran unrouted.
    """
    decision_id = str(uuid.uuid4())
    connection = _connect(db_path)
    try:
        with connection:
            connection.execute(
                "INSERT INTO routing_decisions"
                " (decision_id, session_id, plan_id, task_id, intent, domain, complexity,"
                "  route, confidence, fault, engine, evidence, decided_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision_id, str(session_id), str(plan_id), str(task_id), str(intent),
                    str(domain), str(complexity), str(route), float(confidence), str(fault),
                    str(engine), json.dumps([str(item) for item in (evidence or [])]),
                    time.time(),
                ),
            )
    finally:
        connection.close()
    return decision_id


def routing_decisions_for_task(db_path: str, plan_id: str, task_id: str) -> List[Dict[str, Any]]:
    """Every routing decision recorded for one node, oldest first. Uses ``idx_routing_task``."""
    connection = _connect(db_path)
    try:
        rows = connection.execute(
            "SELECT * FROM routing_decisions WHERE plan_id = ? AND task_id = ?"
            " ORDER BY decided_at, decision_id",
            (str(plan_id), str(task_id)),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def recent_routing_decisions(db_path: str, limit: int = 50) -> List[Dict[str, Any]]:
    """The newest routing decisions first, bounded by ``limit``. Uses ``idx_routing_recorded``."""
    connection = _connect(db_path)
    try:
        rows = connection.execute(
            "SELECT * FROM routing_decisions ORDER BY decided_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def record_agent_fault(
    db_path: str,
    *,
    intent_id: str,
    kind: str,
    detail: str = "",
    role: str = "",
    steps: int = 0,
) -> str:
    """Append one loop fault, returning its id. **Raises** on failure, like :func:`record`.

    ``intent_id`` is required and must not be blank: a fault that cannot be joined to the run that
    caused it is not evidence. In a concurrent engine it is worse than nothing -- a detached row
    reads as noise, and the reader cannot tell which of the night's runs it belongs to.

    Written by the loop that hit the ceiling, because that is the only thing that knows for certain,
    exactly as the exec server records its own receipt.
    """
    resolved = str(intent_id or "").strip()
    if not resolved:
        raise ValueError("a fault must belong to an intent; intent_id is required")
    fault_id = str(uuid.uuid4())
    connection = _connect(db_path)
    try:
        with connection:
            connection.execute(
                "INSERT INTO agent_faults"
                " (fault_id, intent_id, kind, role, detail, steps, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    fault_id, resolved, str(kind), str(role), str(detail), int(steps),
                    time.time(),
                ),
            )
    finally:
        connection.close()
    return fault_id


def agent_faults(
    db_path: str, *, intent_id: str = "", limit: int = 50
) -> List[Dict[str, Any]]:
    """The newest loop faults first, bounded by ``limit``. Uses ``idx_agent_faults_recorded``.

    ``intent_id`` narrows it to one run, which is the join the correlation id exists for; without it
    the answer is the engine's recent fault history. The narrowed read uses ``idx_agent_faults_intent``.
    """
    connection = _connect(db_path)
    try:
        if str(intent_id or "").strip():
            rows = connection.execute(
                "SELECT * FROM agent_faults WHERE intent_id = ?"
                " ORDER BY recorded_at DESC LIMIT ?",
                (str(intent_id), max(1, int(limit))),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM agent_faults ORDER BY recorded_at DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def receipt(db_path: str, execution_id: str) -> Optional[Dict[str, Any]]:
    """One receipt by execution id, or ``None``."""
    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT * FROM execution_telemetry WHERE execution_id = ?", (str(execution_id),)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        connection.close()


def receipts_for_session(db_path: str, session_id: str) -> List[Dict[str, Any]]:
    """Every receipt for one session, in execution order. Uses ``idx_telemetry_session``."""
    connection = _connect(db_path)
    try:
        rows = connection.execute(
            "SELECT * FROM execution_telemetry WHERE session_id = ? ORDER BY recorded_at",
            (str(session_id),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def count(db_path: str) -> int:
    """How many receipts the ledger holds."""
    connection = _connect(db_path)
    try:
        row = connection.execute("SELECT COUNT(*) AS n FROM execution_telemetry").fetchone()
        return int(row["n"] if row is not None else 0)
    finally:
        connection.close()


def recent(db_path: str, limit: int = 50) -> List[Dict[str, Any]]:
    """The newest receipts first, bounded by ``limit``. Uses ``idx_telemetry_recorded``.

    This is the read the API gateway serves (``GET /api/telemetry``): a bounded window, ordered
    by the index, so a long-lived ledger cannot turn one HTTP request into a full scan.
    """
    connection = _connect(db_path)
    try:
        rows = connection.execute(
            "SELECT * FROM execution_telemetry ORDER BY recorded_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()
