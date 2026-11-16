"""Verify the committed synthetic artifact before sharing or deploying it."""

from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harnesslab.cases import load_cases
from harnesslab.evaluation import grade_run, summarize
from harnesslab.models import digest
from harnesslab.store import Store


def main() -> None:
    directory = ROOT / "examples" / "demo"
    report = json.loads((directory / "results.json").read_text(encoding="utf-8"))
    manifest = report["manifest"]
    assert manifest["mode"] == "scripted", "Only the reviewed synthetic report belongs in this example"
    source_hash = digest({p.name: p.read_text(encoding="utf-8") for p in sorted((ROOT / "harnesslab").glob("*.py"))})
    assert source_hash == manifest["runtime_source_sha256"], "Regenerate the example after changing runtime source"
    assert digest([c.to_dict() for c in load_cases()]) == manifest["dataset_sha256"], "Dataset differs from example"
    assert summarize(report["runs"]) == report["summaries"], "Summary differs from individual runs"
    trace_path = (directory / manifest["trace_database"]).resolve()
    assert trace_path.parent == directory.resolve() and trace_path.is_file(), "Missing or invalid example trace path"
    store = Store(trace_path)
    try:
        for run in report["runs"]:
            state = store.replay(run["run_id"])
            assert digest(state) == run["state_sha256"], "Trace state does not match report"
            assert grade_run(state, store.effects(run["run_id"])) == run["grade"], "Grade does not match durable state"
    finally:
        store.close()
    print(f"Verified source, dataset, summaries, grades and audit chains for {len(report['runs'])} runs.")


if __name__ == "__main__":
    main()
