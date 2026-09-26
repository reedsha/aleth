"""The System 2 provider seam: route names, configuration and one call.

These pin the contract the workflow relies on -- a route's transport prefix is stripped,
an incomplete endpoint is "not configured" rather than an error, the opt-out flag is
honoured, and a failed call returns ``None`` instead of raising into the caller.
"""

import os
import unittest
from types import SimpleNamespace
from unittest import mock

from orchestration import system2


class _Completions:
    """A fake ``client.chat.completions`` recording the kwargs it was called with."""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.response


class _Client:
    def __init__(self, completions):
        self.chat = SimpleNamespace(completions=completions)


def _response(content, prompt=12, completion=7, finish_reason="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


class RouteModelTests(unittest.TestCase):
    def test_provider_prefix_is_stripped(self):
        self.assertEqual(system2.route_model("openai:policy/coder-deep-test"), "policy/coder-deep-test")

    def test_a_plain_model_name_is_unchanged(self):
        self.assertEqual(system2.route_model("policy/free"), "policy/free")


class ConfigurationTests(unittest.TestCase):
    CONFIGURED = {"OPENAI_API_KEY": "sk-test", "OPENAI_BASE_URL": "https://router.example/v1"}

    def test_missing_key_or_base_url_is_not_configured(self):
        for env in ({}, {"OPENAI_API_KEY": "sk"}, {"OPENAI_BASE_URL": "https://x"}):
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertIsNone(system2.provider_config())
                self.assertFalse(system2.is_configured())

    def test_an_empty_export_counts_as_unset(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "  ", "OPENAI_BASE_URL": "https://x"}, clear=True):
            self.assertIsNone(system2.provider_config())

    def test_base_url_is_normalised(self):
        with mock.patch.dict(os.environ, self.CONFIGURED, clear=True):
            self.assertEqual(system2.provider_config()["base_url"], "https://router.example/v1")

    def test_enabled_once_configured(self):
        with mock.patch.dict(os.environ, self.CONFIGURED, clear=True):
            self.assertTrue(system2.is_enabled())

    def test_the_opt_out_flag_beats_a_configured_provider(self):
        for flag in ("0", "false", "OFF", " no "):
            env = dict(self.CONFIGURED, DEEPAGENTS_SYSTEM2=flag)
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertFalse(system2.is_enabled())

    def test_unset_flag_leaves_a_bare_checkout_off(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(system2.is_enabled())


class CompleteTests(unittest.TestCase):
    def test_no_configuration_returns_none_without_a_client(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(system2.complete(model="openai:policy/x", system="s", user="u"))

    def test_a_successful_call_reports_text_and_usage(self):
        completions = _Completions(response=_response("print('hi')\n", prompt=30, completion=9))
        result = system2.complete(
            model="openai:policy/coder-deep-test", system="sys", user="ask", client=_Client(completions)
        )
        self.assertEqual(result.text, "print('hi')\n")
        self.assertEqual(result.model, "openai:policy/coder-deep-test")
        self.assertEqual(result.prompt_tokens, 30)
        self.assertEqual(result.completion_tokens, 9)
        self.assertEqual(result.total_tokens, 39)

    def test_the_route_prefix_does_not_reach_the_api(self):
        completions = _Completions(response=_response("x"))
        system2.complete(
            model="openai:policy/coder-deep-test", system="s", user="u", client=_Client(completions)
        )
        self.assertEqual(completions.kwargs["model"], "policy/coder-deep-test")

    def test_a_finished_answer_is_reported_as_finished(self):
        completions = _Completions(response=_response("ok", finish_reason="stop"))
        result = system2.complete(model="m", system="s", user="u", client=_Client(completions))
        self.assertEqual(result.finish_reason, "stop")

    def test_a_length_finish_is_reported_faithfully(self):
        completions = _Completions(response=_response("partial", finish_reason="length"))
        result = system2.complete(model="m", system="s", user="u", client=_Client(completions))
        self.assertEqual(result.finish_reason, "length")

    def test_a_transport_error_returns_none_instead_of_raising(self):
        completions = _Completions(error=RuntimeError("connection reset"))
        self.assertIsNone(
            system2.complete(model="openai:policy/x", system="s", user="u", client=_Client(completions))
        )

    def test_an_empty_answer_returns_a_completion_with_empty_text(self):
        # The caller decides what "empty" means; the seam reports it faithfully.
        completions = _Completions(response=_response(""))
        result = system2.complete(
            model="openai:policy/x", system="s", user="u", client=_Client(completions)
        )
        self.assertEqual(result.text, "")

    def test_no_choices_returns_none(self):
        response = SimpleNamespace(choices=[], usage=None)
        self.assertIsNone(
            system2.complete(
                model="openai:policy/x", system="s", user="u",
                client=_Client(_Completions(response=response)),
            )
        )


if __name__ == "__main__":
    unittest.main()
