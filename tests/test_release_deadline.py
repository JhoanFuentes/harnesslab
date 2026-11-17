"""Cooperative retry deadlines with a fake monotonic clock and local SQLite."""

from hashlib import sha256
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.release_concurrency import RetryDeadlineExceeded, publish_with_retry
from scripts.release_persistence import SQLiteFakeGitHub
from scripts.release_simulation import ReleaseIdentity, REQUIRED_CHECKS, Verification, publish_verified


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.waits = []

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.advance(seconds)


class ReleaseDeadlineTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "deadline-fake.sqlite"
        self.package = b"Small offline deadline fixture."
        self.identity = ReleaseIdentity("v0.0.0-deadline-test", "a" * 40, "fixture.whl", sha256(self.package).hexdigest())
        self.verification = Verification(self.identity, {name: "success" for name in REQUIRED_CHECKS})
        self.github = SQLiteFakeGitHub.create(self.path, tags={self.identity.tag: self.identity.source_commit})
        self.initial_payload = self.github._expected_payload
        self.clock = FakeClock()
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "subprocess.Popen"):
            blocker = patch(target, side_effect=AssertionError("Offline deadline test"))
            blocker.start()
            self.addCleanup(blocker.stop)

    def run_publisher(self, **options):
        options.setdefault("monotonic", self.clock.monotonic)
        options.setdefault("sleep", self.clock.sleep)
        options.setdefault("uniform", lambda low, high: high)
        return publish_with_retry(self.path, self.identity, self.package, self.verification, **options)

    def lock(self) -> sqlite3.Connection:
        holder = sqlite3.connect(self.path, timeout=0.0)
        self.addCleanup(holder.close)
        holder.execute("BEGIN EXCLUSIVE")
        return holder

    def assert_published_once(self) -> None:
        saved = SQLiteFakeGitHub(self.path)
        release = saved.get_release(self.identity.tag)
        self.assertIsNotNone(release)
        self.assertFalse(release.draft)
        self.assertEqual(release.identity, self.identity)
        self.assertEqual(release.assets, {self.identity.asset_name: self.package})
        self.assertEqual(saved.publish_attempts, 1)
        self.assertEqual(saved.mutations, [
            ("create_draft", self.identity.tag),
            ("upload_asset", self.identity.tag),
            ("publish", self.identity.tag),
        ])

    def test_zero_budget_does_not_open_client_or_change_state(self) -> None:
        factory = Mock(wraps=SQLiteFakeGitHub)

        with self.assertRaises(RetryDeadlineExceeded):
            self.run_publisher(max_elapsed_seconds=0.0, client_factory=factory)

        factory.assert_not_called()
        self.assertEqual(self.clock.waits, [])
        self.assertEqual(SQLiteFakeGitHub(self.path)._expected_payload, self.initial_payload)

    def test_wait_is_clipped_to_remaining_time_and_no_new_round_starts(self) -> None:
        holder = self.lock()
        factory = Mock(wraps=SQLiteFakeGitHub)
        uniform = Mock(return_value=0.25)
        try:
            with self.assertRaises(RetryDeadlineExceeded):
                self.run_publisher(
                    max_elapsed_seconds=0.125, max_rounds=4,
                    base_delay_seconds=0.25, max_delay_seconds=0.5,
                    uniform=uniform, client_factory=factory,
                )
        finally:
            holder.rollback()

        uniform.assert_called_once_with(0.0, 0.25)
        self.assertEqual(self.clock.waits, [0.125])
        self.assertEqual(self.clock.now, 100.125)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(SQLiteFakeGitHub(self.path)._expected_payload, self.initial_payload)

    def test_oversleep_is_detected_before_another_client_is_opened(self) -> None:
        holder = self.lock()
        factory = Mock(wraps=SQLiteFakeGitHub)

        def oversleep(seconds: float) -> None:
            self.clock.sleep(seconds)
            self.clock.advance(0.125)

        try:
            with self.assertRaises(RetryDeadlineExceeded):
                self.run_publisher(
                    max_elapsed_seconds=0.125, base_delay_seconds=0.0625,
                    max_delay_seconds=0.25, sleep=oversleep, client_factory=factory,
                )
        finally:
            holder.rollback()

        self.assertEqual(self.clock.waits, [0.0625])
        self.assertEqual(self.clock.now, 100.1875)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(SQLiteFakeGitHub(self.path)._expected_payload, self.initial_payload)

    def test_opening_time_counts_and_deadline_precedes_busy_round_exhaustion(self) -> None:
        for busy in (False, True):
            with self.subTest(busy_during_load=busy):
                clock = FakeClock()
                holder = self.lock() if busy else None
                uniform = Mock()

                def load(path: str | Path) -> SQLiteFakeGitHub:
                    clock.advance(0.125)
                    return SQLiteFakeGitHub(path)

                factory = Mock(side_effect=load)
                try:
                    with self.assertRaises(RetryDeadlineExceeded):
                        self.run_publisher(
                            max_elapsed_seconds=0.125, max_rounds=1, client_factory=factory,
                            monotonic=clock.monotonic, sleep=clock.sleep, uniform=uniform,
                        )
                finally:
                    if holder is not None:
                        holder.rollback()

                self.assertEqual(factory.call_count, 1)
                uniform.assert_not_called()
                self.assertEqual(clock.waits, [])
                self.assertEqual(SQLiteFakeGitHub(self.path)._expected_payload, self.initial_payload)

    def test_expiration_after_publication_does_not_undo_or_repeat_the_effect(self) -> None:
        clock = self.clock

        class SlowPublication(SQLiteFakeGitHub):
            confirmations = 0

            def publish(self, tag: str) -> None:
                super().publish(tag)
                clock.advance(0.125)

            def assert_current(self) -> None:
                type(self).confirmations += 1
                super().assert_current()

        with self.assertRaises(RetryDeadlineExceeded):
            self.run_publisher(max_elapsed_seconds=0.125, client_factory=SlowPublication)

        self.assertEqual(SlowPublication.confirmations, 0)
        self.assert_published_once()
        before = SQLiteFakeGitHub(self.path)._expected_payload

        # A new invocation starts its own clock window and inspects saved state.
        with patch.object(SQLiteFakeGitHub, "publish", side_effect=AssertionError("Unexpected republication")):
            outcome = self.run_publisher(max_elapsed_seconds=0.125)

        self.assertEqual(outcome.rounds, 1)
        self.assertEqual(outcome.elapsed_seconds, 0.0)
        self.assertEqual(SQLiteFakeGitHub(self.path)._expected_payload, before)

    def test_confirmation_overrun_cannot_return_success(self) -> None:
        clock = self.clock

        class SlowConfirmation(SQLiteFakeGitHub):
            confirmations = 0

            def assert_current(self) -> None:
                super().assert_current()
                type(self).confirmations += 1
                clock.advance(0.125)

        with self.assertRaises(RetryDeadlineExceeded):
            self.run_publisher(max_elapsed_seconds=0.125, client_factory=SlowConfirmation)

        self.assertEqual(SlowConfirmation.confirmations, 1)
        self.assert_published_once()
        before = SQLiteFakeGitHub(self.path)._expected_payload
        with patch.object(SQLiteFakeGitHub, "publish", side_effect=AssertionError("Unexpected republication")):
            self.run_publisher(max_elapsed_seconds=0.125)
        self.assertEqual(SQLiteFakeGitHub(self.path)._expected_payload, before)

    def test_elapsed_metrics_include_work_and_differ_from_requested_backoff(self) -> None:
        clock = self.clock
        holder = self.lock()

        class TimedClient(SQLiteFakeGitHub):
            def __init__(self, path: str | Path) -> None:
                clock.advance(0.03125)
                super().__init__(path)

            def publish(self, tag: str) -> None:
                super().publish(tag)
                clock.advance(0.125)

            def assert_current(self) -> None:
                super().assert_current()
                clock.advance(0.03125)

        def wait(seconds: float) -> None:
            clock.sleep(seconds)
            holder.rollback()

        outcome = self.run_publisher(
            max_elapsed_seconds=1.0, base_delay_seconds=0.125,
            client_factory=TimedClient, uniform=lambda low, high: 0.0625, sleep=wait,
        )

        self.assertEqual((outcome.rounds, outcome.busy_errors), (2, 1))
        self.assertEqual(clock.waits, [0.0625])
        self.assertAlmostEqual(outcome.requested_backoff_seconds, 0.0625)
        self.assertAlmostEqual(outcome.elapsed_seconds, 0.28125)
        self.assertGreater(outcome.elapsed_seconds, outcome.requested_backoff_seconds)
        self.assert_published_once()

    def test_stale_and_busy_rounds_share_one_time_budget(self) -> None:
        opened = 0
        holders = []

        def load(path: str | Path) -> SQLiteFakeGitHub:
            nonlocal opened
            opened += 1
            if opened == 1:
                stale = SQLiteFakeGitHub(path)
                competitor = SQLiteFakeGitHub(path)
                publish_verified(competitor, self.identity, self.package, self.verification)
                self.clock.advance(0.0625)
                return stale
            if opened == 2:
                holders.append(self.lock())
                self.clock.advance(0.0625)
                return SQLiteFakeGitHub(path)
            raise AssertionError("Deadline should prevent another factory call")

        uniform = Mock()
        try:
            with self.assertRaises(RetryDeadlineExceeded):
                self.run_publisher(max_elapsed_seconds=0.125, client_factory=load, uniform=uniform)
        finally:
            for holder in holders:
                holder.rollback()

        self.assertEqual(opened, 2)
        self.assertEqual(self.clock.now, 100.125)
        uniform.assert_not_called()
        self.assertEqual(self.clock.waits, [])
        self.assert_published_once()

    def test_slow_jitter_source_cannot_start_a_sleep_after_the_deadline(self) -> None:
        holder = self.lock()
        factory = Mock(wraps=SQLiteFakeGitHub)

        def sample(low: float, high: float) -> float:
            self.clock.advance(0.125)
            return 0.0

        try:
            with self.assertRaises(RetryDeadlineExceeded):
                self.run_publisher(max_elapsed_seconds=0.125, uniform=sample, client_factory=factory)
        finally:
            holder.rollback()

        self.assertEqual(factory.call_count, 1)
        self.assertEqual(self.clock.waits, [])
        self.assertEqual(SQLiteFakeGitHub(self.path)._expected_payload, self.initial_payload)

    def test_invalid_budgets_and_invalid_or_regressing_clocks_fail_before_opening(self) -> None:
        invalid = (
            ("negative", -1), ("nan", float("nan")), ("infinity", float("inf")),
            ("bool", True), ("text", "1"), ("huge_integer", 10 ** 1000),
        )
        for label, budget in invalid:
            with self.subTest(budget=label):
                factory = Mock(wraps=SQLiteFakeGitHub)
                with self.assertRaises(ValueError):
                    self.run_publisher(max_elapsed_seconds=budget, client_factory=factory)
                factory.assert_not_called()

        clocks = (
            ("nan", Mock(return_value=float("nan"))),
            ("infinity", Mock(return_value=float("inf"))),
            ("bool", Mock(return_value=True)),
            ("backwards", Mock(side_effect=[100.0, 99.0])),
        )
        for label, monotonic in clocks:
            with self.subTest(clock=label):
                factory = Mock(wraps=SQLiteFakeGitHub)
                with self.assertRaises(ValueError):
                    self.run_publisher(max_elapsed_seconds=1.0, monotonic=monotonic, client_factory=factory)
                factory.assert_not_called()

        self.assertEqual(self.clock.waits, [])
        self.assertEqual(SQLiteFakeGitHub(self.path)._expected_payload, self.initial_payload)


if __name__ == "__main__":
    unittest.main()
