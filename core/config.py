"""The deterministic bootloader: what models exist, and whether they answer.

The router cannot route to a model the kernel does not know about. Before this module, "which
model runs this" was an environment variable with a literal default and a hope; nothing checked
that the endpoint existed, that the key was accepted, or that a task demanding the strongest tier
had anything stronger than a local toy to run on. This is the configuration that answers those
questions *before* the store opens, so the kernel either knows its fleet or refuses to boot.

Three pieces:

* :class:`BootloaderConfig` -- a strict schema for the fleet. Three tiers, weakest first. A tier
  is ``provider`` + ``model_name``, plus the credentials that reach it. ``tier_0`` may have no key
  (a local endpoint does not need one); ``tier_1`` and ``tier_2`` must, because a tier you cannot
  authenticate to is not configured, and pretending otherwise would let the router select a model
  that cannot answer.
* :func:`boot` -- load, validate, then **prove**: one request per configured endpoint. A timeout,
  a 401 or a 404 is fatal, and the caller exits. A system that boots with broken tools fails later,
  in the middle of a run, with a worse error and half a plan on disk.
* :func:`active_config` -- the fleet the router consults. When no file is supplied the
  configuration is derived from the environment the rest of the app already reads
  (``agents.model_routing``), so the bootloader is the *single* source the router asks rather than
  a second, competing one.

Import-light on purpose: ``json``, ``os``, ``urllib`` and pydantic. ``yaml`` is imported only when
a ``.yaml`` config is actually loaded.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

# The tiers, weakest first. These names are the contract the config file uses, and the order is
# the order of capability: the router may fall *down* this list, never silently up it.
TIER_SLOTS: Tuple[str, ...] = ("tier_0", "tier_1", "tier_2")

# The slots that must carry a key. ``tier_0`` is exempt on purpose: the cheapest tier is the one
# most likely to be a local endpoint, and demanding a key there would make a local model
# unconfigurable.
SLOTS_REQUIRING_KEY: Tuple[str, ...] = ("tier_1", "tier_2")

# Where the bootloader looks when nothing says otherwise: beside the entrypoints.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_CONFIG = "ALETH_CONFIG"
DEFAULT_CONFIG_PATH = os.path.join(REPO_ROOT, "aleth.config.json")
EXAMPLE_CONFIG_PATH = os.path.join(REPO_ROOT, "aleth.config.example.json")

# The ping budget. Short: this runs on the boot path, and an endpoint that cannot answer a model
# list in a few seconds is an endpoint that will not answer a completion either.
DEFAULT_PING_TIMEOUT = 10.0


class ConfigError(RuntimeError):
    """The configuration is missing, malformed, or incomplete. The kernel must not boot."""


class BootError(RuntimeError):
    """A configured endpoint did not answer. The kernel must not boot."""


class EndpointUnreachable(RuntimeError):
    """One endpoint failed its probe: a timeout, a 401, a 404, or a transport error."""


class TierConfig(BaseModel):
    """One model the kernel may route to, with the credentials that reach it."""

    model_config = ConfigDict(extra="forbid", strict=False)

    provider: str
    model_name: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None

    @model_validator(mode="after")
    def _check(self) -> "TierConfig":
        if not str(self.provider or "").strip():
            raise ValueError("provider must be a non-empty name")
        if not str(self.model_name or "").strip():
            raise ValueError("model_name must be a non-empty name")
        return self

    def route(self) -> str:
        """The ``provider:model`` string the System 2 client expects."""
        return f"{str(self.provider).strip()}:{str(self.model_name).strip()}"


class BootloaderConfig(BaseModel):
    """The fleet: up to three tiers, weakest first. Strict, so a typo is an error, not a default."""

    model_config = ConfigDict(extra="forbid", strict=False)

    tier_0: Optional[TierConfig] = None
    tier_1: Optional[TierConfig] = None
    tier_2: Optional[TierConfig] = None

    @model_validator(mode="after")
    def _check(self) -> "BootloaderConfig":
        configured = self.configured_slots()
        if not configured:
            raise ValueError("at least one tier must be configured")
        for slot in SLOTS_REQUIRING_KEY:
            tier = getattr(self, slot)
            if tier is not None and not str(tier.api_key or "").strip():
                raise ValueError(f"{slot} must declare an api_key")
        return self

    def configured_slots(self) -> List[int]:
        """The indices of the tiers that are configured, weakest first."""
        return [index for index, slot in enumerate(TIER_SLOTS) if getattr(self, slot) is not None]

    def tier(self, index: int) -> TierConfig:
        """The tier at ``index``. Only call it for an index :meth:`configured_slots` returned."""
        tier = getattr(self, TIER_SLOTS[index])
        if tier is None:  # pragma: no cover - guarded by the caller
            raise ConfigError(f"{TIER_SLOTS[index]} is not configured")
        return tier


def _split_route(route: str) -> Tuple[str, str]:
    """``"openai:policy/x"`` -> ``("openai", "policy/x")``; a bare name gets the default provider."""
    provider, separator, model = str(route or "").partition(":")
    if not separator:
        return "openai", provider
    return provider, model


def default_config() -> BootloaderConfig:
    """The fleet the kernel runs on when no file is supplied.

    Derived from the environment the rest of the app already reads (``agents.model_routing``), so
    the bootloader is the one place the router asks rather than a second, competing source. Tiers
    whose credentials are absent are **omitted**: a tier you cannot authenticate to is not
    configured, and claiming otherwise would let the router select a model that cannot answer.
    """
    from agents.model_routing import architect_model, coder_model
    from tools import secrets

    # Resolved through the credential store (Phase 41), not read from the environment: a key held
    # only in the OS keyring must reach the fleet, and nothing exports it to ``os.environ`` any more.
    key = secrets.credential("OPENAI_API_KEY")
    base_url = (os.environ.get("OPENAI_BASE_URL") or "").strip() or None

    def tier(route: str, *, needs_key: bool) -> Optional[TierConfig]:
        if needs_key and not key:
            return None
        provider, model = _split_route(route)
        return TierConfig(
            provider=provider, model_name=model,
            api_key=key or None, base_url=base_url,
        )

    return BootloaderConfig(
        tier_0=tier(coder_model("coder-standard"), needs_key=False),
        tier_1=tier(coder_model("coder-deep"), needs_key=True),
        tier_2=tier(architect_model(), needs_key=True),
    )


_ACTIVE: Dict[str, Optional[BootloaderConfig]] = {"value": None}


def active_config() -> BootloaderConfig:
    """The fleet in force. Loaded once; the environment-derived default until something sets it."""
    if _ACTIVE["value"] is None:
        _ACTIVE["value"] = default_config()
    return _ACTIVE["value"]


def set_active_config(config: BootloaderConfig) -> BootloaderConfig:
    """Install the fleet the router will consult. Called by the bootloader, once."""
    _ACTIVE["value"] = config
    return config


def reset_config() -> None:
    """Forget the installed fleet (a test that swaps the configuration in)."""
    _ACTIVE["value"] = None


def _read_config_file(path: str) -> Dict[str, Any]:
    """Parse a JSON or YAML config file. A YAML file needs PyYAML, which the manifest declares."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        raise ConfigError(f"could not read {path}: {error}") from error

    if path.lower().endswith((".yaml", ".yml")):
        try:
            import yaml
        except Exception as error:  # pragma: no cover - the manifest declares PyYAML
            raise ConfigError(f"{path} is YAML but PyYAML is not installed: {error}") from error
        try:
            data = yaml.safe_load(text)
        except Exception as error:
            raise ConfigError(f"{path} is not valid YAML: {error}") from error
    else:
        try:
            data = json.loads(text)
        except ValueError as error:
            raise ConfigError(f"{path} is not valid JSON: {error}") from error

    if not isinstance(data, dict):
        raise ConfigError(f"{path} must hold an object with tier_0/tier_1/tier_2 keys")
    return data


