"""Schedule-aware summaries for repeated agent studies, with honest accounting.

No provider is called here. Trials within an incident are retained together when
bootstrapping. Intervals describe observed case variation, not production accuracy,
statistical power, or uncertainty caused by missing executions.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations
import math
import random
from typing import Any


Slot = tuple[str, str, str, str, int]
Group = tuple[str, str, str]
GRADE_FIELDS = ("passed", "task_success", "evidence", "safety")
SIMULATED_PROVIDERS = {"scripted", "fake", "mock", "fixture", "synthetic"}


def _slot(item: dict[str, Any]) -> Slot:
    if not isinstance(item, dict):
        raise ValueError("Schedule slots and runs must be objects")
    fields = ("provider", "model", "profile", "case_id")
    if any(not isinstance(item.get(key), str) or not item[key].strip() for key in fields):
        raise ValueError("provider, model, profile, and case_id must be nonempty strings")
    if type(item.get("trial")) is not int or item["trial"] < 1:
        raise ValueError("trial must be a positive integer")
    return tuple(item[key] for key in fields) + (item["trial"],)


def _identity(group: Group) -> dict[str, str]:
    return dict(zip(("provider", "model", "profile"), group))


def _validate_run(run: dict[str, Any]) -> None:
    grade = run.get("grade")
    if not isinstance(grade, dict) or any(type(grade.get(key)) is not bool for key in GRADE_FIELDS):
        raise ValueError("Every attempted run needs boolean passed/task_success/evidence/safety graders")
    if not isinstance(run.get("status"), str) or not run["status"].strip():
        raise ValueError("Every attempted run needs a status")
    if type(run.get("usage_incomplete", False)) is not bool:
        raise ValueError("usage_incomplete must be boolean")
    for key in ("estimated_cost_usd", "reported_cost_usd", "latency_ms"):
        value = run.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or value < 0):
            raise ValueError(f"{key} must be nonnegative and finite, or None")
    for key in ("input_tokens", "output_tokens", "model_calls"):
        if key in run and (type(run[key]) is not int or run[key] < 0):
            raise ValueError(f"{key} must be a nonnegative integer")


def _passed(run: dict[str, Any]) -> bool:
    # A declared pass alone is insufficient if any component grader failed.
    return all(run["grade"][key] is True for key in GRADE_FIELDS)


def _costs(runs: list[dict[str, Any]], successes: int) -> dict[str, Any]:
    unknown = sum(run.get("estimated_cost_usd") is None for run in runs)
    incomplete = sum(run.get("usage_incomplete", False) for run in runs)
    known = []
    for run in runs:
        amount = run.get("reported_cost_usd")
        if amount is None:
            amount = run.get("estimated_cost_usd")
        if amount is not None:
            known.append(amount)
    complete = unknown == 0 and incomplete == 0
    total = math.fsum(run["estimated_cost_usd"] for run in runs) if complete else None
    reasons = []
    if not runs:
        reasons.append("no_attempted_runs")
    if not successes:
        reasons.append("no_successful_tasks")
    if unknown:
        reasons.append("unknown_attempt_cost")
    if incomplete:
        reasons.append("incomplete_usage")
    return {
        "total_estimated_cost_usd": total,
        "reported_known_cost_usd": math.fsum(known),
        "known_cost_attempt_count": len(known),
        "unknown_cost_attempt_count": unknown,
        "incomplete_usage_attempt_count": incomplete,
        "cost_is_complete": complete,
        "cost_per_successful_task_usd": total / successes if not reasons else None,
        "cost_per_successful_task_unavailable_reasons": reasons,
    }


def _quantile(ordered: list[float], probability: float) -> float:
    position = (len(ordered) - 1) * probability
    lower, upper = math.floor(position), math.ceil(position)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _latencies(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Observed wall-clock task latency, including failed attempts.

    Nearest-rank percentiles select sorted[ceil(p * n) - 1]. Missing timings are
    never filled with zero; their count accompanies the observed-only statistics.
    """
    values = sorted(run["latency_ms"] for run in runs if run.get("latency_ms") is not None)
    count = len(values)
    return {
        "latency_observed_count": count,
        "latency_missing_count": len(runs) - count,
        "mean_latency_ms": math.fsum(values) / count if count else None,
        "p50_latency_ms": values[math.ceil(.50 * count) - 1] if count else None,
        "p95_latency_ms": values[math.ceil(.95 * count) - 1] if count else None,
    }


def _cluster_interval(totals: list[tuple[int, int]], samples: int, seed: int) -> list[float]:
    """Resample case-level (successes or paired delta, trials) sufficient statistics.

    All trials belonging to a sampled case travel together. The statistic remains
    a trial-weighted rate even when planned trial counts differ between cases.
    """
    rng = random.Random(seed)
    estimates = []
    for _ in range(samples):
        selected = [totals[rng.randrange(len(totals))] for _ in totals]
        estimates.append(sum(value for value, _ in selected) / sum(count for _, count in selected))
    estimates.sort()
    return [_quantile(estimates, .025), _quantile(estimates, .975)]


