"""Phase 45: the schema migration engine. Linear, raw SQL, and strictly ordered.

Before this, the state database was created by four modules and evolved by guarded ``ALTER``s
scattered across them. That works until a column has to be *added to a database that already
exists*: ``CREATE TABLE IF NOT EXISTS`` does nothing to a table that is already there, so the first
release that widened a table would leave every existing user's daemon crashing on
``OperationalError: no such column``.

The replacement is deliberately small. ``schema_version`` holds one integer; ``storage/migrations/``
holds numbered ``.sql`` patches; this module applies every patch newer than the recorded version,
each inside one ``BEGIN EXCLUSIVE`` transaction that also bumps the version -- so a patch that fails
rolls back *including* its number, and a half-migrated database cannot exist. A failure raises, and
the boot path refuses to start rather than serving a database whose shape it does not know.

No ORM, no migration framework: the patches are the SQL a person would have typed, and the runner is
the transaction and the ordering. That is the whole mechanism, and it is auditable by reading it.
"""

from __future__ import annotations

import os
import re
import sqlite3
from typing import List, Optional, Tuple

# The patches, beside this module so a wheel ships them with the package.
MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")

# ``NNN_name.sql``. The number is the version; anything else in the directory is ignored, so a
# README or an editor's backup cannot be mistaken for a patch.
_FILENAME_RE = re.compile(r"^(\d+)_[A-Za-z0-9_\-]+\.sql$")

VERSION_TABLE = "schema_version"

# A repair ``ALTER``, recognised by its own syntax -- our SQL, not SQLite's error text. The baseline
# must add a column a pre-baseline database is missing, and SQLite has no ``ADD COLUMN IF NOT
# EXISTS``; so the runner decides from the catalog whether the ``ALTER`` is still needed, and skips
# it when it is not. Deterministic, and independent of any message a binding happens to produce.
_ADD_COLUMN_RE = re.compile(
    r"^\s*ALTER\s+TABLE\s+[\"'\[]?(\w+)[\"'\]]?\s+ADD\s+COLUMN\s+[\"'\[]?(\w+)",
    re.IGNORECASE,
)
_DROP_COLUMN_RE = re.compile(
    r"^\s*ALTER\s+TABLE\s+[\"'\[]?(\w+)[\"'\]]?\s+DROP\s+COLUMN\s+[\"'\[]?(\w+)",
    re.IGNORECASE,
)


def _column_present(connection: sqlite3.Connection, table: str, column: str) -> bool:
    """Whether ``table`` already has ``column``, asked of the catalog (``PRAGMA table_info``).

    A missing table answers ``False``; the caller decides what that means for its statement.
    """
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return any(str(row[1]) == column for row in rows)


def _already_satisfied(connection: sqlite3.Connection, statement: str) -> bool:
    """Whether a repair ``ALTER`` has nothing left to do, decided by inspection, never by error.

    Only ``ADD COLUMN`` and ``DROP COLUMN`` are treated this way: they are the two statements whose
    *intent* (the column's presence) is observable in the catalog before they run. Everything else
    executes unconditionally, and a genuine failure still rolls the whole patch back.
    """
    add = _ADD_COLUMN_RE.match(statement)
    if add is not None and _column_present(connection, add.group(1), add.group(2)):
        return True
    drop = _DROP_COLUMN_RE.match(statement)
    if drop is not None and not _column_present(connection, drop.group(1), drop.group(2)):
        return True
    return False


class MigrationError(RuntimeError):
    """A patch failed, or the sequence is unusable. The engine must not start."""


def migration_files(directory: Optional[str] = None) -> List[Tuple[int, str]]:
    """Every ``NNN_*.sql`` in ``directory``, as ``(version, path)``, ordered by version.

    **Strict**, and deliberately so: a duplicate version or a gap in the sequence is a refusal,
    because a numbered sequence with a hole in it is a sequence whose order is a guess. A silent
    guess about schema order is how a database ends up in a shape nobody designed.
    """
    base = str(directory or MIGRATIONS_DIR)
    try:
        names = sorted(os.listdir(base))
    except OSError as error:
        raise MigrationError(
            f"the migrations directory {base!r} could not be read: {error}"
        ) from error
    found: List[Tuple[int, str]] = []
    for name in names:
        match = _FILENAME_RE.match(name)
        if match is not None:
            found.append((int(match.group(1)), os.path.join(base, name)))
    found.sort(key=lambda item: item[0])
    versions = [version for version, _ in found]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"duplicate migration versions in {base!r}: {versions}")
    if versions and versions != list(range(1, len(versions) + 1)):
        raise MigrationError(
            f"the migration sequence must be contiguous from 1, got {versions}"
        )
    return found


