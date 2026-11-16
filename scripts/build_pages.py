"""Publish only reviewed synthetic evidence and the versioned study preview.

No runs, credentials, arbitrary directories or newly generated model results are
copied. The historical report remains at the same public URL.
"""

from pathlib import Path
import argparse
import json
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harnesslab import __version__  # noqa: E402 - runnable from a source checkout
from harnesslab.study import load_plan  # noqa: E402
from harnesslab.study_report import render_plan  # noqa: E402
from verify_example import main as verify_example  # noqa: E402


def build(output: Path) -> list[str]:
    verify_example()
    preview = ROOT / "examples" / "planned-study" / f"v{__version__}"
    plan = load_plan(preview / "plan.json")
    if plan["ready_to_execute"]:
        raise ValueError("Only the reviewed unexecuted draft belongs in the public planning preview")
    if (preview / "plan.html").read_text(encoding="utf-8") != render_plan(plan):
        raise ValueError("Planning HTML differs from the frozen plan")
    report_dir = ROOT / "examples" / "demo"
    manifest = json.loads((report_dir / "results.json").read_text(encoding="utf-8"))["manifest"]
    trace = report_dir / manifest["trace_database"]
    if trace.resolve().parent != report_dir.resolve():
        raise ValueError("Invalid historical trace path")
    files = {
        "index.html": report_dir / "report.html",
        "results.json": report_dir / "results.json",
        trace.name: trace,
        "study/index.html": preview / "plan.html",
        "study/plan.json": preview / "plan.json",
    }
    output.mkdir(parents=True, exist_ok=False)
    for target, source in files.items():
        destination = output / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    return sorted(files)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "_site")
    args = parser.parse_args()
    print(json.dumps({"published_files": build(args.output)}, indent=2))
