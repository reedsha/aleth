"""Phase 22: the bound on the engine's own state.

Every other limit in this engine is on what the *agent* may spend -- its CPU, its memory, its
network, its wall clock. This module is the limit on what the engine spends on *itself*, and it
exists because neither half of that state has a natural end:

* the ledgers are **append-only**, and a ledger with no bound is a disk leak with a respectable
  name. It is valuable because it is complete for a window, not because it is complete forever.
* a shadow workspace is a **copy of the user's repository**, written once per execution and only
  removed when a person merges or rejects it. A run that dies before anyone can act leaves its
  copy behind with nothing left that can reach it.

Both are collected by one sweep at boot, beside the container sweep in ``main.py`` and for the
same reason: a process that dies without unwinding leaves garbage that only the next boot can
find. It reports what it found -- a silent collector hides the crash that produced the garbage.

There is no ORM and no migration framework here. Three ``DELETE`` statements keyed on ``rowid``,
whose subqueries are answered by the recency indexes those tables already carry, and one call into
the staging module.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, FrozenSet, Optional, Tuple

from storage.connection import connect as _sqlite_connect
from storage.db import default_db_path
from storage.intents import IntentLedger
from tools import staging

# A receipt is trimmed while the orchestrator may be appending to the same file. The same bounded
# wait the ledgers' own writers use.
BUSY_TIMEOUT_SECONDS = 10.0

# The window the ledger keeps, per table. A thousand executions is far more history than a
# single-machine engine is ever asked to explain, and it stays small enough that the reads the API
# serves (``storage.telemetry.recent``, ``IntentLedger.recent``) are bounded, index-ordered slices.
# The ledgers are trimmed while the orchestrator may be appending to the same file, and the trim
# itself opens several connections. The bounded wait lives in ``storage.connection`` now.
LEDGER_KEEP_ROWS = 1000

# ``(table, the column its recency index is on)``. The tables belong to the modules that write
# them, so one that does not exist yet is skipped rather than created here.
BOUNDED_LEDGERS: Tuple[Tuple[str, str], ...] = (
    ("execution_telemetry", "recorded_at"),
    ("routing_decisions", "decided_at"),
    ("agent_faults", "recorded_at"),
    ("intent_ledger", "created_at"),
)

# The intents whose shadow is still worth keeping: one in flight is using it, one that finished is
# waiting to be reviewed. See :func:`tools.staging.purge_orphaned_stagings` for why the rest are
# unreachable by every endpoint the engine exposes.
SHADOW_RETAINING_STATUSES = ("queued", "running", "completed")

# Why an unfinished intent was failed at boot. Deliberately its own wording rather than a shared
# constant with ``api.server``'s ``RECOVERY_REASON``: this sweep runs *first*, so this is the string
# a user actually sees, and the gateway's reconcile then finds nothing left to fail.
RECOVERY_REASON = "the engine stopped before this intent finished (recovered at boot)"


def _connect(db_path: str) -> sqlite3.Connection:
    # One configured connection (``storage.connection``): WAL, ``synchronous=NORMAL``, a bounded
    # wait and foreign keys. The trim runs at boot beside other writers, so the journal mode is
    # what keeps it from colliding with them.
    return _sqlite_connect(db_path)


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def prune_ledgers(db_path: str, *, keep: int = LEDGER_KEEP_ROWS) -> Dict[str, int]:
    """Keep the newest ``keep`` rows of every bounded ledger; return rows removed per table.

    One statement per table: the subquery is answered by the recency index the table already
    carries (``idx_telemetry_recorded``, ``idx_routing_recorded``, ``idx_intent_created``) and the
    outer predicate removes whatever it did not select. ``rowid`` is the key rather than the
    ledger's own id so the comparison is on an integer, and ``ORDER BY <time> DESC`` breaks a tie
    by insertion order -- so the survivors are deterministic even when a run writes several rows
    within a single clock tick, and the table lands at exactly the bound rather than near it.

    A table holding fewer rows than the bound deletes nothing: the subquery returns every row and
    the ``NOT IN`` matches all of them.
    """
    bound = max(1, int(keep))
    removed: Dict[str, int] = {}
    connection = _connect(db_path)
    try:
        with connection:
            for table, column in BOUNDED_LEDGERS:
                if not _has_table(connection, table):
                    continue
                cursor = connection.execute(
                    f"DELETE FROM {table} WHERE rowid NOT IN"
                    f" (SELECT rowid FROM {table} ORDER BY {column} DESC LIMIT ?)",
                    (bound,),
                )
                removed[table] = int(cursor.rowcount or 0)
    finally:
        connection.close()
    return removed


def _report(report: Dict[str, Any]) -> None:
    """Say what the sweep did. Zeroes are reported too: that it ran is itself information."""
    if report["skipped"]:
        print(f"[State] boot sweep skipped: {report['skipped']}", flush=True)
        return
    trimmed = ", ".join(
        f"{table}={count}" for table, count in sorted(report["pruned"].items())
    ) or "no ledger yet"
    print(
        f"[State] boot sweep: {report['reconciled']} unfinished intent(s) failed, "
        f"{report['trimmed_rows']} ledger row(s) trimmed ({trimmed}), "
        f"{len(report['purged_stagings'])} orphaned shadow(s) removed",
        # Flushed on purpose: stdout is block-buffered when it is a pipe, and a boot report lost to
        # a dead process's unwritten buffer is not a report.
        flush=True,
    )


def sweep_state(db_path: Optional[str] = None, *, keep: int = LEDGER_KEEP_ROWS) -> Dict[str, Any]:
    """Bound the ledgers and collect the shadows a crash left. Returns a report; never raises.

    The order matters. ``IntentLedger.reconcile`` runs first, so an intent whose engine died is
    terminal *before* anything is judged against it -- which is what makes the shadow of a killed
    run collectable on this boot rather than the one after it. Only then is the ledger trimmed and
    the staging base swept.

    A project that has never run has no ledger, and that is not a fault: it means no intent can
    own a shadow, so every shadow on disk is residue. That is the ``keep`` set of nothing.
    """
    report: Dict[str, Any] = {
        "db_path": "",
        "reconciled": 0,
        "pruned": {},
        "trimmed_rows": 0,
        "purged_stagings": [],
        "skipped": "",
    }
    try:
        path = os.path.abspath(db_path or default_db_path())
        report["db_path"] = path
        if os.path.isfile(path):
            ledger = IntentLedger(path)
            report["reconciled"] = ledger.reconcile(RECOVERY_REASON)
            keep_ids: FrozenSet[str] = ledger.ids_with_status(*SHADOW_RETAINING_STATUSES)
            report["pruned"] = prune_ledgers(path, keep=keep)
        else:
            keep_ids = frozenset()
        report["trimmed_rows"] = sum(report["pruned"].values())
        report["purged_stagings"] = staging.purge_orphaned_stagings(keep_ids)
    except Exception as error:  # a boot that cannot tidy is still a boot
        report["skipped"] = f"{type(error).__name__}: {error}"
    _report(report)
    return report


__all__ = [
    "BOUNDED_LEDGERS",
    "LEDGER_KEEP_ROWS",
    "RECOVERY_REASON",
    "SHADOW_RETAINING_STATUSES",
    "prune_ledgers",
    "sweep_state",
]
