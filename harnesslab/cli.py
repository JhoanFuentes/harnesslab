"""Portable CLI: no server, dependencies, or credentials needed for the demo."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

from .cases import get_case, load_cases
from .engine import Harness, InjectedCrash
from .models import Case, Config
from .providers import OpenAIProvider, ScriptedProvider
from .store import Store


PROFILES = ["baseline", "hardened", "no-evidence-gate", "no-retries"]


def output(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def summary(state: dict) -> dict:
    keys = ("run_id", "status", "provider", "model", "steps", "model_calls", "input_tokens", "output_tokens",
            "estimated_cost_usd", "reported_cost_usd", "usage_incomplete", "approval_id", "result")
    return {key: state.get(key) for key in keys}


def provider_for(case: Case, name: str, model: str | None = None, settings: dict | None = None):
    if name == "scripted":
        return ScriptedProvider(case.scripted_actions)
    settings = settings or {}
    kwargs = {k: v for k, v in settings.items() if k != "prompt_hash" and v is not None}
    return OpenAIProvider(model, **kwargs)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harnesslab", description="Durable AI agent harness and behavioral evaluation lab")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("cases", help="List the public synthetic scenarios")
    export = sub.add_parser("export-cases", help="Export editable synthetic dataset, including evaluator labels")
    export.add_argument("--output", type=Path, default=Path("cases.json"))
    for name in ("demo", "eval"):
        e = sub.add_parser(name, help="Run deterministic comparison" if name == "demo" else "Run offline or opt-in live evaluation")
        e.add_argument("--output", type=Path, default=Path("runs/demo" if name == "demo" else "runs/eval"))
        e.add_argument("--profiles", nargs="+", choices=PROFILES, default=PROFILES if name == "demo" else ["hardened"])
        if name == "eval":
            e.add_argument("--provider", choices=["scripted", "openai"], default="scripted")
            e.add_argument("--model")
            e.add_argument("--trials", type=int, default=1)
            e.add_argument("--cases-file", type=Path)
    run = sub.add_parser("run", help="Start one incident session")
    run.add_argument("--case", default="pool-saturation")
    run.add_argument("--provider", choices=["scripted", "openai"], default="scripted")
    run.add_argument("--model")
    run.add_argument("--profile", choices=PROFILES, default="hardened")
    run.add_argument("--max-steps", type=int)
    run.add_argument("--max-model-calls", type=int)
    run.add_argument("--max-tokens", type=int)
    run.add_argument("--max-cost-usd", type=float)
    run.add_argument("--crash-after-tool", action="store_true", help="Inject crash between durable receipt and observation")
    for name in ("resume", "approve", "inspect", "replay"):
        cmd = sub.add_parser(name)
        cmd.add_argument("run_id")
        if name == "resume":
            cmd.add_argument("--retry-provider", action="store_true", help="Explicitly retry a failed request; usage may be unknown")
        if name == "approve":
            cmd.add_argument("--approval-id", required=True, help="Exact pending action approval identifier")
        if name == "inspect":
            cmd.add_argument("--events", action="store_true")
        cmd.add_argument("--db", type=Path, default=Path("runs/sessions.sqlite"))
    run.add_argument("--db", type=Path, default=Path("runs/sessions.sqlite"))
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "cases":
            output([{"id": c.id, "question": c.question, "tags": c.tags} for c in load_cases()])
            return 0
        if args.command == "export-cases":
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps([c.to_dict() for c in load_cases()], indent=2) + "\n", encoding="utf-8")
            output({"dataset": str(args.output)})
            return 0
        if args.command in {"demo", "eval"}:
            from .evaluation import evaluate
            cases = load_cases()
            if getattr(args, "cases_file", None):
                cases = [Case.from_dict(c) for c in json.loads(args.cases_file.read_text(encoding="utf-8"))]
            provider = getattr(args, "provider", "scripted")
            factory = None if provider == "scripted" else lambda c: provider_for(c, provider, args.model)
            report = evaluate(args.output, profiles=args.profiles, provider_factory=factory,
                              trials=getattr(args, "trials", 1), cases=cases)
            output({"report": str(args.output / "report.html"), "results": str(args.output / "results.json"),
                    "summary": report["summaries"]})
            return 0
        store = Store(args.db)
        try:
            if args.command == "run":
                case = get_case(args.case)
                provider = provider_for(case, args.provider, args.model)
                cfg = Config.named(args.profile)
                overrides = {key: getattr(args, key) for key in ("max_steps", "max_model_calls", "max_tokens", "max_cost_usd")
                             if getattr(args, key) is not None}
                cfg = replace(cfg, **overrides)
                harness = Harness(store, provider)
                run_id = harness.start(case, cfg)
                try:
                    state = harness.run(run_id, crash_after_tool=args.crash_after_tool)
                except InjectedCrash:
                    output({"run_id": run_id, "status": "injected_crash", "db": str(args.db),
                            "next": f"python -m harnesslab resume {run_id} --db {args.db}"})
                    return 3
            else:
                run_id = args.run_id
                state = store.replay(run_id)
                if args.command == "inspect":
                    output({"state": summary(state), "effects": store.effects(run_id),
                            "events": store.events(run_id) if args.events else len(store.events(run_id))})
                    return 0
                if args.command == "replay":
                    output({"verified": True, "event_count": len(store.events(run_id)), "state": summary(state)})
                    return 0
                provider = provider_for(Case.from_dict(state["case"]), state["provider"], state["model"], state["provider_settings"])
                harness = Harness(store, provider)
                if args.command == "approve":
                    harness.approve(run_id, args.approval_id)
                elif args.retry_provider:
                    harness.retry_provider(run_id)
                state = harness.run(run_id)
            output(summary(state))
            return 0 if state["status"] in {"completed", "insufficient_evidence", "waiting_approval"} else 2
        finally:
            store.close()
    except (ValueError, OSError) as exc:
        print(f"harnesslab: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
