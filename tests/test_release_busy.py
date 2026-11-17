"""Bounded SQLite contention retries, with real locks and no actual sleeping."""

from contextlib import closing
from hashlib import sha256
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.release_concurrency import publish_with_retry
from scripts.release_persistence import SQLiteFakeGitHub, StateError, StorageBusy
from scripts.release_simulation import ReleaseIdentity, REQUIRED_CHECKS, Verification


def sqlite_error(code: int | None, message: str = "Injected SQLite error") -> sqlite3.OperationalError:
    error = sqlite3.OperationalError(message)
    if code is not None:
        error.sqlite_errorcode = code
    return error


class ReleaseBusyTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "busy-fake.sqlite"
        # Keep the publication UPDATE below the ordinary SQLite cache size.
        self.package = b"Small offline contention fixture."
        self.identity = ReleaseIdentity("v0.0.0-busy-test", "a" * 40, "fixture.whl", sha256(self.package).hexdigest())
        self.verification = Verification(self.identity, {name: "success" for name in REQUIRED_CHECKS})
        github = SQLiteFakeGitHub.create(self.path, tags={self.identity.tag: self.identity.source_commit})
        github.create_draft(self.identity)
        github.upload_asset(self.identity.tag, self.identity.asset_name, self.package)
        self.initial_payload = github._expected_payload
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "subprocess.Popen"):
            blocker = patch(target, side_effect=AssertionError("Offline contention test"))
            blocker.start()
            self.addCleanup(blocker.stop)

    def lock(self, kind: str) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=0.0)
        self.addCleanup(connection.close)
        connection.execute("BEGIN " + kind)
        return connection

    def run_publisher(self, **options):
        # These older tests exercise contention policy, using the upper jitter bound.
        options.setdefault("uniform", lambda low, high: high)
        return publish_with_retry(self.path, self.identity, self.package, self.verification, **options)

    def assert_published_once(self, attempts: int = 1) -> None:
        restored = SQLiteFakeGitHub(self.path)
        release = restored.get_release(self.identity.tag)
        self.assertIsNotNone(release)
        self.assertFalse(release.draft)
        self.assertEqual(release.identity, self.identity)
        self.assertEqual(release.assets, {self.identity.asset_name: self.package})
        self.assertEqual(restored.upload_attempts, 1)
        self.assertEqual(restored.publish_attempts, attempts)
        self.assertEqual(restored.mutations, [
            ("create_draft", self.identity.tag),
            ("upload_asset", self.identity.tag),
            ("publish", self.identity.tag),
        ])

    def test_writer_lock_retries_with_capped_backoff_then_publishes(self) -> None:
        holder = self.lock("IMMEDIATE")
        waits = []

        def wait(delay: float) -> None:
            waits.append(delay)
            if len(waits) == 3:
                holder.rollback()

        outcome = self.run_publisher(
            max_rounds=4, base_delay_seconds=0.01, max_delay_seconds=0.02, sleep=wait,
        )

        self.assertEqual(waits, [0.01, 0.02, 0.02])
        self.assertEqual((outcome.rounds, outcome.busy_errors, outcome.conflicts), (4, 3, 0))
        self.assertAlmostEqual(outcome.requested_backoff_seconds, 0.05)
        self.assert_published_once()

    def test_exclusive_lock_during_client_load_is_retried(self) -> None:
        holder = self.lock("EXCLUSIVE")
        waits = []
        factory = Mock(wraps=SQLiteFakeGitHub)

        def wait(delay: float) -> None:
            waits.append(delay)
            holder.rollback()

        outcome = self.run_publisher(client_factory=factory, sleep=wait)

        self.assertEqual(factory.call_count, 2)
        self.assertEqual(waits, [0.05])
        self.assertEqual((outcome.rounds, outcome.busy_errors), (2, 1))
        self.assert_published_once()

    def test_persistent_lock_exhausts_budget_without_final_sleep_or_effect(self) -> None:
        holder = self.lock("IMMEDIATE")
        waits = []
        factory = Mock(wraps=SQLiteFakeGitHub)

        with self.assertRaises(StorageBusy):
            self.run_publisher(
                max_rounds=3, base_delay_seconds=0.01, max_delay_seconds=0.02,
                sleep=waits.append, client_factory=factory,
            )

        self.assertEqual(waits, [0.01, 0.02])
        self.assertEqual(factory.call_count, 3)
        holder.rollback()
        self.assertEqual(SQLiteFakeGitHub(self.path)._expected_payload, self.initial_payload)

    def test_busy_commit_closes_transaction_before_waiting_and_restarts(self) -> None:
        test = self
        readers = []
        waits = []
        tag = self.identity.tag

        class ReaderBlocksCommit(SQLiteFakeGitHub):
            armed = True

            def _checkpoint_boundary(self, point: str) -> None:
                if point == "before_commit" and type(self).armed and self.mutations[-1:] == [("publish", tag)]:
                    type(self).armed = False
                    reader = test.lock("DEFERRED")
                    reader.execute("SELECT payload FROM fake_state WHERE id = 1").fetchone()
                    readers.append(reader)

        def wait(delay: float) -> None:
            waits.append(delay)
            saved = SQLiteFakeGitHub(self.path)
            self.assertTrue(saved.get_release(tag).draft)
            self.assertEqual(saved.publish_attempts, 1)
            self.assertEqual(saved.mutations.count(("publish", tag)), 0)
            # A new RESERVED lock succeeds only after the failed writer closed.
            with closing(sqlite3.connect(self.path, timeout=0.0)) as probe:
                probe.execute("BEGIN IMMEDIATE")
                probe.rollback()
            self.assertEqual(len(readers), 1)
            readers[0].rollback()

        outcome = self.run_publisher(client_factory=ReaderBlocksCommit, sleep=wait)

        self.assertEqual(waits, [0.05])
        self.assertEqual((outcome.rounds, outcome.busy_errors), (2, 1))
        self.assert_published_once(attempts=2)

    def test_injected_busy_after_commit_confirms_without_publishing_again(self) -> None:
        # This is an injected post-commit response failure, not a claim that
        # ordinary sqlite3.commit() reports BUSY after committing its changes.
        tag = self.identity.tag
        instances = []
        waits = []

        class BusyAfterCommit(SQLiteFakeGitHub):
            armed = True

            def __init__(self, path: str | Path) -> None:
                super().__init__(path)
                instances.append(self)

            def _checkpoint_boundary(self, point: str) -> None:
                if point == "after_commit" and type(self).armed and self.mutations[-1:] == [("publish", tag)]:
                    type(self).armed = False
                    raise sqlite_error(sqlite3.SQLITE_BUSY, "Injected response failure after commit")

        def wait(delay: float) -> None:
            waits.append(delay)
            self.assert_published_once()
            with self.assertRaises(StateError):
                instances[0].get_release(tag)

        outcome = self.run_publisher(client_factory=BusyAfterCommit, sleep=wait)

        self.assertEqual(len(instances), 2)
        self.assertEqual(waits, [0.05])
        self.assertEqual((outcome.rounds, outcome.busy_errors), (2, 1))
        self.assert_published_once()

    def test_busy_during_final_snapshot_confirmation_is_retried(self) -> None:
        test = self
        holders = []
        waits = []

        class BusyConfirmation(SQLiteFakeGitHub):
            armed = True

            def assert_current(self) -> None:
                if type(self).armed:
                    type(self).armed = False
                    holders.append(test.lock("EXCLUSIVE"))
                super().assert_current()

        def wait(delay: float) -> None:
            waits.append(delay)
            self.assertEqual(len(holders), 1)
            holders[0].rollback()

        outcome = self.run_publisher(client_factory=BusyConfirmation, sleep=wait)

        self.assertEqual(waits, [0.05])
        self.assertEqual((outcome.rounds, outcome.busy_errors), (2, 1))
        self.assert_published_once()

    def test_extended_busy_code_is_classified_and_retried(self) -> None:
        connect = sqlite3.connect
        injected = sqlite_error(sqlite3.SQLITE_BUSY_SNAPSHOT, "An extended contention code")
        calls = 0

        def connect_once_busy(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise injected
            return connect(*args, **kwargs)

        waits = []
        with patch("scripts.release_persistence.sqlite3.connect", side_effect=connect_once_busy):
            outcome = self.run_publisher(sleep=waits.append)

        self.assertEqual(waits, [0.05])
        self.assertEqual((outcome.rounds, outcome.busy_errors), (2, 1))
        self.assert_published_once()

    def test_non_busy_codes_and_message_only_errors_are_not_retried(self) -> None:
        for label, code in (("IOERR", sqlite3.SQLITE_IOERR_WRITE), ("LOCKED", sqlite3.SQLITE_LOCKED), ("no_code", None)):
            with self.subTest(error=label):
                # Even this tempting message is insufficient without a BUSY code.
                injected = sqlite_error(code, "database is locked")
                factory = Mock(wraps=SQLiteFakeGitHub)
                sleeper = Mock()
                with patch("scripts.release_persistence.sqlite3.connect", side_effect=injected):
                    with self.assertRaises(StateError) as caught:
                        self.run_publisher(client_factory=factory, sleep=sleeper)

                self.assertNotIsInstance(caught.exception, StorageBusy)
                self.assertEqual(factory.call_count, 1)
                sleeper.assert_not_called()
                self.assertEqual(SQLiteFakeGitHub(self.path)._expected_payload, self.initial_payload)


if __name__ == "__main__":
    unittest.main()
