"""Offline upload/publication recovery; authored results, no real wheel."""

from dataclasses import replace
from hashlib import sha256
import unittest
from unittest.mock import patch

from scripts.release_simulation import (
    FakeGitHub,
    ReleaseIdentity,
    REQUIRED_CHECKS,
    Verification,
    VerificationFailed,
    publish_verified,
)


class ReleasePublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        # Fail loudly if these offline tests accidentally use sockets or a CLI.
        self.blocked_calls = []
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "subprocess.Popen"):
            blocker = patch(target, side_effect=AssertionError("Offline publication test"))
            self.blocked_calls.append(blocker.start())
            self.addCleanup(blocker.stop)

        # These are test bytes, NOT an installable wheel or measured CI evidence.
        self.package = b"HarnessLab publication fixture; not an installable wheel."
        self.identity = ReleaseIdentity(
            tag="v0.0.0-test",
            source_commit="a" * 40,
            asset_name="harnesslab-test.whl",
            sha256=sha256(self.package).hexdigest(),
        )
        self.verification = Verification(
            identity=self.identity,
            checks={name: "success" for name in REQUIRED_CHECKS},
        )
        self.github = FakeGitHub(tags={self.identity.tag: self.identity.source_commit})

    def tearDown(self) -> None:
        for blocked in self.blocked_calls:
            blocked.assert_not_called()

    def interrupt_after_upload(self) -> None:
        self.github.fail_after_upload_once = True
        with self.assertRaisesRegex(TimeoutError, "stored.*response"):
            publish_verified(self.github, self.identity, self.package, self.verification)

    def interrupt_after_publication(self) -> None:
        self.github.fail_after_publish_once = True
        with self.assertRaisesRegex(TimeoutError, "published.*response"):
            publish_verified(self.github, self.identity, self.package, self.verification)

    def assert_no_retry_writes(self, before: list[tuple[str, str]]) -> None:
        self.assertFalse(self.github.releases[self.identity.tag].draft)
        self.assertEqual(self.github.upload_attempts, 1)
        self.assertEqual(self.github.publish_attempts, 1)
        self.assertEqual(self.github.mutations, before)

    def test_success_publishes_the_verified_bytes(self) -> None:
        publish_verified(self.github, self.identity, self.package, self.verification)

        self.assertEqual(set(self.github.releases), {self.identity.tag})
        release = self.github.releases[self.identity.tag]
        self.assertEqual(release.identity, self.identity)
        self.assertFalse(release.draft)
        self.assertEqual(release.assets, {self.identity.asset_name: self.package})
        uploaded = self.github.download_asset(self.identity.tag, self.identity.asset_name)
        self.assertEqual(sha256(uploaded).hexdigest(), self.identity.sha256)
        self.assertEqual(self.github.mutations.count(("publish", self.identity.tag)), 1)

    def test_failed_verification_blocks_all_mutations(self) -> None:
        failed = replace(
            self.verification,
            checks={**self.verification.checks, "clean_install": "failure"},
        )

        with self.assertRaisesRegex(VerificationFailed, "clean_install"):
            publish_verified(self.github, self.identity, self.package, failed)

        self.assertEqual(self.github.releases, {})
        self.assertEqual(self.github.mutations, [])

    def test_retry_reuses_asset_after_upload_response_is_lost(self) -> None:
        self.interrupt_after_upload()

        # The server accepted the bytes, but the caller never got confirmation.
        draft = self.github.releases[self.identity.tag]
        self.assertTrue(draft.draft)
        self.assertEqual(draft.assets, {self.identity.asset_name: self.package})
        self.assertEqual(self.github.mutations, [
            ("create_draft", self.identity.tag),
            ("upload_asset", self.identity.tag),
        ])
        self.assertEqual(self.github.upload_attempts, 1)
        self.assertFalse(self.github.fail_after_upload_once)

        # Start the function again with reconstructed inputs, no saved locals.
        # Only the fake server keeps the release and the previously stored bytes.
        identity = replace(self.identity)
        verification = replace(self.verification, identity=identity, checks=dict(self.verification.checks))
        publish_verified(self.github, identity, self.package, verification)

        release = self.github.releases[identity.tag]
        self.assertEqual(set(self.github.releases), {identity.tag})
        self.assertEqual(release.identity, identity)
        self.assertFalse(release.draft)
        self.assertEqual(release.assets, {identity.asset_name: self.package})
        self.assertEqual(self.github.upload_attempts, 1)
        self.assertEqual(self.github.mutations, [
            ("create_draft", identity.tag),
            ("upload_asset", identity.tag),
            ("publish", identity.tag),
        ])

    def test_retry_rejects_corrupt_uploaded_bytes_without_overwriting(self) -> None:
        self.interrupt_after_upload()
        draft = self.github.releases[self.identity.tag]
        draft.assets[self.identity.asset_name] = b"corrupt bytes"
        before = list(self.github.mutations)

        with self.assertRaisesRegex(VerificationFailed, "Uploaded package"):
            publish_verified(self.github, self.identity, self.package, self.verification)

        self.assertTrue(draft.draft)
        self.assertEqual(draft.assets[self.identity.asset_name], b"corrupt bytes")
        self.assertEqual(self.github.upload_attempts, 1)
        self.assertEqual(self.github.mutations, before)

    def test_retry_rejects_another_draft_identity(self) -> None:
        self.interrupt_after_upload()
        draft = self.github.releases[self.identity.tag]
        draft.identity = replace(self.identity, source_commit="b" * 40)
        before = list(self.github.mutations)

        with self.assertRaisesRegex(VerificationFailed, "identity differs"):
            publish_verified(self.github, self.identity, self.package, self.verification)

        self.assertTrue(draft.draft)
        self.assertEqual(draft.assets, {self.identity.asset_name: self.package})
        self.assertEqual(self.github.upload_attempts, 1)
        self.assertEqual(self.github.mutations, before)

    def test_retry_rejects_unverified_extra_assets(self) -> None:
        self.interrupt_after_upload()
        draft = self.github.releases[self.identity.tag]
        draft.assets["unexpected.txt"] = b"not verified"
        before = list(self.github.mutations)

        with self.assertRaisesRegex(VerificationFailed, "Unexpected assets"):
            publish_verified(self.github, self.identity, self.package, self.verification)

        self.assertTrue(draft.draft)
        self.assertIn("unexpected.txt", draft.assets)
        self.assertEqual(self.github.upload_attempts, 1)
        self.assertEqual(self.github.mutations, before)

    def test_retry_confirms_publication_after_response_is_lost(self) -> None:
        self.interrupt_after_publication()

        # Publication happened before the response disappeared.
        release = self.github.releases[self.identity.tag]
        self.assertFalse(release.draft)
        self.assertEqual(release.assets, {self.identity.asset_name: self.package})
        self.assertFalse(self.github.fail_after_publish_once)
        before = list(self.github.mutations)
        self.assertEqual(before, [
            ("create_draft", self.identity.tag),
            ("upload_asset", self.identity.tag),
            ("publish", self.identity.tag),
        ])
        self.assert_no_retry_writes(before)

        # Re-enter with reconstructed inputs, keeping only the fake server state.
        identity = replace(self.identity)
        verification = replace(self.verification, identity=identity, checks=dict(self.verification.checks))
        publish_verified(self.github, identity, self.package, verification)

        self.assertEqual(set(self.github.releases), {identity.tag})
        self.assertEqual(release.identity, identity)
        self.assertEqual(release.assets, {identity.asset_name: self.package})
        self.assert_no_retry_writes(before)

    def test_retry_rejects_corrupt_published_bytes_without_repair(self) -> None:
        self.interrupt_after_publication()
        release = self.github.releases[self.identity.tag]
        release.assets[self.identity.asset_name] = b"corrupt published bytes"
        before = list(self.github.mutations)

        with self.assertRaisesRegex(VerificationFailed, "Uploaded package"):
            publish_verified(self.github, self.identity, self.package, self.verification)

        self.assertEqual(release.assets[self.identity.asset_name], b"corrupt published bytes")
        self.assert_no_retry_writes(before)

    def test_retry_rejects_another_published_identity(self) -> None:
        self.interrupt_after_publication()
        release = self.github.releases[self.identity.tag]
        release.identity = replace(self.identity, source_commit="b" * 40)
        before = list(self.github.mutations)

        with self.assertRaisesRegex(VerificationFailed, "identity differs"):
            publish_verified(self.github, self.identity, self.package, self.verification)

        self.assertEqual(release.identity.source_commit, "b" * 40)
        self.assert_no_retry_writes(before)

    def test_retry_rejects_missing_published_asset_without_uploading(self) -> None:
        self.interrupt_after_publication()
        release = self.github.releases[self.identity.tag]
        del release.assets[self.identity.asset_name]
        before = list(self.github.mutations)

        with self.assertRaisesRegex(VerificationFailed, "Published release is missing"):
            publish_verified(self.github, self.identity, self.package, self.verification)

        self.assertEqual(release.assets, {})
        self.assert_no_retry_writes(before)

    def test_retry_rejects_unverified_extra_published_assets(self) -> None:
        self.interrupt_after_publication()
        release = self.github.releases[self.identity.tag]
        release.assets["unexpected.txt"] = b"not verified"
        before = list(self.github.mutations)

        with self.assertRaisesRegex(VerificationFailed, "Unexpected assets"):
            publish_verified(self.github, self.identity, self.package, self.verification)

        self.assertIn("unexpected.txt", release.assets)
        self.assert_no_retry_writes(before)

    def test_retry_rechecks_verification_after_publication(self) -> None:
        self.interrupt_after_publication()
        before = list(self.github.mutations)
        failed = replace(
            self.verification,
            checks={**self.verification.checks, "clean_install": "failure"},
        )

        with self.assertRaisesRegex(VerificationFailed, "clean_install"):
            publish_verified(self.github, self.identity, self.package, failed)

        self.assert_no_retry_writes(before)


if __name__ == "__main__":
    unittest.main()
