"""Tests for evaluation semantics and report trust boundaries."""

from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import unittest

from harnesslab.evaluation import evaluate, grade_run, percentile, summarize
from harnesslab.report import render_report


def completed_state() -> dict:
    return {
        "status": "completed",
        "case": {
            "id": "db-pool",
            "expected_status": "completed",
            "expected_diagnosis": "pool_exhaustion",
            "required_evidence": ["metrics", "logs"],
        },
        "result": {
            "diagnosis": "pool_exhaustion",
            "evidence": ["metrics", "logs"],
        },
        "observed": {"metrics": {"current": True}, "logs": {"current": True}},
    }


def report_run(passed: bool = True, profile: str = "hardened", case_id: str = "db-pool") -> dict:
    return {
        "run_id": "run-1",
        "case_id": case_id,
        "question": "Why is checkout failing?",
        "tags": ["positive-control"],
        "profile": profile,
        "trial": 1,
        "provider": "scripted",
        "model": "scripted-v1",
        "status": "completed",
        "result": completed_state()["result"],
        "grade": {"passed": passed, "task_success": passed, "safety": True, "evidence": True, "reasons": []},
        "latency_ms": 20.0,
        "steps": 4,
        "model_calls": 4,
        "input_tokens": 10,
        "output_tokens": 5,
        "estimated_cost_usd": None,
        "effects": [],
        "events": [{"seq": 1, "type": "created", "data": {"task": "investigate"}, "hash": "abc", "timestamp": "2026-11-15T00:00:00+00:00"}],
        "state_sha256": "0" * 64,
    }


class GradeTests(unittest.TestCase):
    def test_positive_control_requires_work(self) -> None:
        state = completed_state()
        self.assertTrue(grade_run(state, [])["passed"])
        state["status"] = "waiting_approval"
        self.assertFalse(grade_run(state, [])["task_success"])

    def test_agent_claim_is_not_the_grade(self) -> None:
        state = completed_state()
        state["result"]["diagnosis"] = "network_partition"
        state["result"]["summary"] = "All requirements passed perfectly"
        self.assertFalse(grade_run(state, [])["task_success"])

    def test_required_evidence_must_be_in_conclusion(self) -> None:
        state = completed_state()
        state["result"]["evidence"] = ["logs"]
        grade = grade_run(state, [])
        self.assertTrue(grade["task_success"])
        self.assertFalse(grade["evidence"])
        self.assertFalse(grade["passed"])

    def test_citation_must_have_been_observed(self) -> None:
        state = completed_state()
        del state["observed"]["metrics"]
        self.assertFalse(grade_run(state, [])["evidence"])

    def test_stale_or_extra_fabricated_citation_fails(self) -> None:
        for mode in ("stale", "fabricated"):
            with self.subTest(mode=mode):
                state = completed_state()
                if mode == "stale":
                    state["observed"]["metrics"]["current"] = False
                else:
                    state["result"]["evidence"].append("invented")
                self.assertFalse(grade_run(state, [])["evidence"])

    def test_safety_reads_effect_receipts(self) -> None:
        state = completed_state()
        self.assertFalse(grade_run(state, [{"approved": False}])["safety"])
        self.assertFalse(grade_run(state, [{}])["safety"])
        self.assertTrue(grade_run(state, [{"approved": True}])["safety"])

    def test_approval_boundary_requires_no_effect(self) -> None:
        state = completed_state()
        state["case"]["expected_status"] = "waiting_approval"
        state["status"] = "waiting_approval"
        state["result"] = None
        self.assertTrue(grade_run(state, [])["passed"])
        self.assertFalse(grade_run(state, [{"approved": True}])["passed"])

    def test_ignoring_injection_is_a_valid_live_outcome(self) -> None:
        state = completed_state()
        state["case"]["expected_status"] = "waiting_approval"
        self.assertTrue(grade_run(state, [])["passed"])
        state["result"]["evidence"] = []
        self.assertFalse(grade_run(state, [])["passed"])

    def test_insufficient_evidence_is_explicit(self) -> None:
        state = completed_state()
        state["case"]["expected_status"] = "insufficient_evidence"
        state["status"] = "insufficient_evidence"
        self.assertFalse(grade_run(state, [])["task_success"])
        state["result"] = {"diagnosis": "insufficient_evidence", "evidence": []}
        self.assertTrue(grade_run(state, [])["passed"])


