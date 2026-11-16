"""No paid inference: fake the network boundary, exercise the wire contract."""

import io
import json
import os
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

from harnesslab.models import Action
from harnesslab.providers import ACTION_SCHEMA, OpenAIProvider, ProviderError, ScriptedProvider, _NoRedirects


def wire_action(**overrides):
    value = {
        "kind": "finish", "name": "",
        "arguments": {"query": None, "document_id": None, "service": None},
        "diagnosis": "db_pool_exhaustion", "evidence": ["log-1"], "summary": "Pool saturated.",
    }
    value.update(overrides)
    return value


def wire_response(action=None, **overrides):
    value = {
        "id": "resp_authored_fixture", "model": "actual-model-snapshot",
        "status": "completed", "usage": {"input_tokens": 100, "output_tokens": 20},
        "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(action or wire_action())}
        ]}],
    }
    value.update(overrides)
    return value


class ProviderTests(unittest.TestCase):
    def setUp(self):
        # Preserve OS/TLS configuration; isolate only this adapter's inputs.
        self.env = patch.dict(os.environ, {name: "" for name in (
            "OPENAI_MODEL", "OPENAI_API_KEY", "OPENAI_INPUT_USD_PER_MILLION",
            "OPENAI_OUTPUT_USD_PER_MILLION", "OPENAI_CACHED_INPUT_USD_PER_MILLION",
        )})
        self.env.start()
        self.addCleanup(self.env.stop)

    def provider(self, response=None, **kwargs):
        provider = OpenAIProvider("test-model", api_key="secret-not-for-logs", **kwargs)
        provider._opener = Mock()
        result = io.BytesIO(json.dumps(response or wire_response()).encode())
        provider._opener.open.return_value = result
        return provider

    def test_script_restarts_at_durable_index_and_has_no_usage(self):
        actions = [Action("tool", "search", {"query": "payments"}),
                   Action("finish", diagnosis="network_timeout", evidence=["a"])]
        provider = ScriptedProvider(actions)
        result = provider.decide({"decision_index": 1})
        self.assertEqual(result.action.diagnosis, "network_timeout")
        self.assertEqual(result.estimated_cost_usd, 0)
        self.assertEqual(provider.decide({"decision_index": 2}).action.diagnosis, "insufficient_evidence")
        with self.assertRaises(ProviderError):
            provider.decide({"decision_index": -1})

    def test_script_does_not_share_mutable_actions(self):
        original = Action("finish", diagnosis="ok", evidence=["x"])
        provider = ScriptedProvider([original])
        result = provider.decide({"decision_index": 0})
        result.action.evidence.append("tampered")
        self.assertEqual(provider.decide({"decision_index": 0}).action.evidence, ["x"])

    def test_explicit_configuration_and_price_validation(self):
        with self.assertRaises(ValueError):
            OpenAIProvider(api_key="key")
        with self.assertRaises(ValueError):
            OpenAIProvider("model")
        for value in (-1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.provider(input_rate_per_million=value, output_rate_per_million=1)
        with self.assertRaises(ValueError):
            self.provider(input_rate_per_million=1)

    def test_configuration_from_environment(self):
        with patch.dict(os.environ, {"OPENAI_MODEL": "selected", "OPENAI_API_KEY": "key",
                                    "OPENAI_INPUT_USD_PER_MILLION": "2",
                                    "OPENAI_OUTPUT_USD_PER_MILLION": "8"}):
            provider = OpenAIProvider()
        self.assertEqual(provider.model, "selected")
        self.assertEqual(provider.input_rate_per_million, 2)

    def test_request_schema_and_cost_accounting(self):
        provider = self.provider(input_rate_per_million=2, output_rate_per_million=8)
        decision = provider.decide({"task": {"question": "Why?"}, "remaining_output_tokens": 100})
        request = provider._opener.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
        self.assertFalse(payload["store"])
        self.assertEqual(payload["text"]["format"]["schema"], ACTION_SCHEMA)
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertEqual(payload["max_output_tokens"], 100)
        self.assertEqual(decision.action.arguments, {})
        self.assertEqual(decision.input_tokens, 100)
        self.assertEqual(decision.output_tokens, 20)
        self.assertAlmostEqual(decision.estimated_cost_usd, 0.00036)
        self.assertNotIn("secret-not-for-logs", json.dumps(payload))

    def test_no_rates_means_unknown_cost(self):
        self.assertIsNone(self.provider().decide({}).estimated_cost_usd)

    def test_cached_input_costs_use_subsets_and_do_not_double_count_reasoning(self):
        usage = {"input_tokens": 1000, "output_tokens": 100,
                 "input_tokens_details": {"cached_tokens": 800, "cache_write_tokens": 0},
                 "output_tokens_details": {"reasoning_tokens": 60}}
        decision = self.provider(wire_response(usage=usage), input_rate_per_million=2,
                                 output_rate_per_million=8, cached_input_rate_per_million=.5).decide({})
        self.assertEqual((decision.input_tokens, decision.output_tokens, decision.cached_input_tokens), (1000, 100, 800))
        self.assertEqual(decision.cache_write_tokens, 0)
        self.assertAlmostEqual(decision.estimated_cost_usd, .0016)
        self.assertEqual(decision.cost_reason, "configured_rates")
        self.assertEqual((decision.response_id, decision.response_model), ("resp_authored_fixture", "actual-model-snapshot"))

    def test_missing_cached_price_uses_the_full_input_rate(self):
        response = wire_response(usage={"input_tokens": 1000, "output_tokens": 100,
                                       "input_tokens_details": {"cached_tokens": 800}})
        decision = self.provider(response, input_rate_per_million=2, output_rate_per_million=8).decide({})
        self.assertAlmostEqual(decision.estimated_cost_usd, .0028)
        self.assertEqual(decision.cached_input_tokens, 800)
        self.assertEqual(decision.cost_reason, "cached_rate_missing_full_input_rate")
        self.assertIsNone(decision.cache_write_tokens)

    def test_omitted_cache_details_are_unknown_and_priced_as_full_input(self):
        decision = self.provider(input_rate_per_million=2, output_rate_per_million=8,
                                 cached_input_rate_per_million=.5).decide({})
        self.assertIsNone(decision.cached_input_tokens)
        self.assertAlmostEqual(decision.estimated_cost_usd, .00036)
        self.assertEqual(decision.cost_reason, "cache_details_missing_full_input_rate")

    def test_uncached_and_fully_cached_costs(self):
        for cached, expected in ((0, .0028), (1000, .0013)):
            response = wire_response(usage={"input_tokens": 1000, "output_tokens": 100,
                                           "input_tokens_details": {"cached_tokens": cached}})
            with self.subTest(cached=cached):
                decision = self.provider(response, input_rate_per_million=2, output_rate_per_million=8,
                                         cached_input_rate_per_million=.5).decide({})
                self.assertAlmostEqual(decision.estimated_cost_usd, expected)

    def test_invalid_cached_counts_preserve_known_totals_and_safe_metadata(self):
        for cached in (-1, True, "10", 101, None):
            response = wire_response(usage={"input_tokens": 100, "output_tokens": 20,
                                           "input_tokens_details": {"cached_tokens": cached}})
            with self.subTest(cached=cached), self.assertRaises(ProviderError) as caught:
                self.provider(response, input_rate_per_million=2, output_rate_per_million=8).decide({})
            error = caught.exception
            self.assertEqual(error.code, "invalid_cached_input_tokens")
            self.assertEqual((error.input_tokens, error.output_tokens), (100, 20))
            self.assertIsNone(error.cached_input_tokens)
            self.assertIsNone(error.estimated_cost_usd)
            self.assertTrue(error.usage_incomplete)
            self.assertEqual(error.response_id, "resp_authored_fixture")

    def test_cache_write_pricing_fails_closed_with_known_usage(self):
        response = wire_response(usage={"input_tokens": 100, "output_tokens": 20,
                                       "input_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 30}})
        provider = self.provider(response, input_rate_per_million=2, output_rate_per_million=8,
                                 cached_input_rate_per_million=.5)
        with self.assertRaises(ProviderError) as caught:
            provider.decide({})
        error = caught.exception
        self.assertEqual(error.code, "unsupported_cache_write_pricing")
        self.assertEqual((error.input_tokens, error.output_tokens, error.cached_input_tokens, error.cache_write_tokens), (100, 20, 60, 30))
        self.assertFalse(error.usage_incomplete)
        self.assertIsNone(error.estimated_cost_usd)
        self.assertEqual(error.cost_reason, "unsupported_cache_write_pricing")
        self.assertEqual(provider._opener.open.call_count, 1)
        unpriced = self.provider(response).decide({})
        self.assertIsNone(unpriced.estimated_cost_usd)
        self.assertEqual(unpriced.cache_write_tokens, 30)

    def test_invalid_cache_write_and_overlapping_subsets_are_rejected(self):
        for details in ({"cache_write_tokens": -1}, {"cache_write_tokens": True},
                        {"cache_write_tokens": 101}, {"cached_tokens": 80, "cache_write_tokens": 30}, None, []):
            response = wire_response(usage={"input_tokens": 100, "output_tokens": 20, "input_tokens_details": details})
            with self.subTest(details=details), self.assertRaises(ProviderError) as caught:
                self.provider(response).decide({})
            self.assertIsNone(caught.exception.estimated_cost_usd)
            self.assertEqual(caught.exception.input_tokens, 100)

    def test_cached_rates_validate_and_support_environment_configuration(self):
        with patch.dict(os.environ, {"OPENAI_CACHED_INPUT_USD_PER_MILLION": ".5"}):
            self.assertEqual(self.provider(input_rate_per_million=2, output_rate_per_million=8).cached_input_rate_per_million, .5)
        for value in (-1, True, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.provider(input_rate_per_million=2, output_rate_per_million=8, cached_input_rate_per_million=value)
        with self.assertRaises(ValueError):
            self.provider(cached_input_rate_per_million=.5)

    def test_missing_usage_preserves_response_identity_without_claiming_free_cost(self):
        with self.assertRaises(ProviderError) as caught:
            self.provider(wire_response(usage=None)).decide({})
        self.assertEqual(caught.exception.response_id, "resp_authored_fixture")
        self.assertEqual(caught.exception.response_model, "actual-model-snapshot")
        self.assertTrue(caught.exception.usage_incomplete)
        self.assertIsNone(caught.exception.estimated_cost_usd)

    def test_response_metadata_never_persists_an_echoed_key_or_body(self):
        response = wire_response(id="resp_secret-not-for-logs", model="raw body with spaces and secret")
        decision = self.provider(response).decide({})
        self.assertIsNone(decision.response_id)
        self.assertIsNone(decision.response_model)

    def test_tool_arguments_normalize_nulls(self):
        action = wire_action(kind="tool", name="read_document", diagnosis="", evidence=[], summary="",
                             arguments={"query": None, "document_id": "doc-1", "service": None})
        result = self.provider(wire_response(action)).decide({})
        self.assertEqual(result.action.arguments, {"document_id": "doc-1"})

    def test_refusal_and_incomplete_keep_usage(self):
        responses = [
            (wire_response(status="incomplete"), "response_incomplete"),
            (wire_response(output=[{"type": "message", "content": [{"type": "refusal", "refusal": "secret"}]}]), "model_refusal"),
        ]
        for response, code in responses:
            with self.subTest(code=code), self.assertRaises(ProviderError) as caught:
                self.provider(response).decide({})
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.input_tokens, 100)
            self.assertEqual(caught.exception.response_id, "resp_authored_fixture")
            self.assertEqual(caught.exception.response_model, "actual-model-snapshot")
            self.assertNotIn("secret", str(caught.exception))

    def test_bad_actions_are_not_silently_accepted(self):
        actions = [wire_action(kind="shell"), wire_action(arguments={}), wire_action(evidence=[1]),
                   wire_action(extra="x"), wire_action(name="restart_service")]
        for action in actions:
            with self.subTest(action=action), self.assertRaises(ProviderError) as caught:
                self.provider(wire_response(action)).decide({})
            self.assertEqual(caught.exception.code, "invalid_action")

    def test_unknown_usage_does_not_masquerade_as_free(self):
        for usage in (None, {}, {"input_tokens": True, "output_tokens": 1}):
            with self.subTest(usage=usage), self.assertRaises(ProviderError):
                self.provider(wire_response(usage=usage)).decide({})

    def test_http_error_does_not_leak_body_or_key(self):
        provider = self.provider()
        provider._opener.open.side_effect = urllib.error.HTTPError(
            provider.endpoint, 429, "secret-not-for-logs", {}, io.BytesIO(b"server secret"))
        with self.assertRaises(ProviderError) as caught:
            provider.decide({})
        self.assertEqual(str(caught.exception), "http_429")
        self.assertTrue(caught.exception.recoverable)
        self.assertEqual(provider._opener.open.call_count, 1)

    def test_transport_error_is_explicit_and_not_retried(self):
        provider = self.provider()
        provider._opener.open.side_effect = urllib.error.URLError("secret")
        with self.assertRaises(ProviderError) as caught:
            provider.decide({})
        self.assertEqual(str(caught.exception), "transport_error")
        self.assertEqual(provider._opener.open.call_count, 1)

    def test_redirects_are_disabled(self):
        handler = _NoRedirects()
        request = urllib.request.Request("https://api.openai.com/v1/responses", headers={"Authorization": "secret"})
        self.assertIsNone(handler.redirect_request(request, None, 302, "", {}, "https://evil.example"))

    def test_output_budget_prevents_network_call(self):
        provider = self.provider()
        with self.assertRaises(ProviderError):
            provider.decide({"remaining_output_tokens": 0})
        provider._opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
