"""Local SQLite checkpoints and real Python restarts; no network or inference."""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.release_persistence import SQLiteFakeGitHub, StateError
from scripts.release_simulation import publish_verified
from release_restart_worker import inputs, summary


ROOT = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).with_name("release_restart_worker.py")


class ReleasePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state_path = Path(directory.name) / "fake-github.sqlite"
        self.identity, self.package, self.verification = inputs()

    def worker(self, phase: str, expected_code: int) -> dict:
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("OPENAI_", "GH_", "GITHUB_"))
            and key not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"}
        }
        completed = subprocess.run(
            [sys.executable, "-I", str(WORKER), phase, str(self.state_path)],
            cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
            timeout=30, check=False,
        )
        self.assertEqual(completed.returncode, expected_code, completed.stderr)
        return json.loads(completed.stdout)

    def test_new_python_process_confirms_publication_without_repeating_it(self) -> None:
        first = self.worker("crash", expected_code=23)
        self.assertEqual(first["phase"], "interrupted")
        self.assertNotEqual(first["pid"], os.getpid())

        # The first process has exited. Its globals/objects cannot supply state.
        restored = SQLiteFakeGitHub(self.state_path)
        saved = summary(restored, self.identity)
        self.assertEqual(saved, {
            "release_count": 1,
            "draft": False,
            "assets": [self.identity.asset_name],
            "asset_sha256": self.identity.sha256,
            "upload_attempts": 1,
            "publish_attempts": 1,
            "mutations": [["create_draft", self.identity.tag],
                          ["upload_asset", self.identity.tag],
                          ["publish", self.identity.tag]],
        })

        second = self.worker("recover", expected_code=0)
        self.assertEqual(second["phase"], "confirmed")
        self.assertNotEqual(second["pid"], os.getpid())
        self.assertEqual(second["before"], saved)
        self.assertEqual(second["after"], saved)
        self.assertEqual(summary(SQLiteFakeGitHub(self.state_path), self.identity), saved)

    def assert_one_publication(self, state: dict, attempts: int) -> None:
        self.assertEqual(state["release_count"], 1)
        self.assertFalse(state["draft"])
        self.assertEqual(state["assets"], [self.identity.asset_name])
        self.assertEqual(state["asset_sha256"], self.identity.sha256)
        self.assertEqual(state["upload_attempts"], 1)
        self.assertEqual(state["publish_attempts"], attempts)
        self.assertEqual(state["mutations"], [
            ["create_draft", self.identity.tag],
            ["upload_asset", self.identity.tag],
            ["publish", self.identity.tag],
        ])

    def test_crash_before_publication_commit_recovers_pending_effect(self) -> None:
        first = self.worker("crash-before-commit", expected_code=31)
        self.assertEqual(first["boundary"], "before_commit")

        # Exercise recovery of a hot journal, not just reloading Python objects.
        # A read-only connection cannot perform the required rollback itself.
        with self.assertRaises(sqlite3.OperationalError) as failure:
            with closing(sqlite3.connect(self.state_path.as_uri() + "?mode=ro", uri=True)) as connection:
                connection.execute("SELECT payload FROM fake_state").fetchone()
        self.assertEqual(failure.exception.sqlite_errorcode, sqlite3.SQLITE_READONLY_ROLLBACK)

        saved = summary(SQLiteFakeGitHub(self.state_path), self.identity)
        self.assertTrue(saved["draft"])
        self.assertEqual(saved["publish_attempts"], 1)
        self.assertEqual(saved["mutations"], [
            ["create_draft", self.identity.tag], ["upload_asset", self.identity.tag],
        ])

        second = self.worker("recover", expected_code=0)
        self.assertEqual(second["before"], saved)
        self.assert_one_publication(second["after"], attempts=2)
        self.assertEqual(summary(SQLiteFakeGitHub(self.state_path), self.identity), second["after"])

    def test_crash_after_publication_commit_does_not_repeat_effect(self) -> None:
        first = self.worker("crash-after-commit", expected_code=32)
        self.assertEqual(first["boundary"], "after_commit")
        saved = summary(SQLiteFakeGitHub(self.state_path), self.identity)
        self.assert_one_publication(saved, attempts=1)

        second = self.worker("recover", expected_code=0)
        self.assertEqual(second["before"], saved)
        self.assertEqual(second["after"], saved)
        self.assertEqual(summary(SQLiteFakeGitHub(self.state_path), self.identity), saved)

    def test_boundary_errors_require_reload_even_when_commit_succeeded(self) -> None:
        for boundary in ("before_commit", "after_commit"):
            with self.subTest(boundary=boundary):
                tag = self.identity.tag

                class FailedBoundary(SQLiteFakeGitHub):
                    def _checkpoint_boundary(self, point: str) -> None:
                        if point == boundary and self.mutations[-1:] == [("publish", tag)]:
                            raise StateError("Injected checkpoint boundary failure")

                path = self.state_path.with_name(boundary + ".sqlite")
                github = FailedBoundary.create(path, tags={tag: self.identity.source_commit})
                with self.assertRaises(StateError):
                    publish_verified(github, self.identity, self.package, self.verification)
                with self.assertRaises(StateError):
                    github.get_release(tag)

                restored = SQLiteFakeGitHub(path)
                saved = summary(restored, self.identity)
                self.assertEqual(saved["draft"], boundary == "before_commit")
                self.assertEqual(saved["publish_attempts"], 1)
                self.assertEqual(saved["mutations"].count(["publish", tag]),
                                 0 if boundary == "before_commit" else 1)
                publish_verified(restored, self.identity, self.package, self.verification)
                self.assert_one_publication(summary(SQLiteFakeGitHub(path), self.identity),
                                            attempts=2 if boundary == "before_commit" else 1)

    def test_create_does_not_overwrite_existing_state(self) -> None:
        SQLiteFakeGitHub.create(self.state_path, tags={self.identity.tag: self.identity.source_commit})
        before = self.state_path.read_bytes()
        with self.assertRaises(FileExistsError):
            SQLiteFakeGitHub.create(self.state_path, tags={})
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_missing_state_is_not_created_on_recovery(self) -> None:
        with self.assertRaises(StateError):
            SQLiteFakeGitHub(self.state_path)
        self.assertFalse(self.state_path.exists())

    def test_corrupt_state_is_not_replaced_with_an_empty_server(self) -> None:
        damaged = b"This is not a SQLite database."
        self.state_path.write_bytes(damaged)
        with self.assertRaises(StateError):
            SQLiteFakeGitHub(self.state_path)
        self.assertEqual(self.state_path.read_bytes(), damaged)

    def test_unknown_snapshot_version_is_not_reset(self) -> None:
        SQLiteFakeGitHub.create(self.state_path, tags={self.identity.tag: self.identity.source_commit})
        with closing(sqlite3.connect(self.state_path)) as connection:
            with connection:
                payload = json.loads(connection.execute("SELECT payload FROM fake_state WHERE id = 1").fetchone()[0])
                payload["schema_version"] = 999
                connection.execute("UPDATE fake_state SET payload = ? WHERE id = 1", (json.dumps(payload),))
        before = self.state_path.read_bytes()

        with self.assertRaises(StateError):
            SQLiteFakeGitHub(self.state_path)

        self.assertEqual(self.state_path.read_bytes(), before)

    def test_failed_checkpoint_requires_reopening_before_further_use(self) -> None:
        github = SQLiteFakeGitHub.create(self.state_path, tags={self.identity.tag: self.identity.source_commit})
        connect = sqlite3.connect

        class FailedCommit(sqlite3.Connection):
            def commit(self) -> None:
                # UPDATE has run; the uncommitted transaction must be rolled back.
                raise sqlite3.OperationalError("Injected commit failure")

        with patch("scripts.release_persistence.sqlite3.connect", side_effect=lambda *args, **kwargs:
                   connect(*args, factory=FailedCommit, **kwargs)):
            with self.assertRaises(StateError):
                github.create_draft(self.identity)

        # Memory may be ahead of disk, so do not expose it as a confirmed result.
        with self.assertRaises(StateError):
            github.get_release(self.identity.tag)
        restored = SQLiteFakeGitHub(self.state_path)
        self.assertEqual(restored.releases, {})
        self.assertEqual(restored.mutations, [])


if __name__ == "__main__":
    unittest.main()
