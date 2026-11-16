"""User-facing commands exercised in separate Python processes, without API keys."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)
        self.db = self.output / "sessions.sqlite"
        # Neither a developer's API credentials nor import-path overrides may
        # affect these offline tests. Use the interpreter running the test suite.
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith("OPENAI_") and key not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"}
        }
        self.env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")

    def command(self, *args: str, expected_code: int = 0) -> dict | list | None:
        completed = subprocess.run(
            [sys.executable, "-m", "harnesslab", *map(str, args)],
            cwd=ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        self.assertEqual(
            completed.returncode, expected_code,
            f"Command {args!r}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        if expected_code == 2:
            self.assertIn("harnesslab:", completed.stderr)
        return json.loads(completed.stdout) if completed.stdout.strip() else None

    def test_demo_emits_actual_summary_and_artifacts(self) -> None:
        directory = self.output / "demo"
        output = self.command("demo", "--output", str(directory))
        self.assertIsInstance(output["summary"], list)
        saved = json.loads((directory / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(output["summary"], saved["summaries"])
        self.assertEqual(saved["manifest"]["mode"], "scripted")
        self.assertEqual(len(saved["summaries"]), 4)
        self.assertTrue((directory / "report.html").is_file())
        self.assertTrue((directory / saved["manifest"]["trace_database"]).is_file())
        self.assertTrue(all(run["provider"] == "scripted" for run in saved["runs"]))

    def test_run_inspect_and_replay_work_without_environment_credentials(self) -> None:
        state = self.command("run", "--case", "pool-saturation", "--db", str(self.db))
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["result"]["diagnosis"], "db_pool_exhaustion")
        self.assertEqual(set(state["result"]["evidence"]), {"pool-metrics", "pool-runbook"})
        inspected = self.command("inspect", state["run_id"], "--db", str(self.db))
        self.assertEqual(inspected["effects"], [])
        self.assertEqual(inspected["state"], state)
        replayed = self.command("replay", state["run_id"], "--db", str(self.db))
        self.assertTrue(replayed["verified"])
        self.assertEqual(replayed["state"], state)
        self.assertEqual(replayed["event_count"], inspected["events"])

    def test_committed_crash_resumes_in_fresh_process_without_repeating_work(self) -> None:
        crashed = self.command("run", "--crash-after-tool", "--db", str(self.db), expected_code=3)
        self.assertEqual(crashed["status"], "injected_crash")
        run_id = crashed["run_id"]
        resumed = self.command("resume", run_id, "--db", str(self.db))
        self.assertEqual(resumed["status"], "completed")
        # The fixture makes four decisions: search, two reads, then conclude.
        self.assertEqual(resumed["model_calls"], 4)
        inspected = self.command("inspect", run_id, "--events", "--db", str(self.db))
        self.assertEqual(sum(e["type"] == "tool_committed" for e in inspected["events"]), 3)
        replayed = self.command("replay", run_id, "--db", str(self.db))
        self.assertTrue(replayed["verified"])
        again = self.command("resume", run_id, "--db", str(self.db))
        self.assertEqual(again, resumed)
        self.assertEqual(
            self.command("inspect", run_id, "--db", str(self.db))["events"],
            len(inspected["events"]),
        )

    def test_approval_requires_exact_id_and_effect_happens_only_once(self) -> None:
        waiting = self.command("run", "--case", "injected-restart", "--db", str(self.db))
        self.assertEqual(waiting["status"], "waiting_approval")
        self.assertTrue(waiting["approval_id"])
        run_id = waiting["run_id"]
        self.assertEqual(self.command("inspect", run_id, "--db", str(self.db))["effects"], [])
        self.command("approve", run_id, "--approval-id", "wrong-id", "--db", str(self.db), expected_code=2)
        self.assertEqual(self.command("inspect", run_id, "--db", str(self.db))["effects"], [])
        completed = self.command("approve", run_id, "--approval-id", waiting["approval_id"], "--db", str(self.db))
        self.assertEqual(completed["status"], "completed")
        self.command("resume", run_id, "--db", str(self.db))
        self.command("approve", run_id, "--approval-id", waiting["approval_id"], "--db", str(self.db), expected_code=2)
        effects = self.command("inspect", run_id, "--db", str(self.db))["effects"]
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0]["service"], "checkout")
        self.assertTrue(effects[0]["approved"])
        self.assertTrue(effects[0]["simulation"])

    def test_unknown_run_returns_actionable_exit_two(self) -> None:
        for command in ("inspect", "resume", "replay"):
            with self.subTest(command=command):
                self.command(command, "unknown-run", "--db", str(self.db), expected_code=2)


if __name__ == "__main__":
    unittest.main()
