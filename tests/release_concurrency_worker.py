"""Cooperating local publisher process, never a real GitHub client.

Process-local witnesses count publish() calls and committed publication effects.
The SQLite attempt counter counts persisted attempts: a stale process can enter
publish() and fail its counter checkpoint, so that call is absent from SQLite.
"""

from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))
sys.path.insert(0, str(TESTS))

from scripts.release_concurrency import publish_with_retry
from scripts.release_persistence import SQLiteFakeGitHub
from release_restart_worker import inputs, summary


def emit(event: dict) -> None:
    print(json.dumps({"pid": os.getpid(), **event}), flush=True)


def main() -> None:
    state_path = Path(sys.argv[1])
    identity, package, verification = inputs()

    class CoordinatedGitHub(SQLiteFakeGitHub):
        ready_sent = False
        publish_calls = 0
        committed_publications = 0

        def __init__(self, path: str | Path) -> None:
            super().__init__(path)
            if not type(self).ready_sent:
                type(self).ready_sent = True
                emit({"event": "ready", "snapshot_sha256": sha256(self._expected_payload.encode()).hexdigest()})
                # This is the snapshot actually used by round one, before BEGIN.
                if sys.stdin.readline().strip() != "GO":
                    raise RuntimeError("Expected GO from the parent test")

        def publish(self, tag: str) -> None:
            type(self).publish_calls += 1
            super().publish(tag)

        def _checkpoint(self) -> None:
            old = json.loads(self._expected_payload)
            self._previous_publications = old["mutations"].count(["publish", identity.tag])
            super()._checkpoint()

        def _checkpoint_boundary(self, point: str) -> None:
            if point == "after_commit":
                # The base updates _expected_payload only after a successful commit.
                committed = json.loads(self._expected_payload)
                delta = committed["mutations"].count(["publish", identity.tag]) - self._previous_publications
                if delta:
                    type(self).committed_publications += delta
                    emit({"event": "publication_committed", "count": delta})

    outcome = publish_with_retry(
        state_path, identity, package, verification,
        max_rounds=4, client_factory=CoordinatedGitHub,
    )
    emit({
        "event": "done",
        "rounds": outcome.rounds,
        "conflicts": outcome.conflicts,
        "publish_calls": CoordinatedGitHub.publish_calls,
        "committed_publications": CoordinatedGitHub.committed_publications,
        "state": summary(SQLiteFakeGitHub(state_path), identity),
    })


if __name__ == "__main__":
    with (
        patch("socket.socket.connect", side_effect=AssertionError("Offline worker")),
        patch("socket.socket.connect_ex", side_effect=AssertionError("Offline worker")),
        patch("subprocess.Popen", side_effect=AssertionError("Offline worker")),
    ):
        main()
