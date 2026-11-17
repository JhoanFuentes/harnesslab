"""Cooperative cancellation contracts with fake waits and local SQLite only."""

from contextlib import closing
from hashlib import sha256
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from scripts.release_concurrency import RetryCancelled, RetryDeadlineExceeded, publish_with_retry
from scripts.release_persistence import SQLiteFakeGitHub, StaleState, StateError, StorageBusy
from scripts.release_simulation import ReleaseIdentity, REQUIRED_CHECKS, Verification, VerificationFailed


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeCancelEvent:
    def __init__(self, on_wait=None) -> None:
        self.flag = False
        self.waits = []
        self.on_wait = on_wait

    def is_set(self) -> bool:
        return self.flag

    def set(self) -> None:
        self.flag = True

    def clear(self) -> None:
        raise AssertionError("Publisher must not consume the caller's cancellation signal")

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        return self.on_wait(timeout) if self.on_wait is not None else self.flag


class ReleaseCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "cancel-fake.sqlite"
        self.package = b"Small offline cancellation fixture."
        self.identity = ReleaseIdentity("v0.0.0-cancel-test", "a" * 40, "fixture.whl", sha256(self.package).hexdigest())
        self.verification = Verification(self.identity, {name: "success" for name in REQUIRED_CHECKS})
        self.github = SQLiteFakeGitHub.create(self.path, tags={self.identity.tag: self.identity.source_commit})
        self.initial_payload = self.github._expected_payload
        self.clock = FakeClock()
        self.sleeper = Mock(side_effect=AssertionError("Use the signal's wait, not sleep"))
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "subprocess.Popen"):
            blocker = patch(target, side_effect=AssertionError("Offline cancellation test"))
            blocker.start()
            self.addCleanup(blocker.stop)

    def run_publisher(self, **options):
        options.setdefault("monotonic", self.clock.monotonic)
        options.setdefault("sleep", self.sleeper)
        options.setdefault("uniform", lambda low, high: high)
        options.setdefault("base_delay_seconds", 0.125)
        options.setdefault("max_elapsed_seconds", 1.0)
        return publish_with_retry(self.path, self.identity, self.package, self.verification, **options)

    def lock(self, kind: str = "EXCLUSIVE") -> sqlite3.Connection:
        holder = sqlite3.connect(self.path, timeout=0.0)
        self.addCleanup(holder.close)
        holder.execute("BEGIN " + kind)
        return holder

    def assert_published_once(self, path: Path | None = None) -> None:
        saved = SQLiteFakeGitHub(path or self.path)
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

    def test_precancelled_signal_stops_before_clock_and_client(self) -> None:
        signal = threading.Event()
        signal.set()
        factory = Mock(wraps=SQLiteFakeGitHub)
        monotonic = Mock(return_value=100.0)

        with self.assertRaises(RetryCancelled):
            publish_with_retry(
                self.path, self.identity, self.package, self.verification,
                cancel_event=signal, client_factory=factory, monotonic=monotonic,
            )

        factory.assert_not_called()
        monotonic.assert_not_called()
        self.assertTrue(signal.is_set())
        self.assertEqual(SQLiteFakeGitHub(self.path)._expected_payload, self.initial_payload)

    def test_cancellation_after_loading_prevents_writes(self) -> None:
        signal = FakeCancelEvent()

        def load(path: str | Path) -> SQLiteFakeGitHub:
            github = SQLiteFakeGitHub(path)
            signal.set()
            return github

        factory = Mock(side_effect=load)
        with self.assertRaises(RetryCancelled):
            self.run_publisher(cancel_event=signal, client_factory=factory)

        self.assertEqual(factory.call_count, 1)
        self.assertTrue(signal.is_set())
        self.assertEqual(signal.waits, [])
        self.sleeper.assert_not_called()
        self.assertEqual(SQLiteFakeGitHub(self.path)._expected_payload, self.initial_payload)

    def test_wait_cancellation_stops_retry_and_holds_no_writer_transaction(self) -> None:
        for keep_flag in (True, False):
            with self.subTest(flag_still_set_after_wait=keep_flag):
                holder = self.lock("IMMEDIATE")
                clock = FakeClock()
                signal = FakeCancelEvent()
                factory = Mock(wraps=SQLiteFakeGitHub)

                def interrupted_wait(timeout: float) -> bool:
                    clock.advance(0.03125)
                    holder.rollback()
                    # The failed publisher must have closed its own connection.
                    with closing(sqlite3.connect(self.path, timeout=0.0)) as probe:
                        probe.execute("BEGIN EXCLUSIVE")
                        probe.rollback()
                    if keep_flag:
                        signal.set()
                    # True records the observed cancellation even if is_set is now False.
                    return True

                signal.on_wait = interrupted_wait
                try:
                    with self.assertRaises(RetryCancelled):
                        self.run_publisher(cancel_event=signal, client_factory=factory, monotonic=clock.monotonic)
                finally:
                    holder.rollback()

                self.assertEqual(factory.call_count, 1)
                self.assertEqual(signal.waits, [0.125])
                self.assertEqual(clock.now, 100.03125)
                self.assertEqual(signal.is_set(), keep_flag)
                self.sleeper.assert_not_called()
                self.assertEqual(SQLiteFakeGitHub(self.path)._expected_payload, self.initial_payload)

    def test_post_commit_cancellation_preserves_publication_and_fresh_signal_recovers(self) -> None:
        for stage in ("publish", "confirm"):
            with self.subTest(stage=stage):
                path = self.path.with_name(stage + ".sqlite")
                SQLiteFakeGitHub.create(path, tags={self.identity.tag: self.identity.source_commit})
                signal = FakeCancelEvent()
                clock = FakeClock()

                class CancelAfterEffect(SQLiteFakeGitHub):
                    confirmations = 0

                    def publish(self, tag: str) -> None:
                        super().publish(tag)
                        if stage == "publish":
                            clock.advance(0.125)
                            signal.set()

                    def assert_current(self) -> None:
                        super().assert_current()
                        type(self).confirmations += 1
                        if stage == "confirm":
                            clock.advance(0.125)
                            signal.set()

                with self.assertRaises(RetryCancelled):
                    publish_with_retry(
                        path, self.identity, self.package, self.verification,
                        cancel_event=signal, client_factory=CancelAfterEffect,
                        monotonic=clock.monotonic, max_elapsed_seconds=0.125, sleep=self.sleeper,
                    )

                self.assertEqual(CancelAfterEffect.confirmations, 0 if stage == "publish" else 1)
                self.assert_published_once(path)
                before = SQLiteFakeGitHub(path)._expected_payload
                fresh_signal = threading.Event()
                with patch.object(SQLiteFakeGitHub, "publish", side_effect=AssertionError("Unexpected republication")):
                    outcome = publish_with_retry(
                        path, self.identity, self.package, self.verification,
                        cancel_event=fresh_signal, monotonic=clock.monotonic,
                        max_elapsed_seconds=0.125, sleep=self.sleeper,
                    )

                self.assertEqual(outcome.rounds, 1)
                self.assertTrue(signal.is_set())
                self.assertFalse(fresh_signal.is_set())
                self.assertEqual(signal.waits, [])
                self.assertEqual(SQLiteFakeGitHub(path)._expected_payload, before)
        self.sleeper.assert_not_called()

    def test_cancellation_after_retryable_error_precedes_deadline_and_round_limit(self) -> None:
        # Typed failures are authored observations here; real SQLite contention
        # is exercised in the wait tests above and below.
        for error_type in (StorageBusy, StaleState):
            with self.subTest(error=error_type.__name__):
                signal = FakeCancelEvent()
                clock = FakeClock()
                uniform = Mock()

                def fail(path: str | Path):
                    clock.advance(0.125)
                    signal.set()
                    raise error_type("Injected retryable failure")

                factory = Mock(side_effect=fail)
                with self.assertRaises(RetryCancelled):
                    self.run_publisher(
                        cancel_event=signal, client_factory=factory, monotonic=clock.monotonic,
                        max_elapsed_seconds=0.125, max_rounds=1, uniform=uniform,
                    )

                self.assertEqual(factory.call_count, 1)
                uniform.assert_not_called()
                self.assertEqual(signal.waits, [])
                self.assertTrue(signal.is_set())
                self.assertEqual(SQLiteFakeGitHub(self.path)._expected_payload, self.initial_payload)
        self.sleeper.assert_not_called()

    def test_sampler_cancellation_prevents_starting_the_wait(self) -> None:
        holder = self.lock()
        signal = FakeCancelEvent()
        factory = Mock(wraps=SQLiteFakeGitHub)

        def sample(low: float, high: float) -> float:
            self.clock.advance(0.125)
            signal.set()
            return 0.0

        uniform = Mock(side_effect=sample)
        try:
            with self.assertRaises(RetryCancelled):
                self.run_publisher(
                    cancel_event=signal, client_factory=factory, uniform=uniform,
                    max_elapsed_seconds=0.125,
                )
        finally:
            holder.rollback()

        self.assertEqual(factory.call_count, 1)
        uniform.assert_called_once_with(0.0, 0.125)
        self.assertEqual(signal.waits, [])
        self.sleeper.assert_not_called()
        self.assertTrue(signal.is_set())
        self.assertEqual(SQLiteFakeGitHub(self.path)._expected_payload, self.initial_payload)

    def test_unset_signal_uses_wait_and_can_finish_after_contention(self) -> None:
        holder = self.lock()
        signal = FakeCancelEvent()

        def timed_wait(timeout: float) -> bool:
            self.clock.advance(timeout)
            holder.rollback()
            return False

        signal.on_wait = timed_wait
        outcome = self.run_publisher(cancel_event=signal)

        self.assertEqual(signal.waits, [0.125])
        self.assertFalse(signal.is_set())
        self.assertEqual((outcome.rounds, outcome.busy_errors), (2, 1))
        self.assertAlmostEqual(outcome.requested_backoff_seconds, 0.125)
        self.assertAlmostEqual(outcome.elapsed_seconds, 0.125)
        self.sleeper.assert_not_called()
        self.assert_published_once()

    def test_false_wait_rechecks_deadline_and_cancellation_before_reopening(self) -> None:
        for actual_wait, cancelled, expected in (
            (0.125, False, RetryDeadlineExceeded),
            (0.25, False, RetryDeadlineExceeded),
            (0.25, True, RetryCancelled),
        ):
            with self.subTest(actual_wait=actual_wait, cancelled=cancelled):
                holder = self.lock()
                signal = FakeCancelEvent()
                clock = FakeClock()
                factory = Mock(wraps=SQLiteFakeGitHub)

                def timed_wait(timeout: float) -> bool:
                    clock.advance(actual_wait)
                    if cancelled:
                        signal.set()
                    return False

                signal.on_wait = timed_wait
                try:
                    with self.assertRaises(expected):
                        self.run_publisher(
                            cancel_event=signal, client_factory=factory, monotonic=clock.monotonic,
                            max_elapsed_seconds=0.125, base_delay_seconds=0.25,
                        )
                finally:
                    holder.rollback()

                self.assertEqual(factory.call_count, 1)
                self.assertEqual(signal.waits, [0.125])
                self.assertEqual(signal.is_set(), cancelled)
                self.assertEqual(SQLiteFakeGitHub(self.path)._expected_payload, self.initial_payload)
        self.sleeper.assert_not_called()

    def test_cancellation_does_not_hide_original_fatal_errors(self) -> None:
        for fatal in (VerificationFailed("Injected invalid verification"), StateError("Injected I/O failure")):
            with self.subTest(error=type(fatal).__name__):
                signal = FakeCancelEvent()
                clock = FakeClock()

                class FatalDuringOperation(SQLiteFakeGitHub):
                    def resolve_tag(self, tag: str) -> str | None:
                        signal.set()
                        clock.advance(0.125)
                        raise fatal

                with self.assertRaises(type(fatal)) as caught:
                    self.run_publisher(
                        cancel_event=signal, client_factory=FatalDuringOperation,
                        monotonic=clock.monotonic, max_elapsed_seconds=0.125,
                    )

                self.assertIs(caught.exception, fatal)
                self.assertTrue(signal.is_set())
                self.assertEqual(signal.waits, [])
                self.assertEqual(SQLiteFakeGitHub(self.path)._expected_payload, self.initial_payload)
        self.sleeper.assert_not_called()


if __name__ == "__main__":
    unittest.main()
