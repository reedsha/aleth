"""Phase 40: credentials live in the OS keyring, never on disk.

A `.env` file is a plaintext credential store sitting in a developer's home directory -- in every
backup, every screen share, and one `git add -f` away from a repository. This module is the
replacement: the secret goes into the platform's own store (macOS Keychain, Windows Credential
Locker, the Secret Service API on Linux) through ``keyring``, and the engine reads it from there.

Two rules make that a boundary rather than a suggestion:

* **The keyring is the store; the environment is a hand-off.** The engine already reads
  ``OPENAI_API_KEY`` and friends, so :func:`install_into_environment` loads what the keyring holds
  into the process environment at boot. That is memory, and nothing here writes a file.
* **A value never leaves the store except into memory.** :func:`stored_providers` reports *names*,
  so a list command cannot leak a credential into a terminal, a log or a transcript.

``keyring`` is imported lazily and every entry point takes an injectable ``store``: a checkout
without the library still boots, and a test never needs a real platform backend.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

# The service name every credential is filed under, so a person can find them in their own
# keychain UI rather than wondering what wrote them.
SERVICE = "aleth"

# provider -> the environment name the rest of the engine already reads. This mapping *is* the
# seam: the keyring is where a value lives, the environment is how it reaches the model client.
PROVIDERS: Dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "github": "GITHUB_TOKEN",
}


class SecretError(RuntimeError):
    """A credential could not be stored. Never raised for a *read*."""


def _store(store: Any = None) -> Optional[Any]:
    """The keyring backend: the caller's, or the OS one, or ``None`` when neither is available."""
    if store is not None:
        return store
    try:
        import keyring
    except Exception:  # not installed: a checkout still boots, and the CLI says so
        return None
    return keyring


def available(store: Any = None) -> bool:
    """Whether a keyring backend can be used at all."""
    return _store(store) is not None


def provider_env(provider: str) -> str:
    """The environment name a provider's credential is handed to. ``""`` when unknown."""
    return PROVIDERS.get(str(provider or "").strip().lower(), "")


def set_secret(provider: str, value: str, *, store: Any = None) -> str:
    """Store one provider's credential in the keyring. Returns the provider name.

    Raises :class:`SecretError` -- the CLI's job is to turn that into a fix-it message, and a
    silent failure would leave the user believing a credential was stored.
    """
    name = str(provider or "").strip().lower()
    if name not in PROVIDERS:
        raise SecretError(
            f"unknown provider {provider!r}; expected one of {', '.join(sorted(PROVIDERS))}"
        )
    secret = str(value or "").strip()
    if not secret:
        raise SecretError("refusing to store an empty credential")
    backend = _store(store)
    if backend is None:
        raise SecretError(
            "no OS keyring backend is available; install `keyring` and a platform backend"
        )
    try:
        backend.set_password(SERVICE, name, secret)
    except Exception as error:
        raise SecretError(
            f"the keyring refused the credential: {type(error).__name__}: {error}"
        ) from error
    return name


def get_secret(provider: str, *, store: Any = None) -> str:
    """One provider's credential, or ``""``. Never raises -- a read is not a place to fail."""
    name = str(provider or "").strip().lower()
    backend = _store(store)
    if backend is None or name not in PROVIDERS:
        return ""
    try:
        return str(backend.get_password(SERVICE, name) or "")
    except Exception:
        return ""


def clear_secret(provider: str, *, store: Any = None) -> bool:
    """Forget one provider's credential. ``True`` when one was removed."""
    name = str(provider or "").strip().lower()
    backend = _store(store)
    if backend is None or name not in PROVIDERS:
        return False
    try:
        backend.delete_password(SERVICE, name)
        return True
    except Exception:
        return False


def stored_providers(*, store: Any = None) -> List[str]:
    """Which providers hold a credential, **by name only**. A value never leaves the keyring."""
    return [name for name in sorted(PROVIDERS) if get_secret(name, store=store)]


def install_into_environment(*, store: Any = None) -> List[str]:
    """Load every stored credential into this process's environment. Returns the names loaded.

    The one seam between the keyring and everything downstream (``env_boot``, ``system2``, the model
    router all read the environment). It is **memory only** -- nothing here opens a file for writing.

    An already-exported name wins: the operator set it on purpose, and silently replacing it would
    make their deliberate choice impossible to debug.
    """
    loaded: List[str] = []
    for name, env_name in sorted(PROVIDERS.items()):
        if (os.environ.get(env_name) or "").strip():
            continue
        secret = get_secret(name, store=store)
        if secret:
            os.environ[env_name] = secret
            loaded.append(name)
    return loaded


__all__ = [
    "PROVIDERS",
    "SERVICE",
    "SecretError",
    "available",
    "clear_secret",
    "get_secret",
    "install_into_environment",
    "provider_env",
    "set_secret",
    "stored_providers",
]
