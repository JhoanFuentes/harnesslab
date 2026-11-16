"""Behavioral contracts at the model/tool/state boundaries."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harnesslab.cases import get_case
from harnesslab.engine import Harness, InjectedCrash, build_context
from harnesslab.models import Action, Config, Decision, canonical
from harnesslab.providers import ProviderError, ScriptedProvider
from harnesslab.store import Store


class MeteredProvider:
    name = "metered-test"
    model = "fixture-model"
    input_rate_per_million = 1.0
    output_rate_per_million = 1.0

    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context)
        value = next(self.decisions)
        if isinstance(value, BaseException):
            raise value
        return value


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "runs.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())

    def fixture(self, case_id="pool-saturation", config=None):
        case = get_case(case_id)
        provider = ScriptedProvider(case.scripted_actions)
        harness = Harness(self.store, provider)
        return harness, harness.start(case, config)

    def test_positive_control_retrieves_then_completes(self):
        harness, run_id = self.fixture()
        state = harness.run(run_id)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["result"]["diagnosis"], "db_pool_exhaustion")
        self.assertEqual(set(state["observed"]), {"pool-metrics", "pool-runbook"})
        self.assertEqual(state["model_calls"], 4)
        self.assertEqual(self.store.replay(run_id), state)

    def test_approval_is_scoped_to_exact_pending_action(self):
        case = get_case("pool-saturation")
        restart = Action("tool", "restart_service", {"service": "checkout"})
        provider = ScriptedProvider([restart, restart])
        harness = Harness(self.store, provider)
        run_id = harness.start(case)
        first = harness.run(run_id)
        self.assertEqual(first["status"], "waiting_approval")
        self.assertEqual(self.store.effects(run_id), [])
        with self.assertRaises(ValueError):
            harness.approve(run_id, "incorrect-token")
        harness.approve(run_id, first["approval_id"])
        second = harness.run(run_id)
        self.assertEqual(second["status"], "waiting_approval")
        self.assertNotEqual(first["approval_id"], second["approval_id"])
        self.assertEqual(len(self.store.effects(run_id)), 1)
        self.assertTrue(self.store.effects(run_id)[0]["approved"])
        with self.assertRaises(ValueError):
            harness.approve(run_id, first["approval_id"])

    def test_other_run_cannot_reuse_approval(self):
        harness, first_id = self.fixture("injected-restart")
        first = harness.run(first_id)
        _, second_id = self.fixture("injected-restart")
        harness.run(second_id)
        with self.assertRaises(ValueError):
            harness.approve(second_id, first["approval_id"])
        self.assertEqual(self.store.effects(first_id), [])
        self.assertEqual(self.store.effects(second_id), [])

    def test_committed_read_survives_new_store_and_provider(self):
        harness, run_id = self.fixture()
        with self.assertRaises(InjectedCrash):
            harness.run(run_id, crash_after_tool=True)
        saved = self.store.replay(run_id)
        self.assertIsNotNone(saved["pending_action"])
        self.assertIsNotNone(self.store.receipt(run_id, saved["action_id"]))
        self.store.close()
        self.store = Store(self.path)
        fresh = Harness(self.store, ScriptedProvider(get_case("pool-saturation").scripted_actions))
        result = fresh.run(run_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["model_calls"], 4)
        commits = [e for e in self.store.events(run_id) if e["type"] == "tool_committed"]
        self.assertEqual(len(commits), 3)

    def test_approved_effect_is_not_duplicated_after_committed_crash(self):
        case = get_case("pool-saturation")
        script = [Action("tool", "restart_service", {"service": "checkout"})] + case.scripted_actions
        harness = Harness(self.store, ScriptedProvider(script))
        run_id = harness.start(case)
        awaiting = harness.run(run_id)
        harness.approve(run_id, awaiting["approval_id"])
        with self.assertRaises(InjectedCrash):
            harness.run(run_id, crash_after_tool=True)
        self.assertEqual(len(self.store.effects(run_id)), 1)
        self.store.close()
        self.store = Store(self.path)
        state = Harness(self.store, ScriptedProvider(script)).run(run_id)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["model_calls"], len(script))
        effects = self.store.effects(run_id)
        self.assertEqual(len(effects), 1)
        self.assertTrue(effects[0]["approved"])

    def test_unknown_tool_and_scope_escape_are_denied_even_in_baseline(self):
        for case_id, reason in (("unknown-tool", "unknown_tool"), ("scope-escape", "out_of_scope_service")):
            with self.subTest(case=case_id):
                harness, run_id = self.fixture(case_id, Config.named("baseline"))
                state = harness.run(run_id)
                self.assertEqual(state["status"], "completed")
                self.assertEqual(state["feedback"][0]["error"], reason)
                self.assertEqual(self.store.effects(run_id), [])

    def test_completion_gate_rejects_unread_fabricated_and_stale_evidence(self):
        for case_id in ("premature-finish", "fabricated-citation", "stale-runbook"):
            with self.subTest(case=case_id):
                harness, run_id = self.fixture(case_id)
                result = harness.run(run_id)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["result"]["diagnosis"], "db_pool_exhaustion")
                rejected = [e for e in self.store.events(run_id) if e["type"] == "completion_rejected"]
                self.assertEqual(len(rejected), 1)

    def test_baseline_is_real_ablation_and_misses_early_evidence(self):
        harness, run_id = self.fixture("premature-finish", Config.named("baseline"))
        result = harness.run(run_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["observed"], {})
        self.assertEqual(result["model_calls"], 1)

    def test_transient_tool_retry_preserves_decision_and_attempt_count(self):
        harness, run_id = self.fixture("transient-evidence")
        result = harness.run(run_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["model_calls"], 4)
        self.assertEqual(result["fault_counts"], {"read_document": 1})
        retries = [e for e in self.store.events(run_id) if e["type"] == "tool_retry"]
        self.assertEqual(len(retries), 1)
        self.assertIn(2, result["tool_attempts"].values())

    def test_step_limit_stops_before_next_model_call(self):
        harness, run_id = self.fixture(config=Config(max_steps=1))
        result = harness.run(run_id)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["steps"], 1)
        self.assertEqual(result["model_calls"], 1)

    def test_request_reservations_survive_operator_retry(self):
        provider = MeteredProvider([ProviderError("transport_error")])
        harness = Harness(self.store, provider)
        run_id = harness.start(get_case("pool-saturation"), Config(max_model_calls=1))
        failed = harness.run(run_id)
        self.assertEqual(failed["status"], "provider_error")
        harness.retry_provider(run_id)
        result = harness.run(run_id)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(len(provider.contexts), 1)

    def test_interrupted_model_request_is_not_silently_repeated_on_resume(self):
        crashing = MeteredProvider([SystemExit("process interrupted during inference")])
        harness = Harness(self.store, crashing)
        run_id = harness.start(get_case("pool-saturation"))
        with self.assertRaises(SystemExit):
            harness.run(run_id)
        saved = self.store.replay(run_id)
        self.assertTrue(saved["request_in_flight"])
        self.assertEqual(saved["model_calls"], 1)
        self.store.close()
        self.store = Store(self.path)
        fresh_provider = MeteredProvider([])
        resumed = Harness(self.store, fresh_provider).run(run_id)
        self.assertEqual(resumed["status"], "provider_error")
        self.assertTrue(resumed["usage_incomplete"])
        self.assertIsNone(resumed["estimated_cost_usd"])
        self.assertEqual(fresh_provider.contexts, [])
        self.assertEqual(resumed["model_calls"], 1)

    def test_uncertain_billing_cannot_retry_under_cost_budget(self):
        provider = MeteredProvider([ProviderError("transport_error")])
        harness = Harness(self.store, provider)
        run_id = harness.start(get_case("pool-saturation"), Config(max_cost_usd=1))
        state = harness.run(run_id)
        self.assertTrue(state["usage_incomplete"])
        with self.assertRaises(ValueError):
            harness.retry_provider(run_id)
        self.assertEqual(self.store.get(run_id)["status"], "provider_error")

    def test_exact_token_limit_stops_before_another_request(self):
        read = Action("tool", "read_document", {"document_id": "pool-metrics"})
        provider = MeteredProvider([Decision(read, 20, 10, 0.001)])
        harness = Harness(self.store, provider)
        run_id = harness.start(get_case("pool-saturation"), Config(max_tokens=30))
        state = harness.run(run_id)
        self.assertEqual(state["status"], "budget_exhausted")
        self.assertEqual(len(provider.contexts), 1)
        self.assertEqual(state["model_calls"], 1)

    def test_post_request_token_limit_prevents_tool_effect(self):
        decision = Decision(Action("tool", "restart_service", {"service": "checkout"}), 20, 20, 0.01)
        provider = MeteredProvider([decision])
        harness = Harness(self.store, provider)
        run_id = harness.start(get_case("pool-saturation"), Config(max_tokens=30, approval_gate=False))
        state = harness.run(run_id)
        self.assertEqual(state["status"], "budget_exhausted")
        self.assertEqual(state["input_tokens"] + state["output_tokens"], 40)
        self.assertEqual(self.store.effects(run_id), [])
        self.assertEqual(state["steps"], 0)

    def test_post_request_cost_limit_prevents_tool_effect(self):
        decision = Decision(Action("tool", "restart_service", {"service": "checkout"}), 20, 20, 0.02)
        harness = Harness(self.store, MeteredProvider([decision]))
        run_id = harness.start(get_case("pool-saturation"), Config(max_cost_usd=0.01, approval_gate=False))
        state = harness.run(run_id)
        self.assertEqual(state["status"], "budget_exhausted")
        self.assertEqual(state["estimated_cost_usd"], 0.02)
        self.assertEqual(self.store.effects(run_id), [])

    def test_resume_rechecks_budget_after_decision_commit_crash(self):
        decision = Decision(Action("tool", "restart_service", {"service": "checkout"}), 20, 20, 0.02)
        configs = [Config(max_tokens=30, approval_gate=False),
                   Config(max_cost_usd=0.01, approval_gate=False)]
        for config in configs:
            with self.subTest(tokens=config.max_tokens, cost=config.max_cost_usd):
                provider = MeteredProvider([decision])
                harness = Harness(self.store, provider)
                run_id = harness.start(get_case("pool-saturation"), config)
                original_save = self.store.save

                def crash_after_commit(state, kind, data=None):
                    original_save(state, kind, data)
                    if kind == "model_decided":
                        raise SystemExit("crash between durable decision and budget check")

                with patch.object(self.store, "save", side_effect=crash_after_commit):
                    with self.assertRaises(SystemExit):
                        harness.run(run_id)
                committed = self.store.replay(run_id)
                self.assertEqual(committed["status"], "running")
                self.assertIsNotNone(committed["pending_action"])
                self.store.close()
                self.store = Store(self.path)
                fresh = MeteredProvider([])
                resumed = Harness(self.store, fresh).run(run_id)
                self.assertEqual(resumed["status"], "budget_exhausted")
                self.assertEqual(self.store.effects(run_id), [])
                self.assertEqual(resumed["model_calls"], 1)
                self.assertEqual(fresh.contexts, [])

    def test_resume_does_not_replenish_retry_budget_after_failed_attempt(self):
        harness, run_id = self.fixture("transient-evidence", Config(max_tool_retries=0))
        original_save = self.store.save

        def crash_after_retry_record(state, kind, data=None):
            original_save(state, kind, data)
            if kind == "tool_retry":
                raise SystemExit("crash after failed attempt before retry-limit handling")

        with patch.object(self.store, "save", side_effect=crash_after_retry_record):
            with self.assertRaises(SystemExit):
                harness.run(run_id)
        interrupted = self.store.replay(run_id)
        failed_action_id = interrupted["action_id"]
        self.assertEqual(interrupted["tool_attempts"][failed_action_id], 1)
        self.store.close()
        self.store = Store(self.path)
        provider = ScriptedProvider(get_case("transient-evidence").scripted_actions)
        result = Harness(self.store, provider).run(run_id)
        self.assertIsNone(self.store.receipt(run_id, failed_action_id))
        self.assertNotIn("pool-metrics", result["observed"])
        self.assertEqual(result["tool_attempts"][failed_action_id], 1)
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["fault_counts"], {"read_document": 1})
        failed_events = [e for e in self.store.events(run_id) if e["type"] == "tool_failed"]
        self.assertEqual(len(failed_events), 1)
        self.assertEqual(failed_events[0]["data"]["error"], "retry_limit")

    def test_provider_failure_preserves_reported_cost_and_tokens(self):
        error = ProviderError("model_refusal", input_tokens=40, output_tokens=10, estimated_cost_usd=0.003)
        harness = Harness(self.store, MeteredProvider([error]))
        run_id = harness.start(get_case("pool-saturation"))
        state = harness.run(run_id)
        self.assertEqual(state["status"], "provider_error")
        self.assertEqual((state["input_tokens"], state["output_tokens"]), (40, 10))
        self.assertAlmostEqual(state["estimated_cost_usd"], 0.003)

    def test_invalid_custom_action_preserves_reported_usage(self):
        decision = Decision(Action("invalid-action-kind"), 40, 10, 0.003)
        harness = Harness(self.store, MeteredProvider([decision]))
        run_id = harness.start(get_case("pool-saturation"))
        state = harness.run(run_id)
        self.assertEqual(state["status"], "provider_error")
        self.assertEqual((state["input_tokens"], state["output_tokens"]), (40, 10))
        self.assertAlmostEqual(state["estimated_cost_usd"], 0.003)
        self.assertFalse(state["usage_incomplete"])
        self.assertEqual(state["model_calls"], 1)
        self.assertEqual(self.store.effects(run_id), [])

    def test_invalid_custom_usage_cannot_reduce_budget_counters(self):
        action = Action("tool", "restart_service", {"service": "checkout"})
        usage_values = [(-1, 1, 0.001), (1, -1, 0.001), (True, 1, 0.001),
                        (1, 1, -0.001), (1, 1, float("nan")), (1, 1, float("inf"))]
        for usage in usage_values:
            with self.subTest(usage=usage):
                harness = Harness(self.store, MeteredProvider([Decision(action, *usage)]))
                run_id = harness.start(get_case("pool-saturation"), Config(approval_gate=False))
                state = harness.run(run_id)
                self.assertEqual(state["status"], "provider_error")
                self.assertEqual((state["input_tokens"], state["output_tokens"]), (0, 0))
                self.assertEqual(state["reported_cost_usd"], 0)
                self.assertTrue(state["usage_incomplete"])
                self.assertIsNone(state["estimated_cost_usd"])
                self.assertEqual(self.store.effects(run_id), [])

    def test_cost_budget_requires_pricing(self):
        provider = MeteredProvider([])
        provider.input_rate_per_million = None
        with self.assertRaises(ValueError):
            Harness(self.store, provider).start(get_case("pool-saturation"), Config(max_cost_usd=1))

    def test_resume_requires_original_provider_and_model(self):
        _, run_id = self.fixture()
        with self.assertRaises(ValueError):
            Harness(self.store, MeteredProvider([])).run(run_id)

    def test_resume_rejects_changed_provider_pricing(self):
        provider = MeteredProvider([])
        harness = Harness(self.store, provider)
        run_id = harness.start(get_case("pool-saturation"))
        provider.input_rate_per_million = 100
        with self.assertRaises(ValueError):
            harness.run(run_id)

    def test_context_omits_oracle_and_script_and_respects_bound(self):
        harness, run_id = self.fixture(config=Config(context_chars=2000))
        harness.run(run_id)
        state = self.store.get(run_id)
        state["case"]["expected_diagnosis"] = "SECRET_ORACLE"
        state["case"]["required_evidence"] = ["SECRET_CITATION"]
        state["case"]["scripted_actions"] = [{"hidden": "SECRET_SCRIPT"}]
        state["observations"].append({"tool": "read_document", "result": "long evidence " * 5000})
        text = canonical(build_context(state))
        self.assertLessEqual(len(text), 2000)
        self.assertNotIn("SECRET_ORACLE", text)
        self.assertNotIn("SECRET_CITATION", text)
        self.assertNotIn("SECRET_SCRIPT", text)
        self.assertIn('"truncated":true', text)
        self.assertGreater(build_context(state)["omitted_observations"], 0)

    def test_context_bounds_oversized_task_fields(self):
        case = replace(get_case("pool-saturation"), service="service" * 2000, question="question" * 2000)
        harness = Harness(self.store, ScriptedProvider([]))
        run_id = harness.start(case, Config(context_chars=2000))
        context = build_context(self.store.get(run_id))
        self.assertLessEqual(len(canonical(context)), 2000)


if __name__ == "__main__":
    unittest.main()
