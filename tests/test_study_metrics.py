"""Study accounting and cluster-bootstrap contracts; all fixtures are authored."""

from copy import deepcopy
import unittest

from harnesslab.study_metrics import summarize_study


def slot(case="case-a", profile="hardened", trial=1, provider="openai", model="test-model"):
    return {"case_id": case, "profile": profile, "trial": trial, "provider": provider, "model": model}


def run(planned, passed=True, cost=.1, **extra):
    result = {**planned, "run_id": "fixture-run", "status": "completed" if passed else "provider_error",
              "grade": {"passed": passed, "task_success": passed, "evidence": True, "safety": True},
              "estimated_cost_usd": cost, "usage_incomplete": False,
              "input_tokens": 100, "output_tokens": 20, "model_calls": 1}
    result.update(extra)
    return result


def summary(runs, schedule):
    return summarize_study(runs, schedule, bootstrap_samples=300, seed=1729)


class CostAccountingTests(unittest.TestCase):
    def test_failed_attempt_cost_is_included_in_cost_per_success(self):
        schedule = [slot("a"), slot("b")]
        result = summary([run(schedule[0], cost=.4), run(schedule[1], passed=False, cost=.6)], schedule)
        group = result["groups"][0]
        self.assertEqual(group["passed_count"], 1)
        self.assertAlmostEqual(group["total_estimated_cost_usd"], 1)
        self.assertAlmostEqual(group["cost_per_successful_task_usd"], 1)
        self.assertEqual(group["cost_per_successful_task_unavailable_reasons"], [])
        self.assertEqual(group["failure_status_counts"], {"provider_error": 1})

    def test_no_success_is_undefined_not_free(self):
        schedule = [slot()]
        group = summary([run(schedule[0], passed=False, cost=.75)], schedule)["groups"][0]
        self.assertEqual(group["total_estimated_cost_usd"], .75)
        self.assertIsNone(group["cost_per_successful_task_usd"])
        self.assertIn("no_successful_tasks", group["cost_per_successful_task_unavailable_reasons"])

    def test_unknown_cost_retains_reported_lower_bound(self):
        schedule = [slot("a"), slot("b")]
        runs = [run(schedule[0], cost=.5),
                run(schedule[1], passed=False, cost=None, reported_cost_usd=.2, usage_incomplete=True)]
        group = summary(runs, schedule)["groups"][0]
        self.assertIsNone(group["total_estimated_cost_usd"])
        self.assertIsNone(group["cost_per_successful_task_usd"])
        self.assertAlmostEqual(group["reported_known_cost_usd"], .7)
        self.assertEqual(group["unknown_cost_attempt_count"], 1)
        self.assertEqual(group["incomplete_usage_attempt_count"], 1)
        self.assertEqual(set(group["cost_per_successful_task_unavailable_reasons"]),
                         {"unknown_attempt_cost", "incomplete_usage"})

    def test_incomplete_usage_invalidates_even_a_present_estimate(self):
        schedule = [slot()]
        group = summary([run(schedule[0], cost=2, usage_incomplete=True)], schedule)["groups"][0]
        self.assertFalse(group["cost_is_complete"])
        self.assertEqual(group["reported_known_cost_usd"], 2)
        self.assertIsNone(group["total_estimated_cost_usd"])
        self.assertIsNone(group["cost_per_successful_task_usd"])

    def test_declared_pass_does_not_override_failed_component_grade(self):
        schedule = [slot("a"), slot("b")]
        unsafe = run(schedule[1], cost=1)
        unsafe["grade"]["safety"] = False
        group = summary([run(schedule[0], cost=1), unsafe], schedule)["groups"][0]
        self.assertEqual(group["passed_count"], 1)
        self.assertEqual(group["pass_rate"], .5)
        self.assertEqual(group["cost_per_successful_task_usd"], 2)
        self.assertEqual(group["failure_status_counts"], {"completed": 1})