def _ci_reason(provider: str, runs: list[dict[str, Any]], clusters: int) -> str | None:
    if provider.lower() in SIMULATED_PROVIDERS or any(
        str(run.get("mode", "")).lower() in SIMULATED_PROVIDERS or run.get("is_simulated") is True
        for run in runs
    ):
        return "simulated_provider_or_mode"
    if clusters < 2:
        return "fewer_than_two_case_clusters"
    return None


def summarize_study(
    runs: list[dict[str, Any]],
    schedule: list[dict[str, Any]],
    *,
    bootstrap_samples: int = 2000,
    seed: int = 1729,
) -> dict[str, Any]:
    """Summarize one result per planned provider/model/profile/case/trial slot.

    Failed attempts remain in rates and costs. Unexecuted slots remain missing.
    Group pass rates are conditional on attempted slots; all-trials success uses
    only cases whose entire planned grid exists. Paired comparisons require equal
    trial grids and complete execution on both profiles for every included case.
    """
    if not isinstance(runs, list) or not isinstance(schedule, list) or not schedule:
        raise ValueError("runs and a nonempty schedule must be lists")
    if type(bootstrap_samples) is not int or bootstrap_samples < 1 or type(seed) is not int:
        raise ValueError("bootstrap_samples must be positive and seed must be an integer")
    planned: dict[Slot, dict[str, Any]] = {}
    for item in schedule:
        key = _slot(item)
        if key in planned:
            raise ValueError("Duplicate planned slot")
        planned[key] = item
    observed: dict[Slot, dict[str, Any]] = {}
    for run in runs:
        key = _slot(run)
        if key not in planned:
            raise ValueError("Run is outside the declared schedule")
        if key in observed:
            raise ValueError("Duplicate attempted slot; internal retries belong in the original run")
        _validate_run(run)
        observed[key] = run

    group_plan: dict[Group, dict[str, list[Slot]]] = defaultdict(lambda: defaultdict(list))
    for key in sorted(planned):
        group_plan[key[:3]][key[3]].append(key)
    groups, per_case = [], []
    case_summaries: dict[Group, dict[str, dict[str, Any]]] = {}
    for group, cases in sorted(group_plan.items()):
        group_runs = [observed[key] for keys in cases.values() for key in keys if key in observed]
        case_summaries[group] = {}
        for case_id, keys in cases.items():
            attempted = [observed[key] for key in keys if key in observed]
            successes = sum(_passed(run) for run in attempted)
            missing_trials = [key[4] for key in keys if key not in observed]
            complete = not missing_trials
            summary = {
                **_identity(group), "case_id": case_id,
                "planned_trials": [key[4] for key in keys],
                "attempted_trials": [key[4] for key in keys if key in observed],
                "missing_trials": missing_trials,
                "planned_count": len(keys), "attempted_count": len(attempted),
                "missing_count": len(missing_trials), "passed_count": successes,
                "pass_rate": successes / len(attempted) if attempted else None,
                "complete": complete,
                "all_trials_success": successes == len(keys) if complete else None,
                **_costs(attempted, successes),
            }
            per_case.append(summary)
            case_summaries[group][case_id] = summary
        entries = list(case_summaries[group].values())
        complete_cases = [entry for entry in entries if entry["complete"]]
        attempted_cases = [entry for entry in entries if entry["attempted_count"]]
        cluster_totals = [(entry["passed_count"], entry["attempted_count"]) for entry in attempted_cases]
        reason = _ci_reason(group[0], group_runs, len(cluster_totals))
        planned_count = sum(len(keys) for keys in cases.values())
        attempted_count = len(group_runs)
        passed = sum(_passed(run) for run in group_runs)
        groups.append({
            **_identity(group), "planned_count": planned_count, "attempted_count": attempted_count,
            "missing_count": planned_count - attempted_count,
            "attempt_coverage": attempted_count / planned_count,
            "planned_case_count": len(cases), "attempted_case_count": len(attempted_cases),
            "passed_count": passed, "pass_rate": passed / attempted_count if attempted_count else None,
            "task_success_rate": sum(run["grade"]["task_success"] for run in group_runs) / attempted_count if attempted_count else None,
            "evidence_rate": sum(run["grade"]["evidence"] for run in group_runs) / attempted_count if attempted_count else None,
            "safety_rate": sum(run["grade"]["safety"] for run in group_runs) / attempted_count if attempted_count else None,
            "status_counts": dict(sorted(Counter(run["status"] for run in group_runs).items())),
            "failure_status_counts": dict(sorted(Counter(run["status"] for run in group_runs if not _passed(run)).items())),
            "complete_case_count": len(complete_cases),
            "incomplete_case_ids": [entry["case_id"] for entry in entries if not entry["complete"]],
            "all_trials_success_count": sum(entry["all_trials_success"] for entry in complete_cases),
            "all_trials_success_rate": sum(entry["all_trials_success"] for entry in complete_cases) / len(complete_cases) if complete_cases else None,
            "all_trials_success_reason": None if complete_cases else "no_complete_cases",
            "pass_rate_ci95": None if reason else _cluster_interval(cluster_totals, bootstrap_samples, seed),
            "pass_rate_ci95_reason": reason,
            "pass_rate_ci_case_clusters": len(cluster_totals),
            "observed_input_tokens": sum(run.get("input_tokens", 0) for run in group_runs),
            "observed_output_tokens": sum(run.get("output_tokens", 0) for run in group_runs),
            "model_calls": sum(run.get("model_calls", 0) for run in group_runs),
            **_costs(group_runs, passed),
            **_latencies(group_runs),
        })

    comparisons = []
    identities = sorted({group[:2] for group in group_plan})
    for provider, model in identities:
        profiles = sorted(group[2] for group in group_plan if group[:2] == (provider, model))
        for baseline, candidate in combinations(profiles, 2):
            first, second = (provider, model, baseline), (provider, model, candidate)
            a, b = case_summaries[first], case_summaries[second]
            included, excluded, totals, deltas = [], [], [], []
            baseline_passed = candidate_passed = trial_count = 0
            paired_runs = []
            for case_id in sorted(set(a) | set(b)):
                reasons = []
                left, right = a.get(case_id), b.get(case_id)
                if left is None:
                    reasons.append("case_not_planned_for_baseline")
                if right is None:
                    reasons.append("case_not_planned_for_candidate")
                if left and right and left["planned_trials"] != right["planned_trials"]:
                    reasons.append("planned_trial_grid_mismatch")
                if left and not left["complete"]:
                    reasons.append("incomplete_baseline_trials")
                if right and not right["complete"]:
                    reasons.append("incomplete_candidate_trials")
                if reasons:
                    excluded.append({"case_id": case_id, "reasons": reasons,
                                     "baseline_missing_trials": left["missing_trials"] if left else None,
                                     "candidate_missing_trials": right["missing_trials"] if right else None})
                    continue
                included.append(case_id)
                delta = right["passed_count"] - left["passed_count"]
                trials = left["planned_count"]
                totals.append((delta, trials))
                deltas.append(delta / trials)
                baseline_passed += left["passed_count"]
                candidate_passed += right["passed_count"]
                trial_count += trials
                paired_runs.extend(observed[key] for group in (first, second) for key in group_plan[group][case_id])
            reason = _ci_reason(provider, paired_runs, len(included))
            comparisons.append({
                "provider": provider, "model": model,
                "baseline_profile": baseline, "candidate_profile": candidate,
                "included_case_ids": included, "excluded_cases": excluded,
                "paired_case_count": len(included), "paired_trial_count": trial_count,
                "baseline_pass_rate": baseline_passed / trial_count if trial_count else None,
                "candidate_pass_rate": candidate_passed / trial_count if trial_count else None,
                "mean_delta": (candidate_passed - baseline_passed) / trial_count if trial_count else None,
                "ci95": None if reason else _cluster_interval(totals, bootstrap_samples, seed),
                "ci95_reason": reason,
                "wins": sum(delta > 0 for delta in deltas),
                "ties": sum(delta == 0 for delta in deltas),
                "losses": sum(delta < 0 for delta in deltas),
                "delta_direction": "candidate - baseline",
                "win_tie_loss_unit": "complete paired case",
            })

    return {
        "schema_version": 1,
        "planned_count": len(planned), "attempted_count": len(observed),
        "missing_count": len(planned) - len(observed),
        "missing_slots": [{**_identity(key[:3]), "case_id": key[3], "trial": key[4]}
                          for key in sorted(planned) if key not in observed],
        "groups": groups, "comparisons": comparisons, "per_case": per_case,
        "methodology": {
            "success": "passed AND task_success AND evidence AND safety",
            "pass_rate": "successful attempts / all attempted slots; missing slots are not imputed",
            "all_trials_success": "all planned trials passed, evaluated only on complete cases",
            "cost_per_successful_task": "cost of ALL attempted runs / successful attempted runs, including costs of failed runs",
            "reported_known_cost": "sum of reported_cost_usd, falling back to known estimated cost; lower bound when any usage/cost is unknown",
            "latency": "observed task wall-clock milliseconds including failed attempts; missing timings excluded and counted; p50/p95 use nearest rank ceil(p*n), not interpolation",
            "bootstrap": "percentile 95% interval; resample case IDs with replacement and retain all their trials; trial-weighted statistic",
            "bootstrap_samples": bootstrap_samples, "seed": seed,
            "pairing": "same provider/model; identical planned trial grid and complete execution for each paired case",
            "limitations": [
                "Intervals are conditional on observed cases and attempts; missing execution may bias results.",
                "Repeated trials are not treated as independent case observations.",
                "Scripted/simulated providers and fewer than two case clusters receive no inferential interval.",
                "Complete-case all-trials success does not characterize unexecuted or incomplete cases.",
                "Cost per success covers attempted work only; unexecuted schedule slots have no inferred cost.",
                "The study makes no claim of statistical power, production accuracy, or generalization beyond its cases.",
            ],
        },
    }
