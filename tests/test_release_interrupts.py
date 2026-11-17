"""Ctrl+C bridge tests with fake handlers; no operating-system signal is sent."""

from contextlib import closing
from hashlib import sha256
import importlib
from pathlib import Path
import signal
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.release_concurrency import RetryCancelled, RetryDeadlineExceeded, publish_with_retry
from scripts.release_persistence import SQLiteFakeGitHub
from scripts.release_simulation import ReleaseIdentity, REQUIRED_CHECKS, Verification, VerificationFailed


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.waits = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


class FakeSignalAPI:
    SIGINT = signal.SIGINT
    SIG_DFL = signal.SIG_DFL
    SIG_IGN = signal.SIG_IGN

    def __init__(self, previous=signal.SIG_DFL) -> None:
        self.current = previous
        self.installations = []
        self.reads = 0

    def getsignal(self, signum):
        if signum != self.SIGINT:
            raise AssertionError("Unexpected signal number")
        self.reads += 1
        return self.current

    def signal(self, signum, handler):
        if signum != self.SIGINT:
            raise AssertionError("Unexpected signal number")
        previous = self.current
        self.installations.append(handler)
        self.current = handler
        return previous

    def emit(self, frame=None) -> None:
        if not callable(self.current):
            raise AssertionError("No fake handler is installed")
        self.current(self.SIGINT, frame)


class ReleaseInterruptTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "interrupt-fake.sqlite"
        self.package = b"Small offline Ctrl+C fixture."
        self.identity = ReleaseIdentity("v0.0.0-interrupt-test", "a" * 40, "fixture.whl", sha256(self.package).hexdigest())
        self.verification = Verification(self.identity, {name: "success" for name in REQUIRED_CHECKS})
        self.github = SQLiteFakeGitHub.create(self.path, tags={self.identity.tag: self.identity.source_commit})
        self.initial_payload = self.github._expected_payload
        self.clock = FakeClock()
        for target in (
            "signal.raise_signal", "os.kill", "signal.signal", "signal.getsignal",
            "socket.socket.connect", "socket.socket.connect_ex", "subprocess.Popen",
        ):
            blocker = patch(target, side_effect=AssertionError("Offline handler test"))
            blocker.start()
            self.addCleanup(blocker.stop)
        self.bridge = importlib.import_module("scripts.release_interrupts")

    def run_publisher(self, token, **options):
        options.setdefault("monotonic", self.clock.monotonic)
        options.setdefault("sleep", Mock(side_effect=AssertionError("Bridge wait owns polling")))
        options.setdefault("uniform", lambda low, high: high)
        options.setdefault("max_elapsed_seconds", 1.0)
        return publish_with_retry(
            self.path, self.identity, self.package, self.verification, cancel_event=token, **options,
        )

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

    def test_repeated_sigint_sets_a_sticky_flag_without_raising(self) -> None:
        signals = FakeSignalAPI()

        with self.bridge.ctrl_c_cancellation(signal_api=signals) as token:
            self.assertFalse(token.is_set())
            signals.emit(frame=object())
            self.assertTrue(token.is_set())
            signals.emit(frame=object())
            self.assertTrue(token.is_set())

        self.assertIs(signals.current, signals.SIG_DFL)

    def test_original_handler_is_restored_on_normal_and_exceptional_exit(self) -> None:
        previous_handlers = (Mock(), signal.SIG_DFL, signal.SIG_IGN)
        failures = (None, RetryCancelled("cancelled"), VerificationFailed("invalid"),
                    RetryDeadlineExceeded("expired"), KeyboardInterrupt())
        for previous in previous_handlers:
            for failure in failures:
                with self.subTest(previous=repr(previous), error=type(failure).__name__):
                    signals = FakeSignalAPI(previous)
                    if failure is None:
                        with self.bridge.ctrl_c_cancellation(signal_api=signals):
                            self.assertTrue(callable(signals.current))
                    else:
                        with self.assertRaises(type(failure)) as caught:
                            with self.bridge.ctrl_c_cancellation(signal_api=signals):
                                raise failure
                        self.assertIs(caught.exception, failure)
                    self.assertIs(signals.current, previous)
                    self.assertEqual(len(signals.installations), 2)
                    self.assertIs(signals.installations[-1], previous)
        previous_handlers[0].assert_not_called()

    def test_unrestorable_or_failed_installation_prevents_publishing(self) -> None:
        previous = Mock()

        class FailedInstallation(FakeSignalAPI):
            def signal(self, signum, handler):
                raise OSError("Injected handler registration failure")

        for signals, error_type in ((FakeSignalAPI(None), RuntimeError), (FailedInstallation(previous), OSError)):
            with self.subTest(error=error_type.__name__):
                factory = Mock(wraps=SQLiteFakeGitHub)
                with self.assertRaises(error_type):
                    with self.bridge.ctrl_c_cancellation(signal_api=signals) as token:
                        self.run_publisher(token, client_factory=factory)
                factory.assert_not_called()
                self.assertEqual(signals.installations, [])
                self.assertEqual(SQLiteFakeGitHub(self.path)._expected_payload, self.initial_payload)
        self.assertIs(signals.current, previous)

    def test_non_main_thread_identity_is_rejected_before_registration(self) -> None:
        signals = FakeSignalAPI()
        # Simulate thread identity; do not create a thread or register an OS handler.
        with patch("scripts.release_interrupts.threading.current_thread", return_value=object()):
            with self.assertRaisesRegex(RuntimeError, "main thread"):
                with self.bridge.ctrl_c_cancellation(signal_api=signals):
                    self.fail("Non-main-thread context must not be entered")

        self.assertEqual(signals.reads, 0)
        self.assertEqual(signals.installations, [])

    def test_polling_caps_chunks_and_uninterrupted_publication_succeeds(self) -> None:
        signals = FakeSignalAPI(signal.SIG_IGN)
        with self.bridge.ctrl_c_cancellation(
            signal_api=signals, monotonic=self.clock.monotonic, sleep=self.clock.sleep,
            poll_interval_seconds=0.0625,
        ) as token:
            self.assertFalse(token.wait(0.0))
            self.assertEqual(self.clock.waits, [])
            self.assertFalse(token.wait(0.15625))
            self.assertEqual(self.clock.waits, [0.0625, 0.0625, 0.03125])
            self.assertFalse(token.is_set())
            outcome = self.run_publisher(token)

        self.assertEqual(outcome.rounds, 1)
        self.assertEqual(outcome.busy_errors, 0)
        self.assertIs(signals.current, signal.SIG_IGN)
        self.assert_published_once()

    def test_sigint_before_publishing_prevents_opening_the_client(self) -> None:
        signals = FakeSignalAPI()
        factory = Mock(wraps=SQLiteFakeGitHub)
        with self.assertRaises(RetryCancelled):
            with self.bridge.ctrl_c_cancellation(signal_api=signals) as token:
                signals.emit(frame=object())
                self.run_publisher(token, client_factory=factory)

        factory.assert_not_called()
        self.assertIs(signals.current, signal.SIG_DFL)
        self.assertEqual(SQLiteFakeGitHub(self.path)._expected_payload, self.initial_payload)

    def test_sigint_during_backoff_stops_on_the_next_virtual_poll(self) -> None:
        signals = FakeSignalAPI()
        holder = sqlite3.connect(self.path, timeout=0.0)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")
        factory = Mock(wraps=SQLiteFakeGitHub)
        wrapper_sleep = Mock(side_effect=AssertionError("Use the bridge's wait"))

        def polling_sleep(seconds: float) -> None:
            self.clock.sleep(seconds)
            signals.emit(frame=object())
            holder.rollback()
            with closing(sqlite3.connect(self.path, timeout=0.0)) as probe:
                probe.execute("BEGIN EXCLUSIVE")
                probe.rollback()

        try:
            with self.assertRaises(RetryCancelled):
                with self.bridge.ctrl_c_cancellation(
                    signal_api=signals, monotonic=self.clock.monotonic,
                    sleep=polling_sleep, poll_interval_seconds=0.0625,
                ) as token:
                    self.run_publisher(
                        token, client_factory=factory, base_delay_seconds=0.25, sleep=wrapper_sleep,
                    )
        finally:
            holder.rollback()

        self.assertEqual(factory.call_count, 1)
        self.assertEqual(self.clock.waits, [0.0625])
        wrapper_sleep.assert_not_called()
        self.assertIs(signals.current, signal.SIG_DFL)
        self.assertEqual(SQLiteFakeGitHub(self.path)._expected_payload, self.initial_payload)

    def test_sigint_after_commit_preserves_effect_and_new_context_recovers(self) -> None:
        signals = FakeSignalAPI(signal.SIG_IGN)

        class InterruptAfterCommit(SQLiteFakeGitHub):
            def publish(self, tag: str) -> None:
                super().publish(tag)
                signals.emit(frame=object())

        with self.assertRaises(RetryCancelled):
            with self.bridge.ctrl_c_cancellation(signal_api=signals) as old_token:
                self.run_publisher(old_token, client_factory=InterruptAfterCommit)

        self.assertTrue(old_token.is_set())
        self.assertIs(signals.current, signal.SIG_IGN)
        self.assert_published_once()
        before = SQLiteFakeGitHub(self.path)._expected_payload
        with patch.object(SQLiteFakeGitHub, "publish", side_effect=AssertionError("Unexpected republication")):
            with self.bridge.ctrl_c_cancellation(signal_api=signals) as fresh_token:
                self.assertFalse(fresh_token.is_set())
                outcome = self.run_publisher(fresh_token)

        self.assertEqual(outcome.rounds, 1)
        self.assertTrue(old_token.is_set())
        self.assertIs(signals.current, signal.SIG_IGN)
        self.assertEqual(SQLiteFakeGitHub(self.path)._expected_payload, before)

    def test_nested_contexts_restore_handlers_in_lifo_order(self) -> None:
        previous = Mock()
        signals = FakeSignalAPI(previous)
        with self.bridge.ctrl_c_cancellation(signal_api=signals) as outer:
            outer_handler = signals.current
            with self.bridge.ctrl_c_cancellation(signal_api=signals) as inner:
                self.assertIsNot(signals.current, outer_handler)
                signals.emit(frame=object())
                self.assertTrue(inner.is_set())
                self.assertFalse(outer.is_set())
            self.assertIs(signals.current, outer_handler)
            signals.emit(frame=object())
            self.assertTrue(outer.is_set())

        self.assertIs(signals.current, previous)
        self.assertEqual(len(signals.installations), 4)
        previous.assert_not_called()


if __name__ == "__main__":
    unittest.main()
