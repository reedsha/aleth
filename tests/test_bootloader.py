"""Phase 6.5: the deterministic bootloader and the router's fallback matrix.

    python -m pytest tests/test_bootloader.py -n0 -q

Two things are proven here. First, the configuration is *strict*: a typo is an error, a tier
without a key is an error, and an endpoint that does not answer stops the boot. Second, the router
never silently under-powers a task -- it falls back one way only, says so at CRITICAL, and refuses
outright when the fleet's strongest tier is the weakest one configured.
"""

from __future__ import annotations

import json
import urllib.error

import pytest
from pydantic import ValidationError

from core import config as boot_config
from orchestration import model_router
from orchestration.model_router import BOOT_SLOTS, TaskUnroutable, resolve_endpoint, route


@pytest.fixture(autouse=True)
def _clean_fleet():
    """No test inherits another's fleet, and none leaks one out."""
    boot_config.reset_config()
    yield
    boot_config.reset_config()


def _tier(model: str = "policy/x", *, key: str | None = "k", base: str | None = "https://example.invalid/v1"):
    return boot_config.TierConfig(
        provider="openai", model_name=model, api_key=key, base_url=base
    )


def _fleet(**tiers):
    return boot_config.BootloaderConfig(**tiers)


class TestTheSchema:
    def test_a_tier_needs_a_provider_and_a_model(self):
        with pytest.raises(ValidationError):
            boot_config.TierConfig(provider="openai")
        with pytest.raises(ValidationError):
            boot_config.TierConfig(provider="openai", model_name="   ")

    def test_an_unknown_key_is_refused_rather_than_ignored(self):
        with pytest.raises(ValidationError):
            boot_config.BootloaderConfig(tier_0=_tier(), tier_9=_tier())

    def test_a_fleet_with_no_tiers_is_refused(self):
        with pytest.raises(ValidationError):
            boot_config.BootloaderConfig()

    def test_tier_zero_may_have_no_key(self):
        """The weakest tier is the one most likely to be a local endpoint."""
        fleet = boot_config.BootloaderConfig(tier_0=_tier(key=None))
        assert fleet.configured_slots() == [0]

    @pytest.mark.parametrize("slot", ["tier_1", "tier_2"])
    def test_a_stronger_tier_must_declare_a_key(self, slot):
        with pytest.raises(ValidationError, match="api_key"):
            boot_config.BootloaderConfig(tier_0=_tier(key=None), **{slot: _tier(key=None)})

    def test_the_route_is_provider_colon_model(self):
        assert _tier("policy/coder-deep-test").route() == "openai:policy/coder-deep-test"


class TestLoadingTheFile:
    def test_a_json_file_round_trips(self, tmp_path):
        path = tmp_path / "fleet.json"
        path.write_text(json.dumps({
            "tier_0": {"provider": "openai", "model_name": "small", "base_url": "https://a/v1"},
            "tier_2": {"provider": "openai", "model_name": "big", "api_key": "k"},
        }), encoding="utf-8")

        fleet = boot_config.load_config(str(path))

        assert fleet.configured_slots() == [0, 2]
        assert fleet.tier(2).route() == "openai:big"

    def test_a_missing_file_is_an_error_not_a_default(self, tmp_path):
        with pytest.raises(boot_config.ConfigError, match="no bootloader config"):
            boot_config.load_config(str(tmp_path / "absent.json"))

    def test_malformed_json_is_refused(self, tmp_path):
        path = tmp_path / "fleet.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(boot_config.ConfigError, match="not valid JSON"):
            boot_config.load_config(str(path))

    def test_a_file_missing_a_key_is_refused(self, tmp_path):
        path = tmp_path / "fleet.json"
        path.write_text(json.dumps({
            "tier_2": {"provider": "openai", "model_name": "big"},
        }), encoding="utf-8")
        with pytest.raises(boot_config.ConfigError, match="not a valid bootloader config"):
            boot_config.load_config(str(path))

    def test_a_yaml_file_is_read(self, tmp_path):
        path = tmp_path / "fleet.yaml"
        path.write_text(
            "tier_0:\n  provider: openai\n  model_name: small\n  base_url: https://a/v1\n",
            encoding="utf-8",
        )
        assert boot_config.load_config(str(path)).configured_slots() == [0]


