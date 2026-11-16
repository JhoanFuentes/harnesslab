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
            "OPENAI_OUTPUT_USD_PER_MILLION",
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
