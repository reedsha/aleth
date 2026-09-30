"""The handful of settings the app can change from inside the app.

Everything the app needs to reach a provider is already environment-driven: ``.env`` is
loaded by ``env_boot`` and deliberately wins over the process environment,
``agents/model_routing`` reads its routes per call, and ``orchestration/system2`` reads the
endpoint per call. So changing a route or a key is a *write* to one file plus this process's
environment -- no reload, no restart, and the next workflow picks it up.

Three rules make that safe to expose to a window:

* **The key is write-only.** ``OPENAI_API_KEY`` is the one secret here, and its value never
  crosses the bridge: the panel is told whether it is set and how long it is, and can send
  a new one, but nothing can read the current one back out of the webview.
* **Only allowlisted names are accepted.** The bridge takes a name/value pair *from* the
  webview; without a fixed list, a devtools session (or a bug) could write an arbitrary
  variable into the file every later launch loads.
* **A blank value clears.** An empty field means "unset", and it is written as an empty
  assignment and removed from this process, so a cleared route falls back to its default
  rather than to a stale value nobody can see.

This module also owns each name's default, so the panel and the code that reads the values
cannot disagree about what "not set" means.
"""

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from tools.payloads import SettingsPayload, validated
from tools.workspace import PROJECT_ROOT

ENV_FILENAME = ".env"

# Our own view of the file. The value of a non-secret name is returned as text; the secret is
# only ever described.
FIELDS: List[Dict[str, Any]] = [
    {
        "name": "OPENAI_API_KEY",
        "label": "Provider API key",
        "secret": True,
        "help": "Sent to the provider. Never read back into this window.",
    },
    {
        "name": "OPENAI_BASE_URL",
        "label": "Provider base URL",
        "secret": False,
        "help": "An OpenAI-compatible endpoint, e.g. https://router.example/v1",
    },
    {
        "name": "ALETH_ARCHITECT_MODEL",
        "label": "Architect model",
        "secret": False,
        "help": "The route the Gatekeeper runs on.",
    },
    {
        "name": "ALETH_CODER_DEEP_MODEL",
        "label": "Coder model (deep)",
        "secret": False,
        "help": "The route for core logic and UI implementation.",
    },
    {
        "name": "ALETH_CODER_STANDARD_MODEL",
        "label": "Coder model (standard)",
        "secret": False,
        "help": "The route for tests, boilerplate and documentation.",
    },
]

CONFIGURABLE_NAMES: Tuple[str, ...] = tuple(field["name"] for field in FIELDS)

# A value that needs no quoting when written back. Anything else (spaces, quotes, a `#`) is
# quoted, so a value can never terminate its own line and inject a second variable.
_UNQUOTED_RE = re.compile(r"^[A-Za-z0-9_./:@+~-]*$")

# ``NAME=value`` or ``export NAME=value``. The prefix is remembered so a line the author
# wrote as an export stays one.
_ASSIGNMENT_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", re.IGNORECASE)


def _defaults() -> Dict[str, str]:
    """Each name's default, taken from the module that owns the route literals.

    Imported at call time, and its failure is tolerated on purpose. ``agents`` builds the
    Architect on import, which needs provider credentials -- so without this the settings
    panel could not be *read* when no key is configured, which is exactly the state it
    exists to get you out of. A route whose default cannot be resolved reports no default
    rather than a wrong one.
    """
    routes = {
        "ALETH_ARCHITECT_MODEL": "",
        "ALETH_CODER_DEEP_MODEL": "",
        "ALETH_CODER_STANDARD_MODEL": "",
    }
    try:
        from agents.model_routing import (
            DEFAULT_ARCHITECT,
            DEFAULT_CODER_DEEP,
            DEFAULT_CODER_STANDARD,
        )

        routes = {
            "ALETH_ARCHITECT_MODEL": DEFAULT_ARCHITECT,
            "ALETH_CODER_DEEP_MODEL": DEFAULT_CODER_DEEP,
            "ALETH_CODER_STANDARD_MODEL": DEFAULT_CODER_STANDARD,
        }
    except Exception:
        pass

    return {
        "OPENAI_API_KEY": "",
        # The OpenAI SDK's own default, so "unset" here means the same endpoint the SDK
        # would otherwise use.
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        **routes,
    }


