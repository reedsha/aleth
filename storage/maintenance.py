"""Phase 46: state pruning and storage reclamation. The daemon cleans up its own mess.

Phase 22 bounded the ledgers by *row count* at boot. That is not enough for a daemon that runs for
months: two things grow without any bound that a row cap does not touch.

* **The age of the rows.** A thousand recent intents is bounded in count but not in time, and the
  heavy column is ``intent_step_spend.context_blob`` -- the loop's full memory, written every step.
  A year of runs is a year of context blobs, and the row cap keeps the newest thousand of them,
  which is exactly the ones a person is least likely to still need.
* **The space a delete leaves behind.** SQLite does not shrink a file when rows are deleted; it puts
  the pages on a free list and reuses them. The file stays at its high-water mark forever, and the
  WAL grows with it until something checkpoints it.

So this module does two things on a timer: it purges terminal intents older than a TTL, and it
reclaims the space -- truncating the WAL and releasing free pages incrementally.

**No full ``VACUUM``, on purpose.** A ``VACUUM`` rewrites the whole database under a write lock, and
in a daemon with an active agent loop that is ``database is locked`` for the run. Reclamation is
``PRAGMA incremental_vacuum``, which is bounded and cooperative, and it is enabled by
``auto_vacuum=INCREMENTAL`` set when the file is created (``storage.migrations``).
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional

from storage.connection import connect as _sqlite_connect

# How long a finished intent's state is kept. Long enough that a person can still read what a run
# did a few weeks ago; short enough that the context blobs do not accumulate forever.
DEFAULT_TTL_DAYS = 30
TTL_ENV = "ALETH_RETENTION_DAYS"

# How often the sweeper wakes. Six hours: frequent enough to bound the WAL between restarts, rare
# enough to be invisible. The first pass runs immediately, so a daemon that restarts often still
# prunes on every boot.
DEFAULT_INTERVAL_SECONDS = 6 * 3600.0
INTERVAL_ENV = "ALETH_MAINTENANCE_SECONDS"

# How many free pages one incremental vacuum may release. Bounded so the sweep cannot hold the write
# lock for a long time and stall an active run; the rest are released on later passes.
INCREMENTAL_VACUUM_PAGES = 1000

# SQLite's default host-parameter limit is 999; the deletes are chunked well under it.
_CHUNK = 400


def retention_days() -> int:
    """The retention window in days, from the environment when it is set to a usable number."""
    raw = (os.environ.get(TTL_ENV) or "").strip()
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_TTL_DAYS


def sweep_interval_seconds() -> float:
    """How often the sweeper wakes, from the environment when it is set to a usable number."""
    raw = (os.environ.get(INTERVAL_ENV) or "").strip()
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_SECONDS


def _chunks(items: List[str], size: int = _CHUNK):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def purge_expired(
    db_path: str, *, ttl_days: Optional[int] = None, now: Optional[float] = None
) -> Dict[str, int]:
    """Delete terminal intents older than the TTL, with their step rows and their shadows.

    Only **terminal** intents are touched -- a ``queued``, ``running`` or ``paused`` run is live
    state, and deleting it out from under the engine would be the opposite of maintenance. The
    ``updated_at`` stamp is the age, so an intent that was acknowledged or settled recently is kept
    even if it was created long ago.

    ``artifacts`` is deliberately **not** touched. It is keyed by ``(plan_id, task_id)`` -- it holds
    a *plan's* deliverables, which a person reviews and merges -- and it carries no intent id, so
    there is no honest way to attribute a row to an expired intent. Deleting a plan's approved code
    because one run of it aged out would destroy work, not reclaim space. The intent-scoped tables
    (``intent_step_spend``, ``agent_faults``) and the shadow directory are what an expired intent
    actually owns.
    """
    from storage.intents import TERMINAL
    from tools import staging

    path = os.path.abspath(str(db_path))
    report = {"intents": 0, "steps": 0, "faults": 0, "staging": 0}
    if not os.path.isfile(path):
        return report

    days = int(ttl_days if ttl_days is not None else retention_days())
    cutoff = float(now if now is not None else time.time()) - days * 86400.0
    connection = _sqlite_connect(path, isolation_level=None)
    expired: List[str] = []
    try:
        marks = ", ".join("?" for _ in TERMINAL)
        expired = [
            str(row[0])
            for row in connection.execute(
                f"SELECT intent_id FROM intent_ledger"
                f" WHERE status IN ({marks}) AND updated_at < ?",
                (*TERMINAL, cutoff),
            ).fetchall()
        ]
        if expired:
            with connection:
                for chunk in _chunks(expired):
                    holes = ", ".join("?" for _ in chunk)
                    report["steps"] += int(connection.execute(
                        f"DELETE FROM intent_step_spend WHERE intent_id IN ({holes})", chunk
                    ).rowcount or 0)
                    report["faults"] += int(connection.execute(
                        f"DELETE FROM agent_faults WHERE intent_id IN ({holes})", chunk
                    ).rowcount or 0)
                    report["intents"] += int(connection.execute(
                        f"DELETE FROM intent_ledger WHERE intent_id IN ({holes})", chunk
                    ).rowcount or 0)
    finally:
        connection.close()

    # The physical half, outside the transaction: the shadow this intent left behind is a directory
    # tree, not a row, and it is the larger of the two.
    for intent_id in expired:
        shadow = staging.find_staging(intent_id=intent_id)
        if shadow is None or not os.path.isdir(shadow.path):
            continue
        staging.remove_tree(shadow.path)
        if not os.path.isdir(shadow.path):
            report["staging"] += 1
    return report


def reclaim(db_path: str) -> Dict[str, Any]:
    """Truncate the WAL, refresh the planner's statistics, and release free pages incrementally.

    ``wal_checkpoint(TRUNCATE)`` rather than ``PASSIVE``: the point is to *shrink* the log, and a
    passive checkpoint leaves it at its high-water mark. If another connection is mid-write the
    pragma reports busy and truncates nothing -- which is the correct outcome, because the
    alternative is blocking an active run. The next pass will get it.
    """
    path = os.path.abspath(str(db_path))
    report: Dict[str, Any] = {"checkpoint": None, "released_pages": 0}
    if not os.path.isfile(path):
        return report
    connection = _sqlite_connect(path, isolation_level=None)
    try:
        row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        report["checkpoint"] = tuple(int(value) for value in row) if row is not None else None
        # ``optimize`` is the one the SQLite docs recommend running periodically: it updates the
        # planner's statistics and only then, when it is cheap. It takes no write lock of note.
        connection.execute("PRAGMA optimize")
        before = int(connection.execute("PRAGMA page_count").fetchone()[0])
        connection.execute(f"PRAGMA incremental_vacuum({INCREMENTAL_VACUUM_PAGES})")
        after = int(connection.execute("PRAGMA page_count").fetchone()[0])
        report["released_pages"] = max(0, before - after)
    finally:
        connection.close()
    return report


def run_maintenance(
    db_path: Optional[str] = None, *, ttl_days: Optional[int] = None, now: Optional[float] = None
) -> Dict[str, Any]:
    """One maintenance pass: purge, trim the bounded ledgers, then reclaim. Never raises.

    The row-count trim runs here too (not only at boot): ``execution_telemetry`` gains one row per
    container execution and ``routing_decisions`` one per dispatch, so a daemon that stays up for
    months used to accumulate both without bound between restarts -- the sweeper pruned only the
    intent tables.
    """
    from storage.connection import default_db_path
    from storage import retention

    path = os.path.abspath(str(db_path or default_db_path()))
    purged = purge_expired(path, ttl_days=ttl_days, now=now)
    trimmed: Dict[str, int] = {}
    try:
        trimmed = retention.prune_ledgers(path)
    except Exception as error:
        # A trim is a bound, not the run: never let it take the reclaim (or the thread) with it.
        trimmed = {"error": f"{type(error).__name__}: {error}"}
    reclaimed = reclaim(path)
    return {
        "db_path": path,
        "purged": purged,
        "trimmed": trimmed,
        "reclaimed": reclaimed,
    }


class MaintenanceThread:
    """A daemon thread that prunes expired state and reclaims storage on an interval.

    The first pass runs immediately, so a daemon that is restarted frequently still prunes on every
    boot; the loop then waits on the stop event rather than sleeping, so ``stop`` is prompt.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        *,
        interval: Optional[float] = None,
        ttl_days: Optional[int] = None,
    ):
        self._db_path = db_path
        self._interval = float(interval if interval is not None else sweep_interval_seconds())
        self._ttl_days = ttl_days
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.runs = 0
        self.last_report: Optional[Dict[str, Any]] = None

    def run_once(self) -> Dict[str, Any]:
        report = run_maintenance(self._db_path, ttl_days=self._ttl_days)
        self.runs += 1
        self.last_report = report
        return report

    def start(self) -> "MaintenanceThread":
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._loop, name="aleth-maintenance", daemon=True
            )
            self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as error:
                # A sweep that cannot run must never take the daemon down: maintenance is an
                # observer of the state, not a part of the run.
                print(
                    f"[maintenance] the sweep failed: {type(error).__name__}: {error}",
                    file=sys.stderr,
                )
            self._stop.wait(self._interval)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)


_ACTIVE: Optional[MaintenanceThread] = None
_ACTIVE_LOCK = threading.Lock()


def start_maintenance(db_path: Optional[str] = None) -> MaintenanceThread:
    """Start the process's sweeper, once. Idempotent."""
    global _ACTIVE
    with _ACTIVE_LOCK:
        if _ACTIVE is None:
            _ACTIVE = MaintenanceThread(db_path).start()
        return _ACTIVE


def stop_maintenance(timeout: float = 5.0) -> None:
    """Stop the process's sweeper, if one is running. Safe to call twice."""
    global _ACTIVE
    with _ACTIVE_LOCK:
        thread, _ACTIVE = _ACTIVE, None
    if thread is not None:
        thread.stop(timeout=timeout)


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_TTL_DAYS",
    "INTERVAL_ENV",
    "INCREMENTAL_VACUUM_PAGES",
    "MaintenanceThread",
    "TTL_ENV",
    "purge_expired",
    "reclaim",
    "retention_days",
    "run_maintenance",
    "start_maintenance",
    "stop_maintenance",
    "sweep_interval_seconds",
]
