"""Deterministic full-jitter contracts; local SQLite locks, no real sleeping."""

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from scripts.release_concurrency import publish_with_retry
from scripts.release_persistence import SQLiteFakeGitHub, StorageBusy
from scripts.release_simulation import ReleaseIdentity, REQUIRED_CHECKS, Verification, VerificationFailed, publish_verified


class ReleaseJitterTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "jitter-fake.sqlite"
        self.package = b"Small offline jitter fixture."
        self.identity = ReleaseIdentity("v0.0.0-jitter-test", "a" * 40, "fixture.whl", sha256(self.package).hexdigest())
        self.verification = Verification(self.identity, {name: "success" for name in REQUIRED_CHECKS})
        github = SQLiteFakeGitHub.create(self.path, tags={self.identity.tag: self.identity.source_commit})
        github.create_draft(self.identity)
        github.upload_asset(self.identity.tag, self.identity.asset_name, self.package)
        self.initial_payload = github._expected_payload
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "subprocess.Popen"):
            blocker = patch(target, side_effect=AssertionError("Offline jitter test"))
            blocker.start()
            self.addCleanup(blocker.stop)

    def lock(self, kind: str = "EXCLUSIVE") -> sqlite3.Connection:
        holder = sqlite3.connect(self.path, timeout=0.0)
        self.addCleanup(holder.close)
        holder.execute("BEGIN " + kind)
        return holder

    def run_publisher(self, **options):
        return publish_with_retry(self.path, self.identity, self.package, self.verification, **options)

    def assert_published_once(self) -> None:
        saved = SQLiteFakeGitHub(self.path)
        release = saved.get_release(self.identity.tag)
        self.assertIsNotNone(release)
        self.assertFalse(release.draft)
        self.assertEqual(release.identity, self.identity)
        self.assertEqual(release.assets, {self.identity.asset_name: self.package})
        self.assertEqual(saved.publish_attempts, 1)
        self.assertEqual(saved.mutations.count(("publish", self.identity.tag)), 1)

    def test_samples_follow_independent_ceiling_growth_and_cap(self) -> None:
        holder = self.lock("IMMEDIATE")
        uniform = Mock(side_effect=[0.002, 0.016, 0.006])
        waits = []

        def wait(delay: float) -> None:
            waits.append(delay)
            if len(waits) == 3:
                holder.rollback()

        outcome = self.run_publisher(
            max_rounds=4, base_delay_seconds=0.01, max_delay_seconds=0.02,
            uniform=uniform, sleep=wait,
        )

        self.assertEqual(uniform.call_args_list, [call(0.0, 0.01), call(0.0, 0.02), call(0.0, 0.02)])
        self.assertEqual(waits, [0.002, 0.016, 0.006])
        self.assertEqual((outcome.rounds, outcome.busy_errors), (4, 3))
        self.assertAlmostEqual(outcome.requested_backoff_seconds, 0.024)
        self.assert_published_once()

    def test_two_controlled_publishers_use_their_own_wait_streams(self) -> None:
        # Sequential, controlled invocations: this is not a concurrency benchmark
        # or evidence that random publishers can never select the same delay.
        selected_streams = ([0.001, 0.009], [0.008, 0.003])
        observed = []
        for selected in selected_streams:
            holder = self.lock()
            uniform = Mock(side_effect=selected)
            waits = []

            def wait(delay: float) -> None:
                waits.append(delay)
                if len(waits) == 2:
                    holder.rollback()

            outcome = self.run_publisher(
                max_rounds=3, base_delay_seconds=0.01, max_delay_seconds=0.02,
                uniform=uniform, sleep=wait,
            )
            self.assertEqual(uniform.call_args_list, [call(0.0, 0.01), call(0.0, 0.02)])
            self.assertEqual(waits, selected)
            self.assertEqual((outcome.rounds, outcome.busy_errors), (3, 2))
            observed.append(waits)

        self.assertNotEqual(observed[0], observed[1])
        self.assert_published_once()

    def test_zero_and_upper_endpoint_are_valid_samples(self) -> None:
        for selected in (0, 0.01):
            with self.subTest(selected=selected):
                holder = self.lock()
                uniform = Mock(return_value=selected)
                waits = []

                def wait(delay: float) -> None:
                    waits.append(delay)
                    holder.rollback()

                outcome = self.run_publisher(
                    base_delay_seconds=0.01, max_delay_seconds=0.02,
                    uniform=uniform, sleep=wait,
                )

                uniform.assert_called_once_with(0.0, 0.01)
                self.assertEqual(waits, [selected])
                self.assertEqual((outcome.rounds, outcome.busy_errors), (2, 1))
                self.assertAlmostEqual(outcome.requested_backoff_seconds, selected)
                self.assert_published_once()

    def test_invalid_samples_fail_before_sleep_or_publication(self) -> None:
        samples = (
            ("negative", -0.001), ("above_ceiling", 0.011),
            ("nan", float("nan")), ("infinity", float("inf")),
            ("negative_infinity", float("-inf")), ("true", True),
            ("false", False), ("text", "0.005"), ("none", None),
            ("huge_integer", 10 ** 1000),
        )
        for label, selected in samples:
            with self.subTest(sample=label):
                holder = self.lock()
                uniform = Mock(return_value=selected)
                sleeper = Mock()
                try:
                    with self.assertRaises(ValueError):
                        self.run_publisher(
                            base_delay_seconds=0.01, max_delay_seconds=0.02,
                            uniform=uniform, sleep=sleeper,
                        )
                finally:
                    holder.rollback()

                uniform.assert_called_once_with(0.0, 0.01)
                sleeper.assert_not_called()
                self.assertEqual(SQLiteFakeGitHub(self.path)._expected_payload, self.initial_payload)

    def test_final_busy_round_does_not_sample_or_sleep_even_with_zero_jitter(self) -> None:
        for rounds in (1, 3):
            with self.subTest(max_rounds=rounds):
                holder = self.lock()
                uniform = Mock(return_value=0.0)
                sleeper = Mock()
                try:
                    with self.assertRaises(StorageBusy):
                        self.run_publisher(max_rounds=rounds, uniform=uniform, sleep=sleeper)
                finally:
                    holder.rollback()

                self.assertEqual(uniform.call_count, rounds - 1)
                self.assertEqual(sleeper.call_args_list, [call(0.0)] * (rounds - 1))
                self.assertEqual(SQLiteFakeGitHub(self.path)._expected_payload, self.initial_payload)

    def test_success_does_not_sample_or_sleep(self) -> None:
        uniform, sleeper = Mock(), Mock()

        outcome = self.run_publisher(uniform=uniform, sleep=sleeper)

        uniform.assert_not_called()
        sleeper.assert_not_called()
        self.assertEqual((outcome.rounds, outcome.busy_errors), (1, 0))
        self.assertEqual(outcome.requested_backoff_seconds, 0.0)
        self.assert_published_once()

    def test_stale_snapshot_reloads_without_sampling_or_sleeping(self) -> None:
        identity, package, verification = self.identity, self.package, self.verification

        class PublishCompetitorOnFirstLoad(SQLiteFakeGitHub):
            opened = 0

            def __init__(self, path: str | Path) -> None:
                super().__init__(path)
                type(self).opened += 1
                if type(self).opened == 1:
                    competitor = SQLiteFakeGitHub(path)
                    publish_verified(competitor, identity, package, verification)

        uniform, sleeper = Mock(), Mock()
        outcome = self.run_publisher(
            client_factory=PublishCompetitorOnFirstLoad, uniform=uniform, sleep=sleeper,
        )

        self.assertEqual((outcome.rounds, outcome.conflicts, outcome.busy_errors), (2, 1, 0))
        uniform.assert_not_called()
        sleeper.assert_not_called()
        self.assert_published_once()

    def test_failed_verification_does_not_sample_or_sleep(self) -> None:
        failed = replace(self.verification, checks={**self.verification.checks, "clean_install": "failure"})
        uniform, sleeper = Mock(), Mock()

        with self.assertRaises(VerificationFailed):
            publish_with_retry(
                self.path, self.identity, self.package, failed, uniform=uniform, sleep=sleeper,
            )

        uniform.assert_not_called()
        sleeper.assert_not_called()
        self.assertEqual(SQLiteFakeGitHub(self.path)._expected_payload, self.initial_payload)

    def test_default_sampler_uses_random_uniform_at_call_time(self) -> None:
        holder = self.lock()
        waits = []

        def wait(delay: float) -> None:
            waits.append(delay)
            holder.rollback()

        with patch("random.uniform", return_value=0.004) as uniform:
            outcome = self.run_publisher(sleep=wait)

        uniform.assert_called_once_with(0.0, 0.05)
        self.assertEqual(waits, [0.004])
        self.assertEqual((outcome.rounds, outcome.busy_errors), (2, 1))
        self.assertAlmostEqual(outcome.requested_backoff_seconds, 0.004)
        self.assert_published_once()


if __name__ == "__main__":
    unittest.main()