def env_file_path(env_path: Optional[str] = None) -> str:
    """The ``.env`` this module reads and writes, resolved against the app root."""
    return env_path or os.path.join(PROJECT_ROOT, ENV_FILENAME)


def _name_of(line: str) -> Optional[str]:
    """The variable name a dotenv line assigns, or ``None`` for anything else."""
    match = _ASSIGNMENT_RE.match(line)
    return match.group(1) if match else None


def _quote(value: str) -> str:
    """A value as it can be written after ``NAME=`` without escaping its line.

    A literal CR or LF would end the assignment early and could inject a second
    variable into the file, so newlines are folded to spaces before quoting.
    """
    value = value.replace("\r", " ").replace("\n", " ")
    if _UNQUOTED_RE.match(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def read_settings(env_path: Optional[str] = None) -> Dict[str, Any]:
    """The panel's payload: one entry per configurable name.

    A non-secret name reports its value and the value that is actually in force (itself, or
    the default when it is unset). The secret reports neither -- only whether it is set and
    how long it is, which is enough to tell "configured" from "empty" without putting the
    key in a payload a devtools session could read.
    """
    path = env_file_path(env_path)
    defaults = _defaults()
    found = os.path.isfile(path)

    entries = []
    for field in FIELDS:
        name = field["name"]
        value = os.environ.get(name) or ""
        entry = {
            "name": name,
            "label": field["label"],
            "secret": field["secret"],
            "help": field["help"],
            "set": bool(value),
            "length": len(value),
            "default": "" if field["secret"] else defaults.get(name, ""),
            "value": "" if field["secret"] else value,
            "effective": "" if field["secret"] else (value or defaults.get(name, "")),
        }
        entries.append(entry)

    return validated(SettingsPayload, {
        "success": True,
        "found": found,
        "filename": ENV_FILENAME,
        "fields": entries,
        "error": "",
    })


def _rewrite_env(path: str, updates: Dict[str, str]) -> None:
    """Writes ``updates`` into the file, leaving every other line as it was.

    A name that is already assigned is replaced *in place*, so the file keeps its comments,
    its blank lines and its order. A name that is absent is appended. A duplicate assignment
    is collapsed: dotenv takes the last one, so leaving an older line below the new value
    would silently win.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []

    written = set()
    out: List[str] = []
    for line in lines:
        name = _name_of(line)
        if name is not None and name in updates:
            if name in written:
                continue  # a second assignment would shadow the value just written
            prefix = "export " if line.lstrip().lower().startswith("export ") else ""
            out.append(f"{prefix}{name}={_quote(updates[name])}")
            written.add(name)
        else:
            out.append(line)

    for name in CONFIGURABLE_NAMES:
        if name in updates and name not in written:
            out.append(f"{name}={_quote(updates[name])}")

    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")


def save_settings(values: Dict[str, Any], env_path: Optional[str] = None) -> Dict[str, Any]:
    """Persists the given values to ``.env`` and to this process, then reports the result.

    Only allowlisted names are accepted; anything else is reported as ignored rather than
    written. A blank value clears the name, so a route that is cleared falls back to its
    default instead of to a value nobody can see.

    The environment is updated here as well as on disk because that is what makes the change
    take effect now: the route and endpoint readers consult ``os.environ`` on every call.
    """
    values = values or {}
    updates: Dict[str, str] = {}
    ignored: List[str] = []

    for raw_name, raw_value in values.items():
        name = str(raw_name or "").strip()
        if name not in CONFIGURABLE_NAMES:
            ignored.append(name)
            continue
        updates[name] = str(raw_value if raw_value is not None else "").strip()

    if updates:
        path = env_file_path(env_path)
        try:
            _rewrite_env(path, updates)
        except OSError as exc:
            return validated(SettingsPayload, {
                "success": False,
                "found": False,
                "filename": ENV_FILENAME,
                "fields": read_settings(env_path)["fields"],
                "error": f"Could not write {ENV_FILENAME}: {exc}",
            })

        for name, value in updates.items():
            if value:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)

    result = read_settings(env_path)
    result["saved"] = sorted(updates)
    result["ignored"] = ignored
    return validated(SettingsPayload, result)
