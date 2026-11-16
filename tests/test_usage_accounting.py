"""Auditable usage at durable boundaries; every HTTP response is authored locally."""

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from harnesslab.cases import get_case
from harnesslab.cli import provider_for
from harnesslab.engine import Harness
from harnesslab.models import Action, Config, Decision
from harnesslab.providers import OpenAIProvider, ProviderError
from harnesslab.store import Store


def response(identifier="resp_fixture", *, cached=60, written=0, tool=False):
    action = {"kind": "tool" if tool else "finish", "name": "search" if tool else "",
              "arguments": {"query": "checkout" if tool else None, "document_id": None, "service": None},
              "diagnosis": "" if tool else "insufficient_evidence", "evidence": [], "summary": ""}
    return {"id": identifier, "model": "actual-snapshot-2026-01-01", "status": "completed",
            "usage": {"input_tokens": 100, "output_tokens": 20,
                      "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": written}},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(action)}]}]}


class UsageAccountingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "runs.sqlite")
        self.addCleanup(self.store.close)
        self.env = patch.dict(os.environ, {"OPENAI_API_KEY": "authored-test-key", "OPENAI_MODEL": "",
                                          "OPENAI_INPUT_USD_PER_MILLION": "", "OPENAI_OUTPUT_USD_PER_MILLION": "",
                                          "OPENAI_CACHED_INPUT_USD_PER_MILLION": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def provider(self, *responses, cached_price=.5):
        provider = OpenAIProvider("requested-alias", api_key="authored-test-key", input_rate_per_million=2,
                                  output_rate_per_million=8, cached_input_rate_per_million=cached_price)
        provider._opener = Mock()
        provider._opener.open.side_effect = [io.BytesIO(json.dumps(value).encode()) for value in responses]
        return provider

    def test_success_accumulates_cache_subsets_and_records_each_response(self):
        provider = self.provider(response("resp_first", cached=60, tool=True), response("resp_second", cached=30))
        harness = Harness(self.store, provider)
        run_id = harness.start(get_case("pool-saturation"))
        state = harness.run(run_id)
        self.assertEqual((state["input_tokens"], state["output_tokens"], state["cached_input_tokens"], state["cache_write_tokens"]),
                         (200, 40, 90, 0))
        self.assertFalse(state["cached_input_tokens_incomplete"])
        self.assertFalse(state["cache_write_tokens_incomplete"])
        self.assertAlmostEqual(state["estimated_cost_usd"], .000585)
        self.assertEqual(state["model"], "requested-alias")
        self.assertEqual(state["provider_settings"]["cached_input_rate_per_million"], .5)
        events = [event["data"] for event in self.store.events(run_id) if event["type"] == "model_decided"]
        self.assertEqual([event["response_id"] for event in events], ["resp_first", "resp_second"])
        self.assertEqual([event["cached_input_tokens"] for event in events], [60, 30])
        self.assertEqual(state["last_model_call"]["response_model"], "actual-snapshot-2026-01-01")
        self.assertEqual(self.store.replay(run_id), state)
        self.assertNotIn("authored-test-key", json.dumps(self.store.events(run_id)))

    def test_unpriced_cache_writes_stop_before_action_and_block_cost_budget_retry(self):
        provider = self.provider(response(written=30))
        harness = Harness(self.store, provider)
        run_id = harness.start(get_case("pool-saturation"), Config(max_cost_usd=1))
        state = harness.run(run_id)
        self.assertEqual(state["status"], "provider_error")
        self.assertEqual((state["input_tokens"], state["output_tokens"], state["cached_input_tokens"], state["cache_write_tokens"]),
                         (100, 20, 60, 30))
        self.assertIsNone(state["estimated_cost_usd"])
        self.assertFalse(state["usage_incomplete"])
        self.assertTrue(state["cost_incomplete"])
        self.assertEqual(state["last_model_call"]["cost_reason"], "unsupported_cache_write_pricing")
        self.assertEqual(self.store.effects(run_id), [])
        self.assertEqual(provider._opener.open.call_count, 1)
        with self.assertRaisesRegex(ValueError, "cost budget"):
            harness.retry_provider(run_id)
        event = next(event for event in self.store.events(run_id) if event["type"] == "model_failed")
        self.assertEqual(event["data"]["response_id"], "resp_fixture")

    def test_missing_usage_preserves_metadata_and_marks_totals_unknown(self):
        wire = response()
        wire["usage"] = None
        harness = Harness(self.store, self.provider(wire))
        state = harness.run(harness.start(get_case("pool-saturation")))
        self.assertEqual(state["last_model_call"]["response_id"], "resp_fixture")
        self.assertEqual(state["last_model_call"]["response_model"], "actual-snapshot-2026-01-01")
        self.assertEqual(state["last_model_call"]["error"], "missing_usage")
        self.assertTrue(state["usage_incomplete"])
        self.assertIsNone(state["estimated_cost_usd"])
        self.assertEqual(state["reported_cost_usd"], 0)

    def test_malformed_cache_counts_keep_reported_base_counts_on_failure(self):
        harness = Harness(self.store, self.provider(response(cached=101)))
        state = harness.run(harness.start(get_case("pool-saturation")))
        self.assertEqual((state["input_tokens"], state["output_tokens"]), (100, 20))
        self.assertEqual(state["cached_input_tokens"], 0)
        self.assertTrue(state["cached_input_tokens_incomplete"])
        self.assertTrue(state["usage_incomplete"])
        self.assertIsNone(state["estimated_cost_usd"])

    def test_reported_zero_usage_on_incomplete_output_is_not_missing_usage(self):
        wire = response(cached=0)
        wire["status"] = "incomplete"
        wire["usage"].update(input_tokens=0, output_tokens=0)
        harness = Harness(self.store, self.provider(wire))
        state = harness.run(harness.start(get_case("pool-saturation")))
        self.assertEqual(state["status"], "provider_error")
        self.assertFalse(state["usage_incomplete"])
        self.assertFalse(state["cost_incomplete"])
        self.assertEqual(state["estimated_cost_usd"], 0)

    def test_new_settings_roundtrip_through_cli_and_reject_changed_cached_price(self):
        provider = self.provider(response())
        harness = Harness(self.store, provider)
        run_id = harness.start(get_case("pool-saturation"))
        state = self.store.get(run_id)
        restored = provider_for(get_case("pool-saturation"), "openai", state["model"], state["provider_settings"])
        self.assertEqual(restored.cached_input_rate_per_million, .5)
        self.assertEqual(Harness(self.store, restored)._provider_settings(), state["provider_settings"])
        provider.cached_input_rate_per_million = .75
        with self.assertRaisesRegex(ValueError, "original provider settings"):
            harness.run(run_id)
        provider._opener.open.assert_not_called()

    def test_legacy_settings_without_cached_price_remain_resumable(self):
        provider = self.provider(response(), cached_price=None)
        harness = Harness(self.store, provider)
        settings = harness._provider_settings()
        settings.pop("cached_input_rate_per_million")
        run_id = self.store.create(get_case("pool-saturation"), Config(), provider.name, provider.model, settings)
        state = harness.run(run_id)
        self.assertEqual(state["status"], "insufficient_evidence")
        self.assertEqual(state["provider_settings"], settings)
        self.assertEqual(provider._opener.open.call_count, 1)

    def test_unknown_cost_does_not_become_known_after_explicit_retry(self):
        class Metered:
            name, model = "metered", "fixture"
            input_rate_per_million = output_rate_per_million = 1.0

            def __init__(self):
                self.values = iter([
                    ProviderError("unsupported_cache_write_pricing", input_tokens=100, output_tokens=20,
                                  cached_input_tokens=0, cache_write_tokens=20, usage_incomplete=False),
                    Decision(Action("finish", diagnosis="insufficient_evidence"), 30, 10, .0001,
                             cached_input_tokens=0, cache_write_tokens=0),
                ])

            def decide(self, context):
                value = next(self.values)
                if isinstance(value, Exception):
                    raise value
                return value

        harness = Harness(self.store, Metered())
        run_id = harness.start(get_case("pool-saturation"))
        harness.run(run_id)
        harness.retry_provider(run_id)
        state = harness.run(run_id)
        self.assertEqual(state["status"], "insufficient_evidence")
        self.assertEqual((state["input_tokens"], state["output_tokens"], state["cache_write_tokens"]), (130, 30, 20))
        self.assertAlmostEqual(state["reported_cost_usd"], .0001)
        self.assertIsNone(state["estimated_cost_usd"])
        self.assertTrue(state["cost_incomplete"])

    def test_crash_after_failure_event_cannot_implicitly_retry(self):
        provider = self.provider(response(written=30))
        harness = Harness(self.store, provider)
        run_id = harness.start(get_case("pool-saturation"))
        original = self.store.save

        def crash(state, kind, data=None):
            original(state, kind, data)
            if kind == "model_failed":
                raise SystemExit("authored crash")

        with patch.object(self.store, "save", side_effect=crash), self.assertRaises(SystemExit):
            harness.run(run_id)
        state = harness.run(run_id)
        self.assertEqual(state["status"], "provider_error")
        self.assertEqual(provider._opener.open.call_count, 1)
        self.assertEqual(state["last_model_call"]["error"], "unsupported_cache_write_pricing")


if __name__ == "__main__":
    unittest.main()
