"""Deterministic stale-snapshot races between real, offline Python processes."""

from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from scripts.release_concurrency import publish_with_retry
from scripts.release_persistence import SQLiteFakeGitHub, StaleState, StateError
from scripts.release_simulation import VerificationFailed, publish_verified
from release_restart_worker import inputs, summary


ROOT = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).with_name("release_concurrency_worker.py")


class ReleaseConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state_path = Path(directory.name) / "concurrent-fake.sqlite"
        self.identity, self.package, self.verification = inputs()
        self.github = SQLiteFakeGitHub.create(
            self.state_path, tags={self.identity.tag: self.identity.source_commit},
        )
        # The parent may start the test workers; neither parent nor child needs networking.
        for target in ("socket.socket.connect", "socket.socket.connect_ex"):
            blocker = patch(target, side_effect=AssertionError("Offline concurrency test"))
            blocker.start()
            self.addCleanup(blocker.stop)

    def prepare_draft(self) -> None:
        self.github.create_draft(self.identity)
        self.github.upload_asset(self.identity.tag, self.identity.asset_name, self.package)

    def start_worker(self) -> subprocess.Popen:
        env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("OPENAI_", "GH_", "GITHUB_"))
            and key not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"}
        }
        process = subprocess.Popen(
            [sys.executable, "-I", str(WORKER), str(self.state_path)],
            cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
        )

        def cleanup() -> None:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=10)
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()

        self.addCleanup(cleanup)
        return process

    def read_ready(self, process: subprocess.Popen) -> dict:
        messages = queue.Queue()

        def read_one() -> None:
            try:
                messages.put(process.stdout.readline())
            except Exception as error:
                messages.put(error)

        reader = threading.Thread(target=read_one, daemon=True)
        reader.start()
        try:
            line = messages.get(timeout=20)
        except queue.Empty:
            self.fail("Worker did not announce its loaded snapshot within 20 seconds")
        reader.join(timeout=1)
        if isinstance(line, Exception):
            raise line
        self.assertTrue(line, "Worker exited before announcing its snapshot")
        event = json.loads(line)
        self.assertEqual(event["event"], "ready")
        return event

    def finish_worker(self, process: subprocess.Popen) -> tuple[list[dict], dict]:
        output, errors = process.communicate("GO\n", timeout=30)
        self.assertEqual(process.returncode, 0, errors)
        events = [json.loads(line) for line in output.splitlines() if line.strip()]
        done = [event for event in events if event["event"] == "done"]
        self.assertEqual(len(done), 1, events)
        return events, done[0]

    def test_two_loaded_processes_commit_only_one_publication(self) -> None:
        self.prepare_draft()
        first, second = self.start_worker(), self.start_worker()
        ready_a, ready_b = self.read_ready(first), self.read_ready(second)
        self.assertEqual(ready_a["snapshot_sha256"], ready_b["snapshot_sha256"])
        self.assertNotEqual(ready_a["pid"], ready_b["pid"])

        # Both have loaded the same draft. A finishes while B retains stale state.
        # This controls the race without timing sleeps or barriers inside a write lock.
        events_a, result_a = self.finish_worker(first)
        events_b, result_b = self.finish_worker(second)

        # A final snapshot alone could hide B overwriting A's publication history.
        # Independent post-commit witnesses must also show exactly one effect.
        committed = [event for event in events_a + events_b if event["event"] == "publication_committed"]
        self.assertEqual(sum(event["count"] for event in committed), 1)
        self.assertEqual(result_a["committed_publications"], 1)
        self.assertEqual(result_b["committed_publications"], 0)
        self.assertEqual((result_a["rounds"], result_a["conflicts"]), (1, 0))
        self.assertEqual((result_b["rounds"], result_b["conflicts"]), (2, 1))
        self.assertEqual(result_a["publish_calls"], 1)
        self.assertEqual(result_b["publish_calls"], 1)

        state = summary(SQLiteFakeGitHub(self.state_path), self.identity)
        self.assertEqual(state, result_a["state"])
        self.assertEqual(state, result_b["state"])
        self.assertFalse(state["draft"])
        self.assertEqual(state["release_count"], 1)
        self.assertEqual(state["assets"], [self.identity.asset_name])
        self.assertEqual(state["asset_sha256"], self.identity.sha256)
        self.assertEqual(state["upload_attempts"], 1)
        # B entered publish(), but its stale counter update never committed.
        self.assertEqual(state["publish_attempts"], 1)
        self.assertEqual(state["mutations"], [
            ["create_draft", self.identity.tag],
            ["upload_asset", self.identity.tag],
            ["publish", self.identity.tag],
        ])

    def test_stale_writer_is_invalidated_without_overwriting_new_state(self) -> None:
        stale = SQLiteFakeGitHub(self.state_path)
        self.prepare_draft()
        expected = self.github._expected_payload

        with self.assertRaises(StaleState):
            stale.create_draft(self.identity)
        with self.assertRaises(StateError):
            stale.get_release(self.identity.tag)

        self.assertEqual(SQLiteFakeGitHub(self.state_path)._expected_payload, expected)

    def test_read_only_confirmation_rejects_a_stale_snapshot(self) -> None:
        stale = SQLiteFakeGitHub(self.state_path)
        self.prepare_draft()

        with self.assertRaises(StaleState):
            stale.assert_current()
        with self.assertRaises(StateError):
            stale.resolve_tag(self.identity.tag)

    def test_retry_budget_stops_repeated_snapshot_conflicts(self) -> None:
        self.prepare_draft()
        self.github.publish(self.identity.tag)
        before = self.github._expected_payload

        class AlwaysStale(SQLiteFakeGitHub):
            opened = 0

            def __init__(self, path: str | Path) -> None:
                super().__init__(path)
                type(self).opened += 1

            def assert_current(self) -> None:
                self._available = False
                raise StaleState("Injected snapshot change")

        with self.assertRaisesRegex(StaleState, "limit"):
            publish_with_retry(
                self.state_path, self.identity, self.package, self.verification,
                max_rounds=3, client_factory=AlwaysStale,
            )

        self.assertEqual(AlwaysStale.opened, 3)
        self.assertEqual(SQLiteFakeGitHub(self.state_path)._expected_payload, before)

    def test_failed_verification_is_not_retried(self) -> None:
        self.prepare_draft()
        before = self.github._expected_payload
        failed = replace(self.verification, checks={**self.verification.checks, "clean_install": "failure"})
        factory = Mock(wraps=SQLiteFakeGitHub)

        with self.assertRaises(VerificationFailed):
            publish_with_retry(
                self.state_path, self.identity, self.package, failed,
                max_rounds=4, client_factory=factory,
            )

        self.assertEqual(factory.call_count, 1)
        self.assertEqual(SQLiteFakeGitHub(self.state_path)._expected_payload, before)

    def test_retry_revalidates_identity_after_a_competing_publication(self) -> None:
        competing_package = b"Competing publication fixture; not an installable wheel."
        competing_identity = replace(self.identity, sha256=sha256(competing_package).hexdigest())
        competing_verification = replace(
            self.verification, identity=competing_identity, checks=dict(self.verification.checks),
        )
        committed_snapshots = []

        class PublishCompetitorOnFirstLoad(SQLiteFakeGitHub):
            opened = 0

            def __init__(self, path: str | Path) -> None:
                super().__init__(path)
                type(self).opened += 1
                if type(self).opened == 1:
                    # This instance still holds the empty snapshot. Another client
                    # publishes a different approved package under the same tag.
                    competitor = SQLiteFakeGitHub(path)
                    publish_verified(
                        competitor, competing_identity, competing_package, competing_verification,
                    )
                    committed_snapshots.append(competitor._expected_payload)

        with self.assertRaisesRegex(VerificationFailed, "identity differs"):
            publish_with_retry(
                self.state_path, self.identity, self.package, self.verification,
                max_rounds=4, client_factory=PublishCompetitorOnFirstLoad,
            )

        # Round one conflicts; round two must revalidate and refuse this identity.
        self.assertEqual(PublishCompetitorOnFirstLoad.opened, 2)
        restored = SQLiteFakeGitHub(self.state_path)
        self.assertEqual(restored._expected_payload, committed_snapshots[0])
        release = restored.get_release(competing_identity.tag)
        self.assertIsNotNone(release)
        self.assertEqual(release.identity, competing_identity)
        self.assertFalse(release.draft)
        self.assertEqual(release.assets, {competing_identity.asset_name: competing_package})
        self.assertEqual(restored.publish_attempts, 1)
        self.assertEqual(restored.mutations.count(("publish", competing_identity.tag)), 1)


if __name__ == "__main__":
    unittest.main()
