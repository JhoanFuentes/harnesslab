"""Run offline tests once and require explicit native-interrupt test evidence.

The CI entry point discovers the repository's complete unittest suite. Reports
are written on success, failure, and interrupted/incomplete discovery or runs.
Synthetic unit tests may inject required IDs; that does not validate OS signals.
"""

import argparse
from collections import Counter, defaultdict
from contextlib import redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import platform
import sys
import traceback
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REQUIRED_NATIVE_TEST_IDS = (
    "test_release_real_ctrl_c.RealCtrlCTests.test_native_ctrl_c_after_commit_is_confirmed_without_republishing",
    "test_release_real_ctrl_c.RealCtrlCTests.test_native_ctrl_c_during_backoff_leaves_pending_work_for_a_fresh_process",
    "test_release_real_ctrl_c.RealCtrlCTests.test_uninterrupted_child_publishes_once",
)


class _Tee:
    def __init__(self, stream, log) -> None:
        self.stream, self.log = stream, log

    def write(self, text: str) -> None:
        self.stream.write(text)
        self.log.write(text)

    def flush(self) -> None:
        self.stream.flush()
        self.log.flush()


class _TrackingResult(unittest.TextTestResult):
    def __init__(self, *args) -> None:
        super().__init__(*args)
        self.statuses = defaultdict(list)

    def startTest(self, test) -> None:
        self.statuses[test.id()].append("started")
        super().startTest(test)

    def _record(self, test, status: str) -> None:
        # Fixture-level errors/skips may not have a corresponding startTest.
        if self.statuses.get(test.id()):
            self.statuses[test.id()][-1] = status

    def addSuccess(self, test) -> None:
        self._record(test, "passed")
        super().addSuccess(test)

    def addFailure(self, test, err) -> None:
        self._record(test, "failed")
        super().addFailure(test, err)

    def addError(self, test, err) -> None:
        self._record(test, "error")
        super().addError(test, err)

    def addSkip(self, test, reason) -> None:
        self._record(test, "skipped")
        super().addSkip(test, reason)

    def addExpectedFailure(self, test, err) -> None:
        self._record(test, "expected_failure")
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test) -> None:
        self._record(test, "unexpected_success")
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test, subtest, err) -> None:
        if err is not None:
            self._record(test, "failed" if issubclass(err[0], test.failureException) else "error")
        super().addSubTest(test, subtest, err)


def _test_ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _test_ids(item)
        else:
            yield item.id()


def _append_step_summary(path: str | Path, report: dict) -> None:
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(
            "\n## Offline test verification\n\n"
            f"Status: **{report['status']}**; Python {report['python_version']} on {report['platform']}.\n\n"
            f"Discovered: {report['discovered_total']}; run: {report['tests_run']}; "
            f"failures: {report['failures']}; errors: {report['errors']}; skipped: {report['skipped']}.\n\n"
        )
        for name, evidence in report["native_tests"].items():
            handle.write(f"- `{name}`: discovered={evidence['discovered']}, "
                         f"runs={evidence['runs']}, statuses={evidence['statuses']}\n")
        for reason in report["gate_errors"]:
            handle.write(f"- Gate failure: {reason}\n")


def _run_suite(load_suite, output, *, required_ids, stream, step_summary_path) -> int:
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    required_ids = tuple(required_ids)
    result = None
    discovered = Counter()
    interruption = None
    status = "failed"

    def make_result(*args):
        nonlocal result
        result = _TrackingResult(*args)
        return result

    with (output / "tests.log").open("w", encoding="utf-8") as log:
        tee = _Tee(sys.stdout if stream is None else stream, log)
        with redirect_stdout(tee), redirect_stderr(tee):
            try:
                suite = load_suite()
                discovered.update(_test_ids(suite))
                unittest.TextTestRunner(stream=tee, verbosity=2, resultclass=make_result).run(suite)
            except BaseException as error:
                # A SystemExit(0) or KeyboardInterrupt must never create green CI.
                status = "interrupted" if isinstance(error, KeyboardInterrupt) else "error"
                interruption = {"type": type(error).__name__, "message": str(error)}
                traceback.print_exc(file=tee)

        statuses = result.statuses if result is not None else {}
        native = {
            name: {"discovered": discovered[name], "runs": len(statuses.get(name, [])),
                   "statuses": statuses.get(name, [])}
            for name in required_ids
        }
        total = sum(discovered.values())
        tests_run = result.testsRun if result is not None else 0
        gates = []
        if total == 0:
            gates.append("No tests were discovered")
        if tests_run != total:
            gates.append(f"Incomplete suite: discovered {total}, ran {tests_run}")
        for name, evidence in native.items():
            if evidence != {"discovered": 1, "runs": 1, "statuses": ["passed"]}:
                gates.append(f"Required native test must be discovered and pass exactly once: {name}")
        if interruption is not None:
            gates.append("Discovery or execution did not complete normally")
        elif result is not None and result.wasSuccessful() and not gates:
            status = "passed"
        report = {
            "schema_version": 1, "status": status,
            "discovered_total": total, "tests_run": tests_run,
            "failures": len(result.failures) if result is not None else 0,
            "errors": len(result.errors) if result is not None else 0,
            "skipped": len(result.skipped) if result is not None else 0,
            "expected_failures": len(result.expectedFailures) if result is not None else 0,
            "unexpected_successes": len(result.unexpectedSuccesses) if result is not None else 0,
            "platform": sys.platform, "python_version": platform.python_version(),
            "native_tests": native, "gate_errors": gates, "exception": interruption,
        }
        if step_summary_path is not None:
            try:
                _append_step_summary(step_summary_path, report)
            except OSError as error:
                report["status"] = "error"
                report["gate_errors"].append("Could not write the requested step summary")
                report["exception"] = {"type": type(error).__name__, "message": str(error)}
                traceback.print_exc(file=tee)
        (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        tee.write(f"\nCI verification: {report['status']}\n")
        for name, evidence in native.items():
            tee.write(f"- Native {name}: discovered={evidence['discovered']}, "
                      f"runs={evidence['runs']}, statuses={evidence['statuses']}\n")
        for reason in report["gate_errors"]:
            tee.write(f"- {reason}\n")
        tee.flush()
    return 0 if report["status"] == "passed" else 1


def run_suite(
    suite: unittest.TestSuite, output: str | Path, *, required_ids=REQUIRED_NATIVE_TEST_IDS,
    stream=None, step_summary_path: str | Path | None = None,
) -> int:
    """Run an already built suite once; no environment-driven side effects.

    required_ids is injectable for synthetic runner tests. CI uses the fixed
    native IDs above. Only main reads GITHUB_STEP_SUMMARY and discovers tests.
    """
    return _run_suite(lambda: suite, output, required_ids=required_ids,
                      stream=stream, step_summary_path=step_summary_path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "runs" / "ci-tests")
    args = parser.parse_args(argv)
    return _run_suite(
        lambda: unittest.defaultTestLoader.discover(str(ROOT / "tests")), args.output,
        required_ids=REQUIRED_NATIVE_TEST_IDS, stream=None,
        step_summary_path=os.environ.get("GITHUB_STEP_SUMMARY"),
    )


if __name__ == "__main__":
    raise SystemExit(main())
