"""Offline child-process fixture, not a release CLI or a live GitHub adapter."""

from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.release_persistence import SQLiteFakeGitHub
from scripts.release_simulation import (
    PublishResponseLost, ReleaseIdentity, REQUIRED_CHECKS, Verification, publish_verified,
)


def inputs() -> tuple[ReleaseIdentity, bytes, Verification]:
    # Approval is reconstructed independently of the saved fake-server state.
    # Enough pages to spill from the crash worker's deliberately tiny cache.
    package = b"HarnessLab restart fixture; not an installable wheel.\n" * 4096
    identity = ReleaseIdentity("v0.0.0-restart-test", "a" * 40, "fixture.whl", sha256(package).hexdigest())
    return identity, package, Verification(identity, {name: "success" for name in REQUIRED_CHECKS})


def summary(github: SQLiteFakeGitHub, identity: ReleaseIdentity) -> dict:
    release = github.get_release(identity.tag)
    return {
        "release_count": len(github.releases),
        "draft": release.draft if release else None,
        "assets": sorted(release.assets) if release else [],
        "asset_sha256": sha256(github.download_asset(identity.tag, identity.asset_name)).hexdigest()
        if release and identity.asset_name in release.assets else None,
        "upload_attempts": github.upload_attempts,
        "publish_attempts": github.publish_attempts,
        "mutations": [list(event) for event in github.mutations],
    }


def main() -> None:
    phase, state_path = sys.argv[1:]
    identity, package, verification = inputs()
    if phase in ("crash-before-commit", "crash-after-commit"):
        target = "before_commit" if phase == "crash-before-commit" else "after_commit"
        exit_code = 31 if target == "before_commit" else 32

        class CommitCrashGitHub(SQLiteFakeGitHub):
            def _connect(self, *, write: bool):
                connection = super()._connect(write=write)
                if write:
                    # Exercise on-disk rollback, not just loss of cached pages.
                    connection.execute("PRAGMA journal_mode = DELETE")
                    connection.execute("PRAGMA cache_size = 2")
                    connection.execute("PRAGMA cache_spill = ON")
                return connection

            def _checkpoint_boundary(self, point: str) -> None:
                release = self.releases.get(identity.tag)
                publication_pending = (
                    release is not None and not release.draft
                    and self.mutations[-1:] == [("publish", identity.tag)]
                )
                if point == target and publication_pending:
                    print(json.dumps({"phase": phase, "boundary": point,
                                      "pid": os.getpid()}), flush=True)
                    # Exit inside the transaction/connection scope: no unwind.
                    os._exit(exit_code)

        github = CommitCrashGitHub.create(Path(state_path), tags={identity.tag: identity.source_commit})
        publish_verified(github, identity, package, verification)
        raise AssertionError("Expected interruption at the publication commit boundary")
    if phase == "crash":
        github = SQLiteFakeGitHub.create(Path(state_path), tags={identity.tag: identity.source_commit})
        github.fail_after_publish_once = True
        try:
            publish_verified(github, identity, package, verification)
        except PublishResponseLost:
            print(json.dumps({"phase": "interrupted", "pid": os.getpid()}), flush=True)
            # No finally, atexit, destructor, or explicit save may rescue this run.
            os._exit(23)
        raise AssertionError("Expected the response-loss fault")
    if phase == "recover":
        github = SQLiteFakeGitHub(Path(state_path))
        before = summary(github, identity)
        publish_verified(github, identity, package, verification)
        print(json.dumps({"phase": "confirmed", "pid": os.getpid(),
                          "before": before, "after": summary(github, identity)}))
        return
    raise ValueError("Expected crash, crash-before-commit, crash-after-commit, or recover")


if __name__ == "__main__":
    # Only the parent test may launch Python. The child cannot call a CLI/network.
    with (
        patch("socket.socket.connect", side_effect=AssertionError("Offline worker")),
        patch("socket.socket.connect_ex", side_effect=AssertionError("Offline worker")),
        patch("subprocess.Popen", side_effect=AssertionError("Offline worker")),
    ):
        main()
