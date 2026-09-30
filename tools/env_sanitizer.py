"""The environment a child process is allowed to see: an allow-list, never the host's.

A child that inherits ``os.environ`` inherits every credential the operator has exported --
``OPENAI_API_KEY``, cloud tokens, a GitHub PAT. That is not a theoretical leak: the MCP servers
are spawned from this process, and until this module existed they were handed ``dict(os.environ)``
verbatim. A model-authored tool call reaches those children, so the host's secrets were one
``env`` away from the payload.

The rule is therefore **allow-list, not deny-list**: a name is present because it was named
here, not because it happened to be in the parent. A deny-list would silently pass the next
credential someone exports.

WHAT IS ALLOWED, AND WHY EACH ONE
---------------------------------
* ``PATH`` -- the child has to find its interpreter and its tools.
* ``LANG`` / ``LC_ALL`` / ``LC_CTYPE`` -- the locale a program formats and decodes with. Absent,
  Python falls back to ASCII and a UTF-8 workspace path becomes an error.
* ``TERM`` -- a program that writes progress only when it believes it has a terminal.
* ``ALETH_WORKSPACE_DIR`` -- this app's own workspace pointer, which the child may legitimately
  need to resolve a path the same way the parent does.
* ``HOME`` / ``DOCKER_CONFIG`` -- the container client keeps its *configuration* (registry
  endpoints, context) under ``$HOME/.docker``. Neither is a credential: the credential helper
  reads the token from the OS store, not from the environment. Stripping ``HOME`` does not
  protect a secret, it only breaks authenticated pulls.
* On Windows, ``SYSTEMROOT``/``WINDIR``/``PATHEXT``/``COMSPEC``/``TEMP``/``TMP`` -- without
  these a child process cannot start at all. They carry no credential.

Everything else is dropped, and :func:`stripped_names` reports what was dropped by *name* so a
caller can log the shape of the sanitization without logging a value.

THIS APP'S OWN CONFIGURATION TRAVELS
------------------------------------
The ``ALETH_`` namespace is allowed as a namespace rather than name by name: it is this app's
configuration (the workspace pointer, the runtime binary, the sandbox image, the routing seam),
and none of it is a credential. A client pointed at a socket or a different binary is
*configuration* -- stripping it does not protect a secret, it only breaks the child.

``FAKE_DOCKER_`` is the in-repo runtime double's configuration, which the isolation tests set.
It is inert in production (the double is never the runtime there) and it travels under the same
rule, so the *runtime's* configuration has one story rather than two.

Both namespaces are still screened by :data:`SECRET_NAME_RE`: a name that looks like a
credential does not travel, whatever it is prefixed with. That is what keeps the namespace rule
from becoming a hole.
"""

from __future__ import annotations

import os
import re
from typing import Dict, Iterable, List, Mapping, Optional

# The allow-list. See the module docstring for the reasoning behind each entry.
WHITELIST = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "ALETH_WORKSPACE_DIR",
    # The container client's own configuration directory and endpoint. Neither is a credential
    # -- the credential helper reads the token from the OS store -- and stripping either only
    # breaks authenticated pulls or a deployment that points the client at a socket.
    "HOME",
    "DOCKER_CONFIG",
    "DOCKER_HOST",
)

# Required for a child to start on Windows. They carry no credential.
PLATFORM_REQUIRED_WINDOWS = ("SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC", "TEMP", "TMP")

# What a credential *looks like*, for the audit helper. Deliberately broad: it is used to prove
# nothing sensitive survived, never to decide what to allow.
SECRET_NAME_RE = re.compile(
    r"(API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|PRIVATE[_-]?KEY"
    r"|AWS_|GITHUB|_KEY$|_TOKEN$)",
    re.IGNORECASE,
)


# Configuration *namespaces* that travel wholesale: this app's own settings, and the in-repo
# runtime double's knobs (see the module docstring). Credential-shaped names are still dropped.
CONFIG_NAMESPACES = ("ALETH_", "FAKE_DOCKER_")


def allowed_names() -> tuple:
    """The names this platform permits, in a stable order."""
    if os.name == "nt":
        return WHITELIST + PLATFORM_REQUIRED_WINDOWS
    return WHITELIST


def sanitized_environment(
    *,
    base: Optional[Mapping[str, str]] = None,
    extra: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """The environment for a child: the allow-list from ``base``, plus deliberate ``extra``.

    ``base`` defaults to ``os.environ`` (the *source* is the host; only the allow-list crosses).
    ``extra`` is how a caller adds something it must set on purpose -- the MCP servers need
    ``PYTHONPATH`` so they can import the ``tools`` package, and that is a decision this app
    makes rather than something the shell happened to export.

    An ``extra`` name is passed through even if it looks like a credential: the caller named it,
    so it is intentional. That is the difference between an allow-list and a filter.
    """
    source = os.environ if base is None else base
    env = {
        name: value
        for name, value in source.items()
        if name in allowed_names()
        or (name.startswith(CONFIG_NAMESPACES) and not SECRET_NAME_RE.search(name))
    }
    for name, value in (extra or {}).items():
        env[str(name)] = str(value)
    return env


def stripped_names(base: Optional[Mapping[str, str]] = None) -> List[str]:
    """The names the allow-list drops, **by name only** -- never a value.

    This is the auditable half of the boundary: a caller may log *that* ``OPENAI_API_KEY`` was
    withheld, and must not log what it held.
    """
    source = os.environ if base is None else base
    permitted = set(allowed_names())
    return sorted(
        name
        for name in source
        if name not in permitted
        and not (name.startswith(CONFIG_NAMESPACES) and not SECRET_NAME_RE.search(name))
    )


def leaked_secret_names(env: Mapping[str, str]) -> List[str]:
    """Names in ``env`` that look like credentials. Should always be empty.

    The inverse check, for tests and for an operator asking whether a boundary is intact.
    """
    return sorted(name for name in env if SECRET_NAME_RE.search(name))


def describe(base: Optional[Mapping[str, str]] = None) -> str:
    """A one-line, value-free summary of what sanitization does here."""
    dropped = stripped_names(base)
    withheld = [name for name in dropped if SECRET_NAME_RE.search(name)]
    return (
        f"child environment: {len(allowed_names())} names allowed; "
        f"{len(dropped)} dropped, {len(withheld)} of them credential-shaped"
    )


def ensure_sanitized(env: Mapping[str, str], *, allow: Iterable[str] = ()) -> None:
    """Raise when ``env`` carries a credential-shaped name that was not explicitly allowed.

    The fail-loud half: a call site that builds a child environment by hand and forgets this
    module is caught at the boundary rather than discovered in an incident.
    """
    permitted = set(allow)
    offenders = [name for name in leaked_secret_names(env) if name not in permitted]
    if offenders:
        raise ValueError(
            "refusing to hand a child process credential-shaped environment names: "
            + ", ".join(offenders)
        )
