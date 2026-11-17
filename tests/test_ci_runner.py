"""Synthetic suites test CI reporting; they do not deliver native OS signals."""

import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


def synthetic_case(
    test_id: str, outcome: str = "passed", *, executions: list | None = None,
) -> unittest.TestCase:
    class SyntheticCase(unittest.TestCase):
        def id(self) -> str:
            return test_id

        def runTest(self) -> None:
            if executions is not None:
                executions.append(test_id)
            print("synthetic fixture output")
            print("synthetic fixture stderr", file=sys.stderr)
            if outcome in ("failed", "expected_failure"):
                self.fail("synthetic failure")
            if outcome == "error":
                raise RuntimeError("synthetic execution error")
            if outcome == "skipped":
                self.skipTest("synthetic omission")
            if outcome == "interrupted":
                raise KeyboardInterrupt("synthetic interruption, no OS signal")
            if outcome == "subtest_failure":
                with self.subTest(label="synthetic"):
                    self.fail("synthetic subtest failure")

    if outcome in ("expected_failure", "unexpected_success"):
        SyntheticCase.runTest = unittest.expectedFailure(SyntheticCase.runTest)

    return SyntheticCase()


class CIRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = importlib.import_module("scripts.run_ci_tests")
        self.required = ("synthetic.native-a", "synthetic.native-b", "synthetic.native-c")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def run_fixture(self, suite, *, label="case", required=None, **options):
        output = self.directory / label
        stream = io.StringIO()
        code = self.runner.run_suite(
            suite, output, required_ids=self.required if required is None else required,
            stream=stream, **options,
        )
        report = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        log = (output / "tests.log").read_text(encoding="utf-8")
        return code, report, log, stream.getvalue()

    def test_success_reports_each_required_case_once_and_keeps_log(self) -> None:
        executions = []
        suite = unittest.TestSuite(synthetic_case(name, executions=executions) for name in self.required)
        exit_code, report, log, stream = self.run_fixture(suite)
        self.assertEqual(exit_code, 0)
        self.assertEqual(executions, list(self.required))
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["discovered_total"], 3)
        self.assertEqual(report["tests_run"], 3)
        self.assertEqual(report["gate_errors"], [])
        self.assertEqual(report["platform"], sys.platform)
        self.assertTrue(report["python_version"])
        for name in self.required:
            self.assertEqual(report["native_tests"][name],
                             {"discovered": 1, "runs": 1, "statuses": ["passed"]})
            self.assertIn(name, log)
        self.assertIn("synthetic fixture output", log)
        self.assertIn("synthetic fixture stderr", log)
        self.assertEqual(log, stream)

    def test_failures_errors_and_unexpected_successes_propagate(self) -> None:
        for outcome, counter in (
            ("failed", "failures"), ("error", "errors"),
            ("unexpected_success", "unexpected_successes"), ("subtest_failure", "failures"),
        ):
            with self.subTest(outcome=outcome):
                # All required cases pass, but a separate failing test must
                # still make the entire suite fail.
                suite = unittest.TestSuite([
                    *(synthetic_case(name) for name in self.required),
                    synthetic_case("synthetic.other", outcome),
                ])
                code, report, log, _ = self.run_fixture(suite, label=outcome)
                self.assertEqual(code, 1)
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report[counter], 1)
                self.assertEqual(report["tests_run"], 4)
                self.assertIn("FAILED", log)

    def test_required_native_skip_or_expected_failure_is_not_a_pass(self) -> None:
        for outcome in ("skipped", "expected_failure", "subtest_failure"):
            with self.subTest(outcome=outcome):
                name = self.required[0]
                suite = unittest.TestSuite([synthetic_case(name, outcome)])
                code, report, _, _ = self.run_fixture(suite, label=outcome, required=(name,))
                self.assertEqual(code, 1)
                self.assertEqual(report["status"], "failed")
                statuses = report["native_tests"][name]["statuses"]
                self.assertEqual(statuses, ["failed" if outcome == "subtest_failure" else outcome])
                self.assertTrue(report["gate_errors"])

    def test_missing_empty_duplicate_or_partial_suites_are_rejected(self) -> None:
        class PartialSuite(unittest.TestSuite):
            def run(self, result, debug=False):
                next(iter(self)).run(result)
                return result

        fixtures = {
            "missing": unittest.TestSuite([synthetic_case(self.required[0])]),
            "empty": unittest.TestSuite(),
            "duplicate": unittest.TestSuite([
                *(synthetic_case(name) for name in self.required), synthetic_case(self.required[0]),
            ]),
            "partial": PartialSuite(synthetic_case(name) for name in self.required),
        }
        for label, suite in fixtures.items():
            with self.subTest(label=label):
                code, report, _, _ = self.run_fixture(suite, label=label)
                self.assertEqual(code, 1)
                self.assertEqual(report["status"], "failed")
                self.assertTrue(report["gate_errors"])
                if label == "duplicate":
                    self.assertEqual(report["native_tests"][self.required[0]]["discovered"], 2)
                    self.assertEqual(report["native_tests"][self.required[0]]["runs"], 2)
                if label == "partial":
                    self.assertEqual(report["discovered_total"], 3)
                    self.assertEqual(report["tests_run"], 1)

    def test_interruption_or_unexpected_suite_exit_cannot_return_success(self) -> None:
        class BrokenSuite(unittest.TestSuite):
            def run(self, result, debug=False):
                raise SystemExit(0)

        fixtures = (
            ("interrupted", unittest.TestSuite([synthetic_case(self.required[0], "interrupted")]),
             "KeyboardInterrupt"),
            ("error", BrokenSuite([synthetic_case(self.required[0])]), "SystemExit"),
        )
        for expected, suite, error_type in fixtures:
            with self.subTest(expected=expected):
                code, report, log, _ = self.run_fixture(suite, label=expected, required=self.required[:1])
                self.assertEqual(code, 1)
                self.assertEqual(report["status"], expected)
                self.assertEqual(report["exception"]["type"], error_type)
                self.assertIn(error_type, log)
                self.assertTrue(report["gate_errors"])

    def test_cli_discovery_failure_or_interrupt_still_writes_reports(self) -> None:
        for fault, expected in ((RuntimeError("synthetic discovery failure"), "error"),
                                (KeyboardInterrupt("synthetic discovery interruption"), "interrupted")):
            with self.subTest(expected=expected):
                output = self.directory / expected
                with (
                    patch.object(self.runner.unittest.defaultTestLoader, "discover", side_effect=fault) as loader,
                    patch.dict("os.environ", {}, clear=True),
                    patch("sys.stdout", new_callable=io.StringIO),
                ):
                    code = self.runner.main(["--output", str(output)])
                report = json.loads((output / "summary.json").read_text(encoding="utf-8"))
                self.assertEqual(code, 1)
                self.assertEqual(report["status"], expected)
                self.assertEqual(report["tests_run"], 0)
                self.assertEqual(report["discovered_total"], 0)
                self.assertTrue((output / "tests.log").is_file())
                loader.assert_called_once_with(str(self.runner.ROOT / "tests"))

    def test_step_summary_is_explicit_and_fixture_runs_ignore_actions_environment(self) -> None:
        github_summary = self.directory / "github-summary.md"
        github_summary.write_text("Existing content\n", encoding="utf-8")
        with patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": str(github_summary)}):
            self.run_fixture(unittest.TestSuite(synthetic_case(name) for name in self.required))
        self.assertEqual(github_summary.read_text(encoding="utf-8"), "Existing content\n")
        code, _, _, _ = self.run_fixture(
            unittest.TestSuite(synthetic_case(name) for name in self.required),
            label="explicit", step_summary_path=github_summary,
        )
        self.assertEqual(code, 0)
        text = github_summary.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("Existing content\n"))
        self.assertIn("Status: **passed**", text)
        for name in self.required:
            self.assertIn(name, text)

    def test_step_summary_write_error_is_not_silent_success(self) -> None:
        code, report, log, _ = self.run_fixture(
            unittest.TestSuite(synthetic_case(name) for name in self.required),
            step_summary_path=self.directory,  # A directory cannot be opened as a report file.
        )
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "error")
        self.assertIn("Could not write the requested step summary", report["gate_errors"])
        self.assertIsNotNone(report["exception"])
        self.assertIn("Traceback", log)


if __name__ == "__main__":
    unittest.main()