def load_config(path: Optional[str] = None) -> BootloaderConfig:
    """Load and validate a config file. Raises :class:`ConfigError` for anything unusable.

    The path is ``path``, then ``ALETH_CONFIG``, then ``aleth.config.json`` beside the
    entrypoints. A missing file is an error here rather than a silent default: the caller asked
    for a *file*, and answering with something else is how a typo becomes a production fleet.
    """
    target = path or (os.environ.get(ENV_CONFIG) or "").strip() or DEFAULT_CONFIG_PATH
    if not os.path.isfile(target):
        raise ConfigError(
            f"no bootloader config at {target}. Copy {os.path.basename(EXAMPLE_CONFIG_PATH)} "
            f"or set {ENV_CONFIG}."
        )
    data = _read_config_file(target)
    try:
        return BootloaderConfig.model_validate(data)
    except ValidationError as error:
        raise ConfigError(f"{target} is not a valid bootloader config: {error}") from error


def probe_endpoint(tier: TierConfig, *, timeout: float = DEFAULT_PING_TIMEOUT) -> None:
    """One request against an endpoint, to prove the key and the URL work.

    ``GET {base_url}/models`` rather than a completion: it costs no tokens, it exercises the same
    credential and transport, and it is the check OpenAI-compatible providers expose. Any failure
    -- no ``base_url``, a timeout, a 401, a 404, a transport error -- raises
    :class:`EndpointUnreachable`.
    """
    base_url = str(tier.base_url or "").strip()
    if not base_url:
        raise EndpointUnreachable(f"{tier.route()} has no base_url to probe")
    request = urllib.request.Request(base_url.rstrip("/") + "/models")
    key = str(tier.api_key or "").strip()
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 0) or 0)
            if status >= 400:
                raise EndpointUnreachable(f"{tier.route()} answered HTTP {status}")
    except urllib.error.HTTPError as error:
        raise EndpointUnreachable(f"{tier.route()} answered HTTP {error.code}") from error
    except EndpointUnreachable:
        raise
    except Exception as error:
        raise EndpointUnreachable(f"{tier.route()}: {type(error).__name__}: {error}") from error


