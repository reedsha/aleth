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

# A receipt is written by a child while the orchestrator may be writing task state to the same
# file. WAL plus a bounded wait is what keeps a short append from failing under that contention.
BUSY_TIMEOUT_SECONDS = 10.0

TELEMETRY_DDL = """
                CREATE TABLE IF NOT EXISTS execution_telemetry (
                    execution_id TEXT PRIMARY KEY,
                    session_id   TEXT NOT NULL DEFAULT '',
                    target_tool  TEXT NOT NULL DEFAULT '',
                    exit_code    INTEGER NOT NULL DEFAULT 0,
                    duration_ms  INTEGER NOT NULL DEFAULT 0,
                    stdout_hash  TEXT NOT NULL DEFAULT '',
                    stderr_hash  TEXT NOT NULL DEFAULT '',
                    outcome      TEXT NOT NULL DEFAULT '',
                    recorded_at  REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_telemetry_session
                    ON execution_telemetry(session_id);
                CREATE INDEX IF NOT EXISTS idx_telemetry_tool
                    ON execution_telemetry(target_tool);
                CREATE INDEX IF NOT EXISTS idx_telemetry_recorded
                    ON execution_telemetry(recorded_at);
"""

# The routing ledger (Phase 19): one append-only row per *agent* decision. Deliberately its own
# table rather than a row in ``execution_telemetry``: a routing decision is not a container
# execution, and overloading that table's ``execution_id``/``exit_code`` contract with decisions
# that have neither would make every query on it wrong. Same file, same append-only discipline.
ROUTING_DDL = """
                CREATE TABLE IF NOT EXISTS routing_decisions (
                    decision_id  TEXT PRIMARY KEY,
                    session_id   TEXT NOT NULL DEFAULT '',
                    plan_id      TEXT NOT NULL DEFAULT '',
                    task_id      TEXT NOT NULL DEFAULT '',
                    intent       TEXT NOT NULL DEFAULT '',
                    domain       TEXT NOT NULL DEFAULT '',
                    complexity   TEXT NOT NULL DEFAULT '',
                    route        TEXT NOT NULL DEFAULT '',
                    confidence   REAL NOT NULL DEFAULT 0.0,
                    fault        TEXT NOT NULL DEFAULT '',
                    engine       TEXT NOT NULL DEFAULT '',
                    evidence     TEXT NOT NULL DEFAULT '[]',
                    decided_at   REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_routing_session
                    ON routing_decisions(session_id);
                CREATE INDEX IF NOT EXISTS idx_routing_task
                    ON routing_decisions(plan_id, task_id);
                CREATE INDEX IF NOT EXISTS idx_routing_recorded
                    ON routing_decisions(decided_at);
"""

# The agent-fault ledger (Phase 26): one append-only row per *loop* fault -- the model that kept
# calling tools past its ceiling, rather than a container that misbehaved or a route that was
# chosen. Its own table for the same reason as ``routing_decisions``: the fault has no container,
# no exit code and no route, so a row in either of the other two would make their queries wrong.
AGENT_FAULT_DDL = """
                CREATE TABLE IF NOT EXISTS agent_faults (
                    fault_id    TEXT PRIMARY KEY,
                    session_id  TEXT NOT NULL DEFAULT '',
                    kind        TEXT NOT NULL DEFAULT '',
                    role        TEXT NOT NULL DEFAULT '',
                    detail      TEXT NOT NULL DEFAULT '',
                    steps       INTEGER NOT NULL DEFAULT 0,
                    recorded_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_agent_faults_session
                    ON agent_faults(session_id);
                CREATE INDEX IF NOT EXISTS idx_agent_faults_recorded
                    ON agent_faults(recorded_at);
"""

# New columns go here *and* in the DDL above. ``CREATE TABLE IF NOT EXISTS`` never adds a column to
# a table that already exists, so a ledger written by an earlier build is upgraded by the guarded
# ``ALTER`` below rather than silently missing the field.
TELEMETRY_MIGRATIONS = (
    # Phase 13: why an execution ended, when it did not end on its own terms -- ``OOM_KILLED`` when
    # the kernel's OOM killer took the container, ``TIMEOUT`` when the perimeter did. The engine
    # reads it so a memory-leaking command is not mistaken for a transient fault and retried.
    ("outcome", "TEXT NOT NULL DEFAULT ''"),
)


def _connect(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_SECONDS)
    connection.row_factory = sqlite3.Row
    return connection


def apply_telemetry_schema(connection: sqlite3.Connection) -> None:
    """Create all three ledgers on a caller's connection, so a test can build the real tables."""
    connection.executescript(TELEMETRY_DDL)
    connection.executescript(ROUTING_DDL)
    connection.executescript(AGENT_FAULT_DDL)
    # ``PRAGMA table_info`` is read positionally on purpose: this is called with a bare connection
    # (no ``row_factory``), and the column name is index 1.
    existing = {row[1] for row in connection.execute("PRAGMA table_info(execution_telemetry)")}
    for name, ddl in TELEMETRY_MIGRATIONS:
        if name not in existing:
            connection.execute(f"ALTER TABLE execution_telemetry ADD COLUMN {name} {ddl}")


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
    kind: str,
    detail: str = "",
    role: str = "",
    steps: int = 0,
    session_id: str = "",
) -> str:
    """Append one loop fault, returning its id. **Raises** on failure, like :func:`record`.

    Written by the loop that hit the ceiling, because that is the only thing that knows for
    certain -- the same reasoning that has the exec server record its own receipt. ``session_id``
    is empty from there: the workflow layer is handed a message and an action, not an intent id, so
    the *intent's* failure is recorded in the intent ledger by the engine, which is the layer that
    has the id.
    """
    fault_id = str(uuid.uuid4())
    connection = _connect(db_path)
    try:
        with connection:
            connection.execute(
                "INSERT INTO agent_faults"
                " (fault_id, session_id, kind, role, detail, steps, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    fault_id, str(session_id), str(kind), str(role), str(detail), int(steps),
                    time.time(),
                ),
            )
    finally:
        connection.close()
    return fault_id


def agent_faults(db_path: str, limit: int = 50) -> List[Dict[str, Any]]:
    """The newest loop faults first, bounded by ``limit``. Uses ``idx_agent_faults_recorded``."""
    connection = _connect(db_path)
    try:
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