class LatencyTests(unittest.TestCase):
    def test_latency_includes_failures_and_uses_nearest_rank(self):
        schedule = [slot(str(index)) for index in range(4)]
        runs = [run(planned, passed=index != 3, latency_ms=value)
                for index, (planned, value) in enumerate(zip(schedule, [10, 20, 30, 1000]))]
        group = summary(runs, schedule)["groups"][0]
        self.assertEqual(group["latency_observed_count"], 4)
        self.assertEqual(group["latency_missing_count"], 0)
        self.assertEqual(group["mean_latency_ms"], 265)
        self.assertEqual(group["p50_latency_ms"], 20)
        self.assertEqual(group["p95_latency_ms"], 1000)

    def test_missing_timings_remain_explicit_and_do_not_become_zero(self):
        schedule = [slot("a"), slot("b"), slot("c"), slot("not-attempted")]
        runs = [run(schedule[0], latency_ms=40), run(schedule[1]), run(schedule[2], latency_ms=None)]
        group = summary(runs, schedule)["groups"][0]
        self.assertEqual(group["latency_observed_count"], 1)
        self.assertEqual(group["latency_missing_count"], 2)
        self.assertEqual(group["mean_latency_ms"], 40)
        self.assertEqual(group["p50_latency_ms"], 40)
        self.assertEqual(group["p95_latency_ms"], 40)
        for runs in ([], [run(schedule[0])]):
            with self.subTest(attempts=len(runs)):
                group = summary(runs, schedule)["groups"][0]
                self.assertIsNone(group["mean_latency_ms"])
                self.assertIsNone(group["p50_latency_ms"])
                self.assertIsNone(group["p95_latency_ms"])

    def test_invalid_latency_is_rejected(self):
        planned = slot()
        for value in (-1, float("nan"), float("inf"), True, "20"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                summary([run(planned, latency_ms=value)], [planned])


class ScheduleCompletenessTests(unittest.TestCase):
    def test_unexecuted_slots_remain_visible_without_invented_results(self):
        schedule = [slot("a", trial=1), slot("a", trial=2), slot("b")]
        result = summary([], schedule)
        group = result["groups"][0]
        self.assertEqual((result["planned_count"], result["attempted_count"], result["missing_count"]), (3, 0, 3))
        self.assertEqual(len(result["missing_slots"]), 3)
        self.assertEqual(group["attempt_coverage"], 0)
        self.assertIsNone(group["pass_rate"])
        self.assertIsNone(group["all_trials_success_rate"])
        self.assertIsNone(group["cost_per_successful_task_usd"])
        self.assertIn("no_attempted_runs", group["cost_per_successful_task_unavailable_reasons"])

    def test_one_passed_trial_cannot_make_an_incomplete_case_reliable(self):
        schedule = [slot("partial", trial=1), slot("partial", trial=2), slot("complete")]
        result = summary([run(schedule[0]), run(schedule[2])], schedule)
        group = result["groups"][0]
        cases = {case["case_id"]: case for case in result["per_case"]}
        self.assertEqual(group["pass_rate"], 1)
        self.assertAlmostEqual(group["attempt_coverage"], 2 / 3)
        self.assertEqual(group["complete_case_count"], 1)
        self.assertEqual(group["all_trials_success_count"], 1)
        self.assertEqual(group["all_trials_success_rate"], 1)
        self.assertEqual(group["incomplete_case_ids"], ["partial"])
        self.assertIsNone(cases["partial"]["all_trials_success"])
        self.assertEqual(cases["partial"]["missing_trials"], [2])
        self.assertTrue(cases["complete"]["all_trials_success"])

    def test_complete_case_with_failed_trial_is_not_all_trials_success(self):
        schedule = [slot(trial=1), slot(trial=2)]
        result = summary([run(schedule[0]), run(schedule[1], passed=False)], schedule)
        self.assertFalse(result["per_case"][0]["all_trials_success"])
        self.assertEqual(result["groups"][0]["complete_case_count"], 1)
        self.assertEqual(result["groups"][0]["all_trials_success_rate"], 0)

    def test_models_are_separate_and_planned_empty_groups_survive(self):
        schedule = [slot(model="first"), slot(model="second"), slot(model="not-executed")]
        result = summary([run(schedule[0]), run(schedule[1], passed=False)], schedule)
        groups = {group["model"]: group for group in result["groups"]}
        self.assertEqual(set(groups), {"first", "second", "not-executed"})
        self.assertEqual(groups["first"]["pass_rate"], 1)
        self.assertEqual(groups["second"]["pass_rate"], 0)
        self.assertIsNone(groups["not-executed"]["pass_rate"])
        self.assertEqual(result["comparisons"], [])


class ClusterBootstrapTests(unittest.TestCase):
    def test_repeating_identical_trials_does_not_artificially_narrow_case_interval(self):
        def study(trials):
            schedule = [slot(case, trial=trial) for case in ("easy", "hard") for trial in range(1, trials + 1)]
            runs = [run(planned, passed=planned["case_id"] == "easy") for planned in schedule]
            return summary(runs, schedule)["groups"][0]

        once, repeated = study(1), study(20)
        self.assertEqual(once["pass_rate"], .5)
        self.assertEqual(once["pass_rate_ci95"], [0, 1])
        self.assertEqual(repeated["pass_rate_ci95"], once["pass_rate_ci95"])
        self.assertEqual(repeated["pass_rate_ci_case_clusters"], 2)
        self.assertEqual(repeated["attempted_count"], 40)

    def test_one_case_has_no_inferential_interval_even_with_many_trials(self):
        schedule = [slot(trial=trial) for trial in range(1, 21)]
        group = summary([run(planned) for planned in schedule], schedule)["groups"][0]
        self.assertIsNone(group["pass_rate_ci95"])
        self.assertEqual(group["pass_rate_ci95_reason"], "fewer_than_two_case_clusters")

    def test_scripted_and_fake_modes_suppress_intervals(self):
        for provider, mode in (("scripted", None), ("fake", None), ("openai", "mock")):
            schedule = [slot("a", provider=provider), slot("b", provider=provider)]
            runs = [run(planned, mode=mode) for planned in schedule]
            group = summary(runs, schedule)["groups"][0]
            with self.subTest(provider=provider, mode=mode):
                self.assertIsNone(group["pass_rate_ci95"])
                self.assertEqual(group["pass_rate_ci95_reason"], "simulated_provider_or_mode")

    def test_injected_provider_identity_does_not_enable_inference(self):
        schedule = [slot(case, profile, provider="openai", model="test-model")
                    for case in ("a", "b") for profile in ("baseline", "hardened")]
        runs = [run(planned, is_simulated=True, mode="offline_test") for planned in schedule]
        result = summary(runs, schedule)
        for group in result["groups"]:
            self.assertIsNone(group["pass_rate_ci95"])
            self.assertEqual(group["pass_rate_ci95_reason"], "simulated_provider_or_mode")
        self.assertIsNone(result["comparisons"][0]["ci95"])
        self.assertEqual(result["comparisons"][0]["ci95_reason"], "simulated_provider_or_mode")

    def test_trial_weighted_rate_remains_explicit_with_unequal_trial_counts(self):
        schedule = [slot("easy", trial=trial) for trial in range(1, 5)] + [slot("hard")]
        group = summary([run(planned, passed=planned["case_id"] == "easy") for planned in schedule], schedule)["groups"][0]
        self.assertEqual(group["pass_rate"], .8)
        self.assertEqual(group["pass_rate_ci_case_clusters"], 2)

    def test_seed_and_order_are_deterministic_without_mutating_input(self):
        schedule = [slot(case, trial=trial) for case in ("a", "b", "c") for trial in (1, 2)]
        runs = [run(planned, passed=index % 3 == 0) for index, planned in enumerate(schedule)]
        original = deepcopy((runs, schedule))
        first = summary(runs, schedule)
        second = summary(list(reversed(runs)), list(reversed(schedule)))
        self.assertEqual(first, second)
        self.assertEqual((runs, schedule), original)


class PairedComparisonTests(unittest.TestCase):
    def test_pairing_retains_trials_with_their_cases(self):
        schedule = [slot(case, profile, trial) for case in ("a", "b")
                    for profile in ("baseline", "hardened") for trial in range(1, 11)]
        runs = [run(planned, passed=(planned["case_id"] == "a") == (planned["profile"] == "hardened"))
                for planned in schedule]
        comparison = summary(runs, schedule)["comparisons"][0]
        self.assertEqual(comparison["mean_delta"], 0)
        self.assertEqual(comparison["ci95"], [-1, 1])
        self.assertEqual(comparison["paired_case_count"], 2)
        self.assertEqual(comparison["paired_trial_count"], 20)
        self.assertEqual((comparison["wins"], comparison["ties"], comparison["losses"]), (1, 0, 1))

    def test_incomplete_and_differently_planned_cases_are_explicitly_excluded(self):
        schedule = [slot(case, profile, trial) for case in ("complete", "partial")
                    for profile in ("baseline", "hardened") for trial in (1, 2)]
        schedule += [slot("mismatch", "baseline", 1), slot("mismatch", "hardened", 1),
                     slot("mismatch", "hardened", 2), slot("candidate-only", "hardened", 1)]
        runs = [run(planned) for planned in schedule
                if not (planned["case_id"] == "partial" and planned["profile"] == "hardened" and planned["trial"] == 2)]
        comparison = summary(runs, schedule)["comparisons"][0]
        self.assertEqual(comparison["included_case_ids"], ["complete"])
        self.assertEqual(comparison["paired_trial_count"], 2)
        excluded = {entry["case_id"]: entry for entry in comparison["excluded_cases"]}
        self.assertIn("incomplete_candidate_trials", excluded["partial"]["reasons"])
        self.assertEqual(excluded["partial"]["candidate_missing_trials"], [2])
        self.assertIn("planned_trial_grid_mismatch", excluded["mismatch"]["reasons"])
        self.assertIn("case_not_planned_for_baseline", excluded["candidate-only"]["reasons"])
        self.assertIsNone(comparison["ci95"])

    def test_no_complete_pairs_is_not_zero_delta(self):
        schedule = [slot("a", "baseline"), slot("a", "hardened")]
        comparison = summary([run(schedule[0])], schedule)["comparisons"][0]
        self.assertEqual(comparison["paired_case_count"], 0)
        self.assertIsNone(comparison["mean_delta"])
        self.assertIsNone(comparison["baseline_pass_rate"])

    def test_scripted_pairs_have_descriptive_deltas_without_intervals(self):
        schedule = [slot(case, profile, provider="scripted", model="scripted-v1")
                    for case in ("a", "b") for profile in ("baseline", "hardened")]
        runs = [run(planned, passed=planned["profile"] == "hardened", cost=0) for planned in schedule]
        comparison = summary(runs, schedule)["comparisons"][0]
        self.assertEqual(comparison["mean_delta"], 1)
        self.assertIsNone(comparison["ci95"])
        self.assertEqual(comparison["ci95_reason"], "simulated_provider_or_mode")


class ValidationTests(unittest.TestCase):
    def test_duplicate_unplanned_or_invalid_slots_are_rejected(self):
        planned = slot()
        with self.assertRaises(ValueError):
            summary([], [planned, planned])
        with self.assertRaises(ValueError):
            summary([run(planned), run(planned)], [planned])
        with self.assertRaises(ValueError):
            summary([run(slot(model="unplanned"))], [planned])
        with self.assertRaises(ValueError):
            summary([], [slot(trial=True)])
        with self.assertRaises(ValueError):
            summary([], [])

    def test_malformed_usage_cost_and_graders_cannot_be_silently_counted(self):
        planned = slot()
        for field, value in (("estimated_cost_usd", -.1), ("estimated_cost_usd", float("nan")),
                             ("reported_cost_usd", float("inf")), ("reported_cost_usd", True),
                             ("usage_incomplete", "false"), ("input_tokens", -1)):
            malformed = run(planned)
            malformed[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                summary([malformed], [planned])
        malformed = run(planned)
        malformed["grade"]["passed"] = 1
        with self.assertRaises(ValueError):
            summary([malformed], [planned])

    def test_invalid_bootstrap_settings_are_rejected(self):
        for kwargs in ({"bootstrap_samples": 0}, {"bootstrap_samples": True}, {"seed": "1729"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                summarize_study([], [slot()], **kwargs)


if __name__ == "__main__":
    unittest.main()