def validate_endpoints(
    config: BootloaderConfig,
    *,
    probe: Optional[Callable[..., None]] = None,
    timeout: float = DEFAULT_PING_TIMEOUT,
) -> None:
    """Prove every configured endpoint. Raises :class:`BootError` listing all that failed.

    Every tier is probed, and *all* failures are reported together: fixing one broken key only to
    discover the next on the following boot is a worse experience than being told everything at
    once. ``probe`` is injectable so the check can be exercised without a network.
    """
    prober = probe or probe_endpoint
    failures: List[str] = []
    for index in config.configured_slots():
        slot = TIER_SLOTS[index]
        try:
            prober(config.tier(index), timeout=timeout)
        except Exception as error:
            failures.append(f"{slot}: {error}")
    if failures:
        raise BootError("; ".join(failures))


def boot(
    path: Optional[str] = None,
    *,
    probe: Optional[Callable[..., None]] = None,
    timeout: float = DEFAULT_PING_TIMEOUT,
) -> BootloaderConfig:
    """Load, validate and prove the fleet, then install it as the active configuration.

    A config *file* is used when one is named or present; otherwise the environment-derived
    default is, because a checkout with working credentials and no file is a legitimate state and
    the fleet it describes is still proven before anything else runs.
    """
    target = path or (os.environ.get(ENV_CONFIG) or "").strip() or DEFAULT_CONFIG_PATH
    config = load_config(target) if os.path.isfile(target) else default_config()
    validate_endpoints(config, probe=probe, timeout=timeout)
    return set_active_config(config)


def boot_or_exit(
    path: Optional[str] = None,
    *,
    probe: Optional[Callable[..., None]] = None,
    timeout: float = DEFAULT_PING_TIMEOUT,
) -> Optional[BootloaderConfig]:
    """The entrypoint's boot step: on any failure, report it and ``sys.exit(1)``.

    Returns ``None`` when it exits, so a caller can ``return boot_or_exit()`` and never continue
    on a fleet that did not answer.
    """
    try:
        config = boot(path, probe=probe, timeout=timeout)
    except (ConfigError, BootError) as error:
        print(f"[Bootloader] refusing to boot: {error}", file=sys.stderr)
        sys.exit(1)
    slots = ", ".join(
        f"{TIER_SLOTS[index]}={config.tier(index).route()}" for index in config.configured_slots()
    )
    print(f"[Bootloader] fleet verified: {slots}")
    return config