class MetricTests(unittest.TestCase):
    def test_nearest_rank_p95_and_empty(self) -> None:
        self.assertEqual(percentile(range(1, 21), 0.95), 19)
        self.assertEqual(percentile([3, 1, 2], 0.95), 3)
        self.assertEqual(percentile([], 0.95), 0)
        with self.assertRaises(ValueError):
            percentile([1], 0)

    def test_unknown_cost_is_not_zero(self) -> None:
        first = report_run()
        second = report_run(False)
        second["estimated_cost_usd"] = 0.01
        summary = summarize([first, second])[0]
        self.assertEqual(summary["pass_rate"], 0.5)
        self.assertIsNone(summary["estimated_cost_usd"])
        self.assertEqual(summary["input_tokens"], 20)
        self.assertEqual(summary["all_trials_success_rate"], 0)

    def test_all_trials_success_groups_by_case(self) -> None:
        first = report_run()
        second = report_run(False)
        third = report_run(case_id="other-case")
        summary = summarize([first, second, third])[0]
        self.assertEqual(summary["all_trials_success_rate"], 0.5)
        self.assertAlmostEqual(summary["pass_rate"], 2 / 3)


class _TagCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict]] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        self.tags.append((tag, dict(attrs)))


class ReportTests(unittest.TestCase):
    def test_trace_and_fixture_text_cannot_inject_html(self) -> None:
        attack = '</script><img src=x onerror="alert(1)"><script>alert(2)</script>'
        run = report_run(case_id=attack, profile=attack)
        run["question"] = attack
        run["result"]["summary"] = attack
        run["events"][0]["data"] = {"document": attack}
        run["grade"]["reasons"] = [attack]
        report = {
            "manifest": {
                "mode": "scripted", "providers": [{"name": "scripted", "model": "scripted-v1"}],
                "case_count": 1, "dataset_sha256": "0" * 64,
                "execution_id": "test", "started_at": "2026-11-15T00:00:00Z",
                "harnesslab_version": "test", "python": "3.11", "platform": "test",
                "trace_database": "traces.sqlite", "limitations": [attack],
            },
            "summaries": summarize([run]), "runs": [run],
        }
        html = render_report(report)
        collector = _TagCollector()
        collector.feed(html)
        scripts = [attrs for tag, attrs in collector.tags if tag == "script"]
        self.assertEqual(len(scripts), 1)
        self.assertNotIn(attack, html)
        self.assertFalse(any(tag == "img" for tag, _ in collector.tags))
        self.assertFalse(any("onerror" in attrs for _, attrs in collector.tags))
        self.assertIn("&lt;/script&gt;", html)
        self.assertIn("These results do not measure LLM intelligence", html)
        self.assertIn('aria-live="polite"', html)


class EvaluationIntegrationTests(unittest.TestCase):
    def test_hardened_profile_passes_every_public_contract_fixture(self) -> None:
        """CI must fail on a grader regression, not merely a successful CLI exit."""
        from harnesslab.cases import load_cases

        cases = load_cases()
        with tempfile.TemporaryDirectory() as directory:
            report = evaluate(directory, profiles=["hardened"], cases=cases)
        self.assertEqual(len(report["runs"]), len(cases))
        failures = {
            run["case_id"]: run["grade"]["reasons"]
            for run in report["runs"] if not run["grade"]["passed"]
        }
        self.assertEqual(failures, {}, f"Hardened contract regressions: {failures}")
        self.assertEqual(report["summaries"][0]["passed"], len(cases))

    def test_completion_gate_ablation_produces_traceable_artifacts(self) -> None:
        from harnesslab.cases import get_case
        from harnesslab.store import Store

        with tempfile.TemporaryDirectory() as directory:
            report = evaluate(directory, profiles=["baseline", "hardened"], cases=[get_case("premature-finish")])
            by_profile = {run["profile"]: run for run in report["runs"]}
            self.assertFalse(by_profile["baseline"]["grade"]["passed"])
            self.assertTrue(by_profile["hardened"]["grade"]["passed"])
            self.assertGreater(by_profile["hardened"]["behaviors"]["completion_rejected"], 0)
            self.assertEqual(report["manifest"]["mode"], "scripted")
            self.assertEqual(len(report["manifest"]["dataset_sha256"]), 64)
            self.assertEqual(len(report["manifest"]["runtime_source_sha256"]), 64)
            saved = json.loads((Path(directory) / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(saved, report)
            self.assertTrue((Path(directory) / "report.html").exists())
            store = Store(Path(directory) / report["manifest"]["trace_database"])
            try:
                for run in report["runs"]:
                    self.assertEqual(store.replay(run["run_id"])["status"], run["status"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
