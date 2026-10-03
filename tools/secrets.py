"""Phase 40/41: credentials live in the OS keyring, and are *resolved*, never exported.

A `.env` file is a plaintext credential store in every backup, every screen share and one
`git add -f` away from a repository. The replacement is the platform's own store -- macOS Keychain,
Windows Credential Locker, the Secret Service API -- reached through ``keyring``.

**Phase 41 removed the environment mutation.** Phase 40 loaded the keyring into ``os.environ`` at
boot, which was wrong twice over: ``os.environ`` is global mutable state shared by every thread, so
two concurrent runs with different providers would overwrite each other's credentials; and a
third-party library that dumps the environment on a crash would write a live API key into the
operational log. There is now no export step at all. :func:`credential` **resolves** a value --
explicit override, then the keyring, then the environment as a last resort -- and the environment is
only ever *read*, never written.

The store is bound to a :class:`contextvars.ContextVar`, so a caller that needs a different
credential for one run binds its own store for the duration of that run instead of mutating
process-wide state.

``keyring`` is a **hard dependency** (Phase 41): optional security is no security, because a
checkout that boots without it is a checkout whose users fall back to plaintext. The import is
module-level on purpose -- if it cannot load, the engine must not start.
"""

from __future__ import annotations

import contextvars
import os
from typing import Any, Dict, Iterable, List, Optional

import keyring

# The service name every credential is filed under, so a person can find them in their own
# keychain UI rather than wondering what wrote them.
SERVICE = "aleth"

# provider -> the environment name the rest of the engine speaks in. This mapping is the seam: a
# value lives in the keyring, and a caller asks for it by the name it already knows.
PROVIDERS: Dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "github": "GITHUB_TOKEN",
}

_BY_ENV: Dict[str, str] = {env: name for name, env in PROVIDERS.items()}


class SecretError(RuntimeError):
    """A credential could not be stored. Never raised for a *read*."""


class SecretStore:
    """A credential resolver: explicit overrides, then the keyring, then the environment.

    ``overrides`` is how a caller injects a credential explicitly -- a run that must use a
    particular key passes one here rather than setting a process-wide variable. The environment is
    the **last** resort and is never written: CI exports a placeholder, and an operator may
    legitimately export a key for one shell.
    """

    def __init__(self, overrides: Optional[Dict[str, str]] = None, *, backend: Any = None):
        self._overrides = {
            str(name).strip().lower(): str(value)
            for name, value in (overrides or {}).items()
            if str(value or "").strip()
        }
        self._backend = backend

    def provider_for(self, env_name: str) -> str:
        """The provider an environment name belongs to, or ``""``."""
        return _BY_ENV.get(str(env_name or "").strip(), "")

    def get(self, provider: str) -> str:
        """One provider's credential, or ``""``. Never raises -- a read is not a place to fail."""
        name = str(provider or "").strip().lower()
        if name not in PROVIDERS:
            return ""
        override = self._overrides.get(name)
        if override:
            return override
        stored = get_secret(name, backend=self._backend)
        if stored:
            return stored
        return (os.environ.get(PROVIDERS[name]) or "").strip()

    def env(self, env_name: str) -> str:
        """The credential for an environment name, resolved. ``""`` when there is none."""
        name = self.provider_for(env_name)
        if not name:
            # Not one of ours (``OPENAI_BASE_URL``, a model name): the environment is the only
            # source, and reading it is not the sin -- writing it was.
            return (os.environ.get(str(env_name or "")) or "").strip()
        return self.get(name)

    def providers(self) -> List[str]:
        """Which providers this store can resolve, **by name only**. A value never leaves here."""
        return [name for name in sorted(PROVIDERS) if self.get(name)]

    def with_overrides(self, **overrides: str) -> "SecretStore":
        """A store that resolves ``overrides`` first. The injection point for one run."""
        merged = dict(self._overrides)
        merged.update({name: value for name, value in overrides.items() if str(value or "").strip()})
        return SecretStore(merged, backend=self._backend)


_DEFAULT = SecretStore()
_ACTIVE: contextvars.ContextVar = contextvars.ContextVar("aleth_secret_store", default=None)


def active() -> SecretStore:
    """The store bound to this context, or the process default.

    A :class:`contextvars.ContextVar` rather than a module global: two concurrent runs needing
    different credentials bind their own stores instead of overwriting each other's.
    """
    return _ACTIVE.get() or _DEFAULT


def use(store: SecretStore):
    """Bind ``store`` to this context for its duration. Returns the reset token.

    ``token = use(store)`` ... ``_ACTIVE.reset(token)`` -- the caller owns the scope, so a run's
    credential never outlives the run.
    """
    return _ACTIVE.set(store)


def credential(env_name: str) -> str:
    """The single read path: the active store's value for an environment name.

    This is what the client factories call instead of ``os.environ.get(...)``, so a credential held
    only in the keyring reaches the model client without ever being exported.
    """
    return active().env(env_name)


def available() -> bool:
    """Whether the keyring backend can be used. ``keyring`` itself is a hard dependency."""
    try:
        keyring.get_keyring()
        return True
    except Exception:
        return False


def provider_env(provider: str) -> str:
    """The environment name a provider's credential is spoken of as. ``""`` when unknown."""
    return PROVIDERS.get(str(provider or "").strip().lower(), "")


def set_secret(provider: str, value: str, *, backend: Any = None) -> str:
    """Store one provider's credential in the keyring. Returns the provider name.

    Raises :class:`SecretError` -- the CLI turns that into a fix-it message, and a silent failure
    would leave the user believing a credential was stored.
    """
    name = str(provider or "").strip().lower()
    if name not in PROVIDERS:
        raise SecretError(
            f"unknown provider {provider!r}; expected one of {', '.join(sorted(PROVIDERS))}"
        )
    secret = str(value or "").strip()
    if not secret:
        raise SecretError("refusing to store an empty credential")
    try:
        (backend or keyring).set_password(SERVICE, name, secret)
    except Exception as error:
        raise SecretError(
            f"the keyring refused the credential: {type(error).__name__}: {error}"
        ) from error
    return name


def get_secret(provider: str, *, backend: Any = None) -> str:
    """One provider's credential straight from the keyring, or ``""``. Never raises."""
    name = str(provider or "").strip().lower()
    if name not in PROVIDERS:
        return ""
    try:
        return str((backend or keyring).get_password(SERVICE, name) or "")
    except Exception:
        return ""


def clear_secret(provider: str, *, backend: Any = None) -> bool:
    """Forget one provider's credential. ``True`` when one was removed."""
    name = str(provider or "").strip().lower()
    if name not in PROVIDERS:
        return False
    try:
        (backend or keyring).delete_password(SERVICE, name)
        return True
    except Exception:
        return False


def stored_providers(*, backend: Any = None) -> List[str]:
    """Which providers hold a credential, **by name only**. A value never leaves the store."""
    return [name for name in sorted(PROVIDERS) if get_secret(name, backend=backend)]


__all__ = [
    "PROVIDERS",
    "SERVICE",
    "SecretError",
    "SecretStore",
    "active",
    "available",
    "clear_secret",
    "credential",
    "get_secret",
    "provider_env",
    "set_secret",
    "stored_providers",
    "use",
]
