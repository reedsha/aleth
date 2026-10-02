"""One configured SQLite connection: WAL, NORMAL, and a bounded wait.

Every writer in this engine shares one database file per project -- the orchestrator writes task
state, the exec server writes its forensic receipt, the API reads for the UI, and the swarm's
children write their plans. SQLite's default rollback journal locks the **whole file** for a write,
so a reader arriving while a writer holds it is ``database is locked`` -- which surfaces as a
crashed engine rather than a slow one.

Three settings fix that, and they belong in exactly one place so that no new call site can forget
them:

* ``journal_mode=WAL`` -- readers and a writer share the file instead of serialising the database.
  It is a property of the *file* rather than of the connection, so setting it per connection is
  idempotent; it is set anyway, because a database created by a build that did not set it would
  otherwise keep the rollback journal forever.
* ``synchronous=NORMAL`` -- the right durability for WAL. A committed transaction survives a
  process crash; only a host power loss can lose the last one.
* ``busy_timeout`` -- a bounded wait for the write lock, which turns "database is locked" from an
  exception into a short pause. Bounded on purpose: a wedged writer must not hang the UI forever,
  and this engine's writes are short, so five seconds is generous.

``foreign_keys=ON`` rides along for the same reason. It is not a concurrency setting, but it is a
*correctness* one -- the knowledge graph's cascading deletes never fire without it -- and it was
previously repeated at every call site, which is a rule that eventually gets forgotten.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

# How long a writer waits for the file before giving up, in milliseconds.
BUSY_TIMEOUT_MS = 5000


def connect(
    db_path: str, *, isolation_level: Optional[str] = "", **kwargs: Any
) -> sqlite3.Connection:
    """A connection with the settings that make concurrent access survivable.

    ``isolation_level=None`` is passed through for the store, which drives its own explicit
    transactions (autocommit mode, so ``BEGIN``/``COMMIT`` are its own); everything else wants the
    driver's implicit transactions.
    """
    connection = sqlite3.connect(
        str(db_path),
        # The driver's own wait, in seconds, set from the same number the pragma below states so
        # there is one budget rather than two that can drift apart.
        timeout=BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=isolation_level,
        **kwargs,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


__all__ = ["BUSY_TIMEOUT_MS", "connect"]