class TestTheDefaultFleet:
    def test_it_mirrors_the_environment_routes(self, monkeypatch):
        """No file: the fleet is the routes the app already reads, so the router has one source."""
        for name in (
            "DEEPAGENTS_ARCHITECT_MODEL",
            "DEEPAGENTS_CODER_DEEP_MODEL",
            "DEEPAGENTS_CODER_STANDARD_MODEL",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")

        fleet = boot_config.default_config()

        assert fleet.configured_slots() == [0, 1, 2]
        # Exactly the routes the policy ladder names, so the two never disagree.
        assert fleet.tier(0).route() == model_router.TIERS[0]["route"]
        assert fleet.tier(2).route() == model_router.TIERS[model_router.TOP_TIER]["route"]

    def test_a_tier_without_credentials_is_omitted(self, monkeypatch):
        """A tier you cannot authenticate to is not configured, not silently weaker."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert boot_config.default_config().configured_slots() == [0]


class TestProvingTheEndpoints:
    def test_a_failing_probe_stops_the_boot_and_reports_every_failure(self):
        fleet = _fleet(tier_0=_tier(key=None), tier_1=_tier(), tier_2=_tier())

        def probe(_tier_config, **_kwargs):
            raise boot_config.EndpointUnreachable("HTTP 401")

        with pytest.raises(boot_config.BootError) as error:
            boot_config.validate_endpoints(fleet, probe=probe)

        # Both tiers are named, not just the first: one boot tells you everything that is wrong.
        assert "tier_0: " in str(error.value)
        assert "tier_1: " in str(error.value)
        assert "tier_2: " in str(error.value)

    def test_a_probe_that_answers_boots(self):
        boot_config.validate_endpoints(_fleet(tier_0=_tier()), probe=lambda *_a, **_k: None)

    def test_an_unauthorized_endpoint_is_a_failure(self, monkeypatch):
        def deny(*_args, **_kwargs):
            raise urllib.error.HTTPError("https://a/v1/models", 401, "Unauthorized", None, None)

        monkeypatch.setattr(boot_config.urllib.request, "urlopen", deny)
        with pytest.raises(boot_config.EndpointUnreachable, match="401"):
            boot_config.probe_endpoint(_tier())

    def test_an_endpoint_with_no_base_url_cannot_be_probed(self):
        with pytest.raises(boot_config.EndpointUnreachable, match="no base_url"):
            boot_config.probe_endpoint(_tier(base=None))

    def test_a_timeout_is_a_failure(self, monkeypatch):
        def stall(*_args, **_kwargs):
            raise TimeoutError("timed out")

        monkeypatch.setattr(boot_config.urllib.request, "urlopen", stall)
        with pytest.raises(boot_config.EndpointUnreachable, match="TimeoutError"):
            boot_config.probe_endpoint(_tier())


class TestTheBootStep:
    def test_it_exits_when_an_endpoint_is_broken(self, tmp_path, monkeypatch):
        monkeypatch.setenv(boot_config.ENV_CONFIG, str(tmp_path / "fleet.json"))
        (tmp_path / "fleet.json").write_text(
            json.dumps({"tier_0": {"provider": "openai", "model_name": "m",
                                   "base_url": "https://a/v1"}}),
            encoding="utf-8",
        )

        def probe(*_args, **_kwargs):
            raise boot_config.EndpointUnreachable("HTTP 404")

        with pytest.raises(SystemExit) as exit_code:
            boot_config.boot_or_exit(probe=probe)
        assert exit_code.value.code == 1

    def test_it_exits_when_there_is_no_config_and_no_credentials(self, tmp_path, monkeypatch):
        monkeypatch.setenv(boot_config.ENV_CONFIG, str(tmp_path / "absent.json"))
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        def probe(*_args, **_kwargs):
            raise boot_config.EndpointUnreachable("no base_url to probe")

        with pytest.raises(SystemExit) as exit_code:
            boot_config.boot_or_exit(probe=probe)
        assert exit_code.value.code == 1

    def test_a_proven_fleet_is_installed_as_the_active_one(self, tmp_path, monkeypatch):
        monkeypatch.setenv(boot_config.ENV_CONFIG, str(tmp_path / "fleet.json"))
        (tmp_path / "fleet.json").write_text(
            json.dumps({"tier_0": {"provider": "openai", "model_name": "m",
                                   "base_url": "https://a/v1"}}),
            encoding="utf-8",
        )

        config = boot_config.boot_or_exit(probe=lambda *_a, **_k: None)

        assert config is boot_config.active_config()
        assert config.tier(0).route() == "openai:m"


class TestTheFallbackMatrix:
    def test_the_required_tier_is_used_when_it_exists(self):
        fleet = _fleet(tier_0=_tier("small"), tier_1=_tier("mid"), tier_2=_tier("big"))
        endpoint = resolve_endpoint(route(5, 0), fleet)

        assert endpoint["route"] == "openai:big"
        assert endpoint["boot_tier"] == "tier_2"
        assert endpoint["fell_back"] is False

    def test_the_result_carries_the_tiers_endpoint_not_only_its_route(self):
        """The transport needs the credential too, so the router must hand it over."""
        fleet = _fleet(tier_2=boot_config.TierConfig(
            provider="openai", model_name="big",
            base_url="https://tier-two.invalid/v1", api_key="sk-tier-two",
        ))
        endpoint = resolve_endpoint(route(5, 0), fleet)

        assert endpoint["base_url"] == "https://tier-two.invalid/v1"
        assert endpoint["api_key"] == "sk-tier-two"
        assert endpoint["provider"] == "openai"

    def test_a_named_tier_falls_back_without_refusing(self):
        """The Architect has no alternative path; only node dispatch refuses."""
        fleet = _fleet(tier_0=_tier("small"))
        endpoint = model_router.endpoint_for_tier("architect", fleet)

        assert endpoint["route"] == "openai:small"
        assert endpoint["fell_back"] is True

    def test_a_named_tier_that_does_not_exist_is_refused(self):
        with pytest.raises(model_router.RoutingError, match="unknown tier"):
            model_router.endpoint_for_tier("tier-99", _fleet(tier_0=_tier()))

    def test_a_missing_tier_falls_back_one_step_and_says_so(self, caplog):
        fleet = _fleet(tier_0=_tier("small"), tier_1=_tier("mid"))
        with caplog.at_level("CRITICAL", logger="orchestration.model_router"):
            endpoint = resolve_endpoint(route(5, 0), fleet)

        assert endpoint["route"] == "openai:mid"
        assert endpoint["boot_tier"] == "tier_1"
        assert endpoint["fell_back"] is True
        assert caplog.records and "tier_2 is not configured" in caplog.text

    def test_only_the_weakest_configured_tier_refuses_a_top_tier_task(self):
        """Two rungs down is not a fallback: a 7B local model must not be handed system-wide work."""
        fleet = _fleet(tier_0=_tier("small"))
        with pytest.raises(TaskUnroutable, match="only tier_0 is configured"):
            resolve_endpoint(route(5, 0), fleet)

    def test_a_cheap_task_is_never_sent_to_a_stronger_tier(self):
        fleet = _fleet(tier_0=_tier("small"), tier_2=_tier("big"))
        endpoint = resolve_endpoint(route(1, 0), fleet)

        assert endpoint["route"] == "openai:small"
        assert endpoint["fell_back"] is False

    def test_a_fleet_with_nothing_that_cheap_over_provisions_rather_than_fails(self, caplog):
        fleet = _fleet(tier_2=_tier("big"))
        with caplog.at_level("CRITICAL", logger="orchestration.model_router"):
            endpoint = resolve_endpoint(route(1, 0), fleet)

        assert endpoint["route"] == "openai:big"
        assert endpoint["fell_back"] is True

    def test_the_matrix_reads_the_active_fleet_by_default(self):
        boot_config.set_active_config(_fleet(tier_0=_tier("active")))
        assert resolve_endpoint(route(1, 0))["route"] == "openai:active"

    @pytest.mark.parametrize("index,expected", list(enumerate(BOOT_SLOTS)))
    def test_every_configured_tier_is_reachable(self, index, expected):
        fleet = _fleet(**{expected: _tier("m")})
        # Ask for the tier the slot serves, so nothing falls back.
        label = {0: 1, 1: 3, 2: 5}[index]
        assert resolve_endpoint(route(label, 0), fleet)["boot_tier"] == expected