def _statements(script: str) -> List[str]:
    """The script's statements, in order. ``--`` comment lines and blank lines are dropped.

    Deliberately **not** ``executescript``: that helper issues an implicit ``COMMIT`` before it runs
    anything, which would end the transaction the patch is supposed to run inside. Splitting on the
    terminator is safe here because no patch contains a ``;`` inside a string literal, and a patch
    that did would be a reason to look again rather than to guess.
    """
    kept: List[str] = []
    for line in str(script or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        kept.append(line)
    body = "\n".join(kept)
    return [statement.strip() for statement in body.split(";") if statement.strip()]


def current_version(connection: sqlite3.Connection) -> int:
    """``schema_version.version``, or ``0`` when the table is absent or empty.

    Zero is the honest answer for a database written before the runner existed: it has a schema, but
    no record of which one, so the baseline patch is applied to it -- and the baseline is idempotent
    precisely so that is safe.
    """
    exists = connection.execute(
        f"SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = '{VERSION_TABLE}'"
    ).fetchone()
    if exists is None:
        return 0
    row = connection.execute(f"SELECT version FROM {VERSION_TABLE} LIMIT 1").fetchone()
    return int(row[0]) if row is not None else 0


def _bootstrap_version(connection: sqlite3.Connection) -> int:
    """Create ``schema_version`` if absent, and return the recorded version.

    Its own small exclusive transaction, so two processes booting at once cannot both decide the
    table is missing and insert a second row.
    """
    connection.execute("BEGIN EXCLUSIVE TRANSACTION")
    try:
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {VERSION_TABLE} (version INTEGER NOT NULL)"
        )
        row = connection.execute(f"SELECT version FROM {VERSION_TABLE} LIMIT 1").fetchone()
        if row is None:
            connection.execute(f"INSERT INTO {VERSION_TABLE} (version) VALUES (0)")
            version = 0
        else:
            version = int(row[0])
        connection.execute("COMMIT")
        return version
    except Exception:
        connection.execute("ROLLBACK")
        raise


def migrate(
    connection: sqlite3.Connection, *, directory: Optional[str] = None
) -> int:
    """Bring ``connection`` up to the newest patch. Returns the version now.

    One transaction per patch, and the ``schema_version`` bump is inside it: a patch that fails
    leaves neither its statements nor its number behind. The first failure raises
    :class:`MigrationError` and stops -- later patches are not attempted against a schema the
    earlier one failed to establish.
    """
    # Explicit transaction control for the duration: the patches issue their own ``BEGIN
    # EXCLUSIVE``, and a connection in the driver's implicit mode would refuse to nest one.
    previous_isolation = connection.isolation_level
    connection.isolation_level = None
    try:
        version = _bootstrap_version(connection)
        for number, path in migration_files(directory):
            if number <= version:
                continue
            with open(path, "r", encoding="utf-8") as handle:
                script = handle.read()
            try:
                connection.execute("BEGIN EXCLUSIVE TRANSACTION")
                for statement in _statements(script):
                    if _already_satisfied(connection, statement):
                        continue
                    connection.execute(statement)
                connection.execute(
                    f"UPDATE {VERSION_TABLE} SET version = ?", (int(number),)
                )
                connection.execute("COMMIT")
            except Exception as error:
                try:
                    connection.execute("ROLLBACK")
                except Exception:  # a rollback that cannot run must not mask the cause
                    pass
                raise MigrationError(
                    f"migration {os.path.basename(path)} failed and was rolled back: "
                    f"{type(error).__name__}: {error}"
                ) from error
            version = int(number)
        return version
    finally:
        connection.isolation_level = previous_isolation


def migrate_path(db_path: str, *, directory: Optional[str] = None) -> int:
    """Open a file database with the engine's pragmas and migrate it."""
    from storage.connection import connect

    connection = connect(str(db_path), isolation_level=None)
    try:
        return migrate(connection, directory=directory)
    finally:
        connection.close()


def migrate_on_boot(db_path: Optional[str] = None) -> int:
    """Migrate the active project's database, or refuse to start. Returns the version.

    The boot path: called before the bootloader, the API server or any agent loop, because all three
    assume the schema they were written against. A failure is re-raised so the caller can exit
    non-zero -- an engine that starts on a schema it does not understand is worse than one that
    does not start.
    """
    from storage.connection import default_db_path

    path = os.path.abspath(str(db_path or default_db_path()))
    version = migrate_path(path)
    print(f"[migrate] schema at version {version} ({path})", flush=True)
    return version


__all__ = [
    "MIGRATIONS_DIR",
    "MigrationError",
    "VERSION_TABLE",
    "current_version",
    "migrate",
    "migrate_on_boot",
    "migrate_path",
    "migration_files",
]
