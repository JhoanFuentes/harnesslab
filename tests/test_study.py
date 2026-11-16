"""Offline experiment integration: no real credentials or network requests."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import harnesslab.study as study_module
from harnesslab.cases import get_case
from harnesslab.models import Decision, digest
from harnesslab.providers import ProviderError
from harnesslab.store import Store
from harnesslab.study import alias_map, grade_study_run, load_plan, plan_study, read_api_key, run_study
from harnesslab.study_report import render_plan, render_study_report


class FixtureProvider:
    name = "openai"
    input_rate_per_million = 1.0
    output_rate_per_million = 1.0
    cached_input_rate_per_million = None
    max_output_tokens = 1200
    timeout = 30.0

    def __init__(self, model, actions):
        self.model = model
        self.actions = actions

    def decide(self, context):
        assert "expected_diagnosis" not in json.dumps(context)
        return Decision(self.actions[context["decision_index"]], 500, 500, 0.001)


class StudyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case = get_case("pool-saturation")
        self.actions = self.case.scripted_actions
        case = self.case.to_dict()
        case.pop("scripted_actions")
        data = json.dumps([case]).encode()
        (self.root / "test.json").write_bytes(data)
        (self.root / "manifest.json").write_text(json.dumps({
            "dataset_id": "offline-test-fixture",
            "files": {"test.json": {"sha256": hashlib.sha256(data).hexdigest()}},
            "diagnosis_aliases": {"db_pool_exhaustion": ["database pool exhausted"]},
        }), encoding="utf-8")
        self.spec = {
            "schema_version": 1, "dataset": "test.json", "dataset_manifest": "manifest.json",
            "models": [{"provider": "openai", "model": name, "input_rate_per_million": 1.0,
                        "output_rate_per_million": 1.0} for name in ("test-model-a", "test-model-b")],
            "profiles": ["baseline", "hardened"], "trials": 2, "seed": 1,
            "max_total_cost_usd": 1.0,
            "pricing": {"source_url": "https://example.invalid/test-only", "checked_on": "2026-11-16"},
        }
        self.config = self.root / "config.json"
        self.plan_path = self.root / "plan.json"

    def plan(self):
        self.config.write_text(json.dumps(self.spec), encoding="utf-8")
        return plan_study(self.config, self.plan_path)

    def factory(self, spec, case):
        return FixtureProvider(spec["model"], self.actions)

    def run_offline(self, factory=None):
        return run_study(self.plan_path, self.root / "result", provider_factory=factory or self.factory)

    def test_plan_is_network_free_frozen_and_repeatable(self):
        with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Network forbidden")):
            plan = self.plan()
        self.assertEqual(len(plan["schedule"]), 8)
        self.assertEqual(plan["max_planned_model_requests"], 160)
        self.assertTrue(plan["ready_to_execute"])
        again = plan_study(self.config, self.root / "another.json")
        self.assertEqual(plan["schedule"], again["schedule"])
        with self.assertRaises(FileExistsError):
            plan_study(self.config, self.plan_path)

    def test_tampered_plan_or_dataset_refused(self):
        plan = self.plan()
        plan["max_total_cost_usd"] = 500
        self.plan_path.write_text(json.dumps(plan), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            load_plan(self.plan_path)
        (self.root / "test.json").write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Dataset bytes"):
            plan_study(self.config, self.root / "bad.json")

    def test_runtime_change_refused(self):
        self.plan()
        with patch("harnesslab.study.source_hash", return_value="changed"):
            with self.assertRaisesRegex(ValueError, "Runtime changed"):
                load_plan(self.plan_path)

    def test_all_trials_persist_with_cost_and_replayable_state(self):
        self.plan()
        report = self.run_offline()
        self.assertEqual(report["manifest"]["status"], "completed")
        self.assertEqual(report["manifest"]["mode"], "offline_test")
        self.assertEqual(len(report["runs"]), 8)
        for group in report["metrics"]["groups"]:
            self.assertEqual(group["passed_count"], 2)
            self.assertAlmostEqual(group["cost_per_successful_task_usd"], 0.004)
        store = Store(self.root / "result/traces.sqlite")
        try:
            for run in report["runs"]:
                self.assertEqual(digest(store.replay(run["run_id"])), run["state_sha256"])
        finally:
            store.close()
        self.assertEqual(json.loads((self.root / "result/results.json").read_text())["runs"], report["runs"])
        self.assertIn("OFFLINE TEST", (self.root / "result/report.html").read_text())
        with self.assertRaises(FileExistsError):
            self.run_offline()

    def test_observed_budget_stops_with_missing_slots_visible(self):
        self.spec["max_total_cost_usd"] = 0.005
        self.plan()
        report = self.run_offline()
        self.assertEqual(report["manifest"]["status"], "stopped")
        self.assertEqual(report["manifest"]["stop_reason"], "observed_total_cost_threshold")
        self.assertLess(len(report["runs"]), 8)
        self.assertTrue(any(g["missing_count"] for g in report["metrics"]["groups"]))
        # Sequential, one crossing request is the documented soft-cap allowance.
        cost = sum(run["reported_cost_usd"] for run in report["runs"])
        self.assertLessEqual(cost, 0.006000001)

    def test_unknown_transport_cost_stops_before_next_slot(self):
        self.plan()
        calls = []

        def factory(spec, case):
            provider = self.factory(spec, case)
            def fail(context):
                calls.append(1)
                raise ProviderError("transport_error", recoverable=True)
            provider.decide = fail
            return provider

        report = self.run_offline(factory)
        self.assertEqual(len(calls), 1)
        self.assertEqual(report["manifest"]["stop_reason"], "unknown_usage_or_cost")
        self.assertEqual(len(report["runs"]), 1)
        self.assertIsNone(report["runs"][0]["estimated_cost_usd"])

    def test_billed_invalid_output_counts_as_failure_and_cost(self):
        self.plan()

        def factory(spec, case):
            provider = self.factory(spec, case)
            def fail(context):
                raise ProviderError("invalid_action", input_tokens=10, output_tokens=2, estimated_cost_usd=0.002)
            provider.decide = fail
            return provider

        report = self.run_offline(factory)
        self.assertEqual(len(report["runs"]), 8)
        for group in report["metrics"]["groups"]:
            self.assertEqual(group["passed_count"], 0)
            self.assertIsNone(group["cost_per_successful_task_usd"])
            self.assertAlmostEqual(group["total_estimated_cost_usd"], 0.004)

    def test_keyboard_interrupt_preserves_attempt_and_uncertain_usage(self):
        self.plan()

        def factory(spec, case):
            provider = self.factory(spec, case)
            def interrupt(context):
                raise KeyboardInterrupt()
            provider.decide = interrupt
            return provider

        report = self.run_offline(factory)
        self.assertEqual(report["manifest"]["status"], "interrupted")
        self.assertEqual(len(report["runs"]), 1)
        self.assertTrue(report["runs"][0]["usage_incomplete"])
        self.assertIsNone(report["runs"][0]["estimated_cost_usd"])

    def test_draft_plan_cannot_be_executed(self):
        self.spec["models"][0]["model"] = "REPLACE_MODEL"
        self.spec["models"][0]["input_rate_per_million"] = None
        plan = self.plan()
        self.assertFalse(plan["ready_to_execute"])
        self.assertIn("No model requests were made", render_plan(plan))
        with self.assertRaisesRegex(ValueError, "Draft plan"):
            self.run_offline()
        self.assertFalse((self.root / "result").exists())

    def test_interrupt_after_durable_completion_preserves_success(self):
        self.plan()
        original = study_module._run_record
        calls = []
        def interrupt_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise KeyboardInterrupt()
            return original(*args, **kwargs)
        with patch("harnesslab.study._run_record", side_effect=interrupt_once):
            report = self.run_offline()
        self.assertEqual(report["manifest"]["status"], "interrupted")
        self.assertEqual(len(report["runs"]), 1)
        self.assertEqual(report["runs"][0]["status"], "completed")
        self.assertTrue(report["runs"][0]["grade"]["passed"])
        self.assertAlmostEqual(report["runs"][0]["estimated_cost_usd"], 0.004)

    def test_minimum_model_output_allowance_is_validated_during_planning(self):
        self.spec["config_overrides"] = {"max_tokens": 10}
        with self.assertRaisesRegex(ValueError, "at least 16"):
            self.plan()
        self.assertFalse(self.plan_path.exists())

    def test_missing_key_is_preflight_error_without_artifacts(self):
        self.plan()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
                run_study(self.plan_path, self.root / "result")
        self.assertFalse((self.root / "result").exists())

    def test_key_reader_never_executes_file_and_ignores_prices(self):
        local = self.root / ".env"
        local.write_text('# local only\nOPENAI_API_KEY="test-not-a-real-key"\nOPENAI_MODEL=ignored\n', encoding="utf-8")
        self.assertEqual(read_api_key(local), "test-not-a-real-key")
        local.write_text("OPENAI_API_KEY=\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            read_api_key(local)
        local.write_text("import os\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            read_api_key(local)

    def test_aliases_are_frozen_exact_and_do_not_modify_trace(self):
        self.plan()
        report = self.run_offline()
        store = Store(self.root / "result/traces.sqlite")
        try:
            state = store.replay(report["runs"][0]["run_id"])
        finally:
            store.close()
        state["result"]["diagnosis"] = "DATABASE-POOL EXHAUSTED"
        original = deepcopy(state)
        grade = grade_study_run(state, [], {"db_pool_exhaustion": ["database pool exhausted"]})
        self.assertTrue(grade["passed"])
        self.assertEqual(state, original)
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            alias_map({"cause_a": ["same"], "cause_b": ["SAME"]})

    def test_report_escapes_run_text_and_plan_names(self):
        plan = self.plan()
        plan["models"][0]["model"] = "<script>alert(1)</script>"
        self.assertNotIn("<script>", render_plan(plan))
        report = self.run_offline()
        report["runs"][0]["result"]["summary"] = "<img src=x onerror=alert(1)>"
        rendered = render_study_report(report)
        self.assertNotIn("<img", rendered)
        self.assertIn("&lt;img", rendered)


if __name__ == "__main__":
    unittest.main()
