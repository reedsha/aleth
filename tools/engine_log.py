"""Phase 40: structured, rotating operational logging.

A daemon that runs for weeks and appends to a flat file will eventually fill the disk and take the
host down with it -- and a plain-text line cannot be joined to the run it belongs to. This module
is the operational record:

* **JSON, one object per line.** A line is machine-readable, so ``jq`` or a log pipeline can filter
  it; :func:`get_logger` callers keep writing plain messages.
* **Every entry carries the run.** The correlation id from ``tools.run_context`` is attached when
  one is set, so a failure can be joined to the intent that caused it (Phase 27) instead of being
  guessed at from timestamps.
* **Rotating, and bounded.** 50 MB per file with three backups, so the record is capped at ~200 MB
  however long the engine runs.

The file lives under the *state* directory, never the user's repository: this is machine state, and
the engine does not litter a person's checkout (Phase 11).
"""

from __future__ import annotations

import datetime
import json
import logging
import logging.handlers
import os
from typing import Any, Optional

# The bound. 50 MB per file, three backups: the operational record can never be what fills a disk.
MAX_BYTES = 50 * 1024 * 1024
BACKUP_COUNT = 3
LOG_FILENAME = "aleth.log"
LOG_SUBDIR = "logs"

_configured_path: str = ""


def _intent_id() -> str:
    """The active run's correlation id, or ``""``. Never raises: logging must not."""
    try:
        from tools.run_context import get_current_intent

        return str(get_current_intent() or "")
    except Exception:
        return ""


class JsonFormatter(logging.Formatter):
    """One JSON object per record: timestamp, level, logger, message, and the run it belongs to."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.datetime.fromtimestamp(
                record.created, datetime.timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        intent_id = _intent_id()
        if intent_id:
            payload["intent_id"] = intent_id
        if record.exc_info:
            # The traceback, not a stringified exception: a stack is the thing a person needs.
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def log_path(log_dir: Optional[str] = None) -> str:
    """Where the operational log lives. Defaults to ``<state_dir>/logs/aleth.log``."""
    if log_dir is None:
        from tools.workspace import state_dir

        log_dir = os.path.join(state_dir(), LOG_SUBDIR)
    return os.path.join(str(log_dir), LOG_FILENAME)


def configure(log_dir: Optional[str] = None, *, level: int = logging.INFO) -> str:
    """Attach the rotating JSON handler to the root logger. Idempotent. Returns the path.

    Idempotent because the engine boots through several entry points (``main.py``, the CLI, a test
    fixture) and a second handler would double every line.
    """
    global _configured_path
    path = log_path(log_dir)
    if _configured_path == path:
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8",
    )
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.setLevel(level)
    # Replace any previous Aleth handler rather than stacking one per boot.
    for existing in list(root.handlers):
        if getattr(existing, "_aleth_engine_log", False):
            root.removeHandler(existing)
    handler._aleth_engine_log = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    _configured_path = path
    return path


def get_logger(name: str) -> logging.Logger:
    """A logger for one module. The handler is the root's, attached by :func:`configure`."""
    return logging.getLogger(str(name))


__all__ = [
    "BACKUP_COUNT",
    "JsonFormatter",
    "LOG_FILENAME",
    "MAX_BYTES",
    "configure",
    "get_logger",
    "log_path",
]
