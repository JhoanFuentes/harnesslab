"""Frozen, randomized repeated-trial studies; preparation never invokes a model.

The original scripted conformance evaluator stays independent. A study records
every planned slot, including work omitted after a cost or infrastructure stop.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import re
import sys
import time
from typing import Any, Callable
import unicodedata
import uuid

from . import __version__
from .engine import Harness
from .evaluation import grade_run
from .models import Case, Config, Provider, digest
from .providers import OpenAIProvider, SYSTEM_PROMPT
from .store import Store


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def source_hash() -> str:
    return digest({p.name: p.read_text(encoding="utf-8")
                   for p in sorted(Path(__file__).parent.glob("*.py"))})


def normalize_diagnosis(value: str) -> str:
    return re.sub(r"[\s-]+", "_", unicodedata.normalize("NFKC", value).casefold().strip())


def alias_map(aliases: dict[str, list[str]]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for canonical, values in aliases.items():
        if not isinstance(canonical, str) or not isinstance(values, list):
            raise ValueError("Diagnosis aliases must map strings to lists of strings")
        for value in [canonical, *values]:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Diagnosis aliases must be nonempty strings")
            normalized = normalize_diagnosis(value)
            if normalized in mapping and mapping[normalized] != canonical:
                raise ValueError("Ambiguous diagnosis alias")
            mapping[normalized] = canonical
    return mapping


def grade_study_run(state: dict, effects: list[dict], aliases: dict) -> dict:
    """Only the evaluator receives the frozen alias rubric, never the provider."""
    mapping = alias_map(aliases)
    graded_state = deepcopy(state)
    raw = (state.get("result") or {}).get("diagnosis", "")
    normalized = mapping.get(normalize_diagnosis(raw), normalize_diagnosis(raw))
    if graded_state.get("result"):
        graded_state["result"]["diagnosis"] = normalized
    grade = grade_run(graded_state, effects)
    grade.update(raw_diagnosis=raw, normalized_diagnosis=normalized,
                 rubric="frozen_aliases_v1; no fuzzy matching or model judge")
    return grade


def _number(value: Any, name: str, *, zero: bool = False) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or (value < 0 if zero else value <= 0):
        raise ValueError(f"{name} must be a finite {'nonnegative' if zero else 'positive'} number")
    return float(value)


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise ValueError(f"Invalid JSON in {path.name}") from None


def plan_study(config_path: str | Path, output_path: str | Path) -> dict:
    """Freeze dataset, rubric, settings, source identity and randomized schedule."""
    config_path, output_path = Path(config_path), Path(output_path)
    spec = _json(config_path)
    allowed = {"schema_version", "dataset", "dataset_manifest", "models", "profiles", "trials",
               "seed", "max_total_cost_usd", "config_overrides", "pricing", "name"}
    if not isinstance(spec, dict) or set(spec) - allowed or spec.get("schema_version") != 1:
        raise ValueError("Unsupported study configuration or unknown fields")
    dataset_path = config_path.parent / spec["dataset"]
    manifest_path = config_path.parent / spec["dataset_manifest"]
    dataset_bytes = dataset_path.read_bytes()
    dataset = _json(dataset_path)
    dataset_manifest = _json(manifest_path)
    expected = dataset_manifest["files"][dataset_path.name]["sha256"]
    if hashlib.sha256(dataset_bytes).hexdigest() != expected:
        raise ValueError("Dataset bytes differ from the frozen dataset manifest")
    cases = [Case.from_dict(row) for row in dataset]
    if not cases or len({case.id for case in cases}) != len(cases):
        raise ValueError("Study needs nonempty cases with unique IDs")
    if any(case.scripted_actions for case in cases):
        raise ValueError("Live study datasets must not contain scripted decision tapes")
    if any(case.expected_status not in {"completed", "insufficient_evidence"} for case in cases):
        raise ValueError("Study success requires a diagnosis or justified abstention, not approval alone")
    aliases = dataset_manifest["diagnosis_aliases"]
    mapping = alias_map(aliases)
    if any(mapping.get(normalize_diagnosis(c.expected_diagnosis)) != c.expected_diagnosis for c in cases):
        raise ValueError("Every expected diagnosis must have a canonical rubric entry")
    trials = spec.get("trials", 5)
    if type(trials) is not int or not 2 <= trials <= 100:
        raise ValueError("Studies require 2 to 100 trials per case, model and profile")
    seed = spec.get("seed", 1729)
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    profiles = spec.get("profiles", ["baseline", "hardened"])
    if not profiles or len(set(profiles)) != len(profiles):
        raise ValueError("Study profiles must be nonempty and unique")
    overrides = spec.get("config_overrides", {})
    if set(overrides) - {"max_steps", "max_model_calls", "max_tokens", "max_cost_usd", "context_chars"}:
        raise ValueError("Unknown study runtime limit")
    configs = {name: replace(Config.named(name), **overrides).to_dict() for name in profiles}
    if any(config["max_tokens"] < 16 for config in configs.values()):
        raise ValueError("OpenAI studies require max_tokens of at least 16")
    budget = _number(spec.get("max_total_cost_usd"), "max_total_cost_usd")
    models = spec.get("models", [])
    if not models or not isinstance(models, list):
        raise ValueError("Supply at least one model specification")
    model_fields = {"provider", "model", "input_rate_per_million", "output_rate_per_million",
                    "cached_input_rate_per_million", "max_output_tokens", "timeout"}
    readiness = []
    for model in models:
        if not isinstance(model, dict) or set(model) - model_fields or model.get("provider") != "openai":
            raise ValueError("Only explicit OpenAI model specifications are supported")
        if not isinstance(model.get("model"), str) or not model["model"].strip():
            raise ValueError("Each model needs an identifier")
        if model["model"].startswith("REPLACE_"):
            readiness.append(f"Select a supported model snapshot for {model['model']}")
        for key in ("input_rate_per_million", "output_rate_per_million", "cached_input_rate_per_million"):
            rate = model.get(key)
            if rate is None:
                if key != "cached_input_rate_per_million":
                    readiness.append(f"Set {key} for {model['model']}")
            else:
                _number(rate, key, zero=True)
        model.setdefault("max_output_tokens", 1200)
        model.setdefault("timeout", 30.0)
        if type(model["max_output_tokens"]) is not int or not 16 <= model["max_output_tokens"] <= 16384:
            raise ValueError("max_output_tokens must be an integer between 16 and 16384")
        _number(model["timeout"], "timeout")
    if len({(m["provider"], m["model"]) for m in models}) != len(models):
        raise ValueError("Model identities must be unique")
    pricing = spec.get("pricing", {})
    if not pricing.get("checked_on") or not pricing.get("source_url"):
        readiness.append("Record pricing source_url and checked_on before running")
    schedule = [{"case_id": case.id, "trial": trial, "profile": profile,
                 "provider": model["provider"], "model": model["model"]}
                for case in cases for trial in range(1, trials + 1)
                for profile in profiles for model in models]
    random.Random(seed).shuffle(schedule)
    plan = {
        "artifact_type": "harnesslab.study_plan", "schema_version": 1,
        "created_at": utc_now(), "name": spec.get("name", "Repeated incident diagnosis study"),
        "ready_to_execute": not readiness, "readiness_issues": readiness,
        "runtime_source_sha256": source_hash(), "prompt_sha256": digest(SYSTEM_PROMPT),
        "dataset_sha256": expected, "dataset_manifest_sha256": digest(dataset_manifest),
        "dataset_id": dataset_manifest.get("dataset_id"), "dataset_file": dataset_path.name,
        "cases": dataset, "diagnosis_aliases": aliases, "models": models,
        "configs": configs, "trials": trials, "seed": seed, "schedule": schedule,
        "max_total_cost_usd": budget, "pricing": pricing,
        "max_planned_model_requests": sum(configs[s["profile"]]["max_model_calls"] for s in schedule),
        "ordering": "Seeded shuffle of all case/trial/model/profile slots; sequential execution",
        "budget_definition": "Observed token-price estimate; stop after crossing request, not a provider billing ceiling",
        "success_definition": "Frozen diagnosis/abstention rubric AND required observed current evidence AND approved effects",
        "cost_per_success_definition": "Estimated cost of ALL attempted tasks / tasks passing every grader; undefined at zero successes",
        "limitations": [
            "Synthetic public cases; no real incident distribution or unseen-benchmark claim.",
            "No human semantic-entailment review; frozen aliases may reject valid paraphrases.",
            "Repeated trials share cases and are not independent task samples.",
            "A single in-flight response may cross the observed spend threshold.",
            "Task cost is model inference only; excludes hosting, development and human review.",
        ],
    }
    plan["plan_sha256"] = digest(plan)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(plan, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    return plan


def load_plan(path: str | Path) -> dict:
    plan = _json(Path(path))
    checksum = plan.get("plan_sha256")
    if checksum != digest({k: v for k, v in plan.items() if k != "plan_sha256"}):
        raise ValueError("Study plan hash mismatch; generate a new plan instead of editing it")
    if plan.get("artifact_type") != "harnesslab.study_plan" or plan.get("schema_version") != 1:
        raise ValueError("Unsupported study plan")
    if plan["runtime_source_sha256"] != source_hash():
        raise ValueError("Runtime changed after planning; generate a new plan")
    return plan


def read_api_key(env_file: str | Path | None = None) -> str | None:
    """Read an explicitly named local file without executing shell expressions."""
    if env_file is None:
        return os.environ.get("OPENAI_API_KEY")
    key = None
    for line in Path(env_file).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not name.strip().startswith("OPENAI_"):
            raise ValueError("Environment file must contain OPENAI_* assignments only")
        if name.strip() != "OPENAI_API_KEY":
            continue  # Study settings and prices come only from the frozen plan.
        if key is not None:
            raise ValueError("Duplicate OPENAI_API_KEY assignment")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        key = value
    if not key or any(char.isspace() for char in key):
        raise ValueError("Set a nonempty OPENAI_API_KEY in the specified local file")
    return key


def _persist(directory: Path, report: dict) -> None:
    from .study_metrics import summarize_study
    from .study_report import render_study_report
    for run in report["runs"]:
        run["mode"] = report["manifest"]["mode"]
        run["is_simulated"] = report["manifest"]["mode"] != "live"
    report["metrics"] = summarize_study(report["runs"], report["plan"]["schedule"],
                                        bootstrap_samples=200 if report["manifest"]["status"] == "running" else 2000,
                                        seed=report["plan"]["seed"])
    report["manifest"]["updated_at"] = utc_now()
    # Atomic per-file snapshots; plan and SQLite are durable independent artifacts.
    for filename, content in (("results.json", json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"),
                              ("report.html", render_study_report(report))):
        temporary = directory / (filename + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(directory / filename)


def run_study(plan_path: str | Path, output_dir: str | Path, *, api_key: str | None = None,
              provider_factory: Callable[[dict, Case], Provider] | None = None) -> dict:
    """Execute once into a new directory; never retry failed requests implicitly.

    Injecting a provider is reserved for offline integration tests; every such
    artifact is labelled offline_test regardless of the provider's name.
    """
    plan = load_plan(plan_path)
    if not plan["ready_to_execute"]:
        raise ValueError("Draft plan is not executable: " + "; ".join(plan["readiness_issues"]))
    cases = {raw["id"]: Case.from_dict(raw) for raw in plan["cases"]}
    specs = {(s["provider"], s["model"]): s for s in plan["models"]}

    def default_factory(spec: dict, case: Case) -> Provider:
        kwargs = {k: v for k, v in spec.items() if k not in {"provider", "model"}}
        return OpenAIProvider(spec["model"], api_key=api_key, **kwargs)

    factory = provider_factory or default_factory
    # Preflight constructors check credentials/settings without HTTP or disk writes.
    for spec in specs.values():
        provider = factory(spec, next(iter(cases.values())))
        if (provider.name, provider.model) != (spec["provider"], spec["model"]):
            raise ValueError("Provider identity differs from frozen plan")
        if provider_factory is None:
            for key in ("input_rate_per_million", "output_rate_per_million", "cached_input_rate_per_million"):
                if getattr(provider, key, None) != spec.get(key):
                    raise ValueError("Environment pricing differs from the frozen plan")
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    report = {"artifact_type": "harnesslab.study_results", "schema_version": 1, "plan": plan,
              "manifest": {"execution_id": uuid.uuid4().hex, "started_at": utc_now(), "status": "running",
                           "mode": "live" if provider_factory is None else "offline_test",
                           "harnesslab_version": __version__, "python": sys.version.split()[0],
                           "platform": platform.platform(), "trace_database": "traces.sqlite",
                           "stop_reason": None}, "runs": []}
    known_cost = 0.0
    store = Store(directory / "traces.sqlite")
    active: tuple[dict, str, float] | None = None
    try:
        _persist(directory, report)
        for slot in plan["schedule"]:
            if known_cost >= plan["max_total_cost_usd"]:
                report["manifest"].update(status="stopped", stop_reason="observed_total_cost_threshold")
                break
            spec = specs[(slot["provider"], slot["model"])]
            provider = factory(spec, cases[slot["case_id"]])
            remaining = plan["max_total_cost_usd"] - known_cost
            config = Config(**plan["configs"][slot["profile"]])
            config = replace(config, max_cost_usd=min(config.max_cost_usd or remaining, remaining))
            harness = Harness(store, provider)
            run_id = harness.start(cases[slot["case_id"]], config)
            active = (slot, run_id, time.perf_counter())
            state = harness.run(run_id)
            row = _run_record(store, active, state, plan["diagnosis_aliases"])
            report["runs"].append(row)
            active = None
            known_cost += row["reported_cost_usd"]
            _persist(directory, report)
            if row["usage_incomplete"] or row["estimated_cost_usd"] is None:
                report["manifest"].update(status="stopped", stop_reason="unknown_usage_or_cost")
                break
            # Credential/model/rate-limit failures halt the experiment, not N more paid attempts.
            if any(code.startswith("http_") or code == "transport_error" for code in row["provider_errors"]):
                report["manifest"].update(status="stopped", stop_reason="provider_infrastructure_error")
                break
        else:
            report["manifest"]["status"] = "completed"
    except (KeyboardInterrupt, Exception) as exc:
        # Never export arbitrary exception messages: third-party errors can echo secrets.
        report["manifest"].update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "error",
                                   stop_reason="operator_interrupt" if isinstance(exc, KeyboardInterrupt) else "runner_error")
        if active is not None:
            state = store.replay(active[1])
            if state["status"] == "running":
                if state["request_in_flight"]:
                    state.update(request_in_flight=False, usage_incomplete=True, estimated_cost_usd=None)
                state["status"] = "provider_error"
                store.save(state, "study_interrupted", {"reason": report["manifest"]["stop_reason"]})
            report["runs"].append(_run_record(store, active, state, plan["diagnosis_aliases"]))
    finally:
        store.close()
        report["manifest"]["finished_at"] = utc_now()
        _persist(directory, report)
    return report


def _run_record(store: Store, active: tuple, state: dict, aliases: dict) -> dict:
    slot, run_id, start = active
    effects, events = store.effects(run_id), store.events(run_id)
    no_requests = state["model_calls"] == 0 and not state["usage_incomplete"] and not state["request_in_flight"]
    return dict(slot, run_id=run_id, question=state["case"]["question"],
                status=state["status"], result=state.get("result"),
                grade=grade_study_run(state, effects, aliases),
                latency_ms=(time.perf_counter() - start) * 1000,
                effective_config=state["config"], provider_settings=state["provider_settings"],
                steps=state["steps"], model_calls=state["model_calls"],
                input_tokens=state["input_tokens"], output_tokens=state["output_tokens"],
                cached_input_tokens=state.get("cached_input_tokens"),
                cached_input_tokens_incomplete=state.get("cached_input_tokens_incomplete", True),
                cache_write_tokens=state.get("cache_write_tokens"),
                cost_incomplete=state.get("cost_incomplete", False),
                estimated_cost_usd=0.0 if no_requests else state["estimated_cost_usd"],
                reported_cost_usd=state["reported_cost_usd"],
                usage_incomplete=state["usage_incomplete"],
                provider_errors=[f["error"] for f in state["feedback"] if "error" in f],
                effects=effects, events=events, state_sha256=digest(state))
