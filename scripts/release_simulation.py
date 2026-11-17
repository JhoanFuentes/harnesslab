"""Offline publication lesson: one asset, in-memory GitHub, no real adapter.

Verification records are authored test inputs, not proof of actual CI results.
The check names below are logical requirements, not existing Actions job IDs.
Sequential retries can resume matching drafts and reuse verified stored bytes.
They can also confirm an already published release after checking its bytes.
Concurrent SQLite callers use the bounded wrapper in release_concurrency.
This module alone does not coordinate concurrent clients or schedule retries.
The default fake uses memory; release_persistence adds local SQLite checkpoints.
Both implementations are teaching simulators, not real GitHub adapters.
It does not install wheels, call models, run gh, or publish anything externally.
"""

from dataclasses import dataclass, field
from hashlib import sha256


REQUIRED_CHECKS = ("contract_tests", "clean_install", "reproducibility")


@dataclass(frozen=True)
class ReleaseIdentity:
    tag: str
    source_commit: str
    asset_name: str
    sha256: str


@dataclass(frozen=True)
class Verification:
    identity: ReleaseIdentity
    checks: dict[str, str]


class VerificationFailed(ValueError):
    """The required verification or package identity does not match."""


class UploadResponseLost(TimeoutError):
    """Simulated lost acknowledgement after the fake server stored an asset."""


class PublishResponseLost(TimeoutError):
    """Simulated lost acknowledgement after the fake release became public."""


@dataclass
class FakeRelease:
    identity: ReleaseIdentity
    draft: bool = True
    assets: dict[str, bytes] = field(default_factory=dict)


class FakeGitHub:
    """Store simulated tags/releases and record mutations entirely in memory.

    This fake models storage operations. The decision to authorize publication
    belongs to publish_verified, not to this class.
    """

    def __init__(self, tags: dict[str, str]) -> None:
        self.tags = dict(tags)
        self.releases: dict[str, FakeRelease] = {}
        self.mutations: list[tuple[str, str]] = []
        self.upload_attempts = 0
        self.fail_after_upload_once = False
        self.publish_attempts = 0
        self.fail_after_publish_once = False

    def _ensure_available(self) -> None:
        """A persistent subclass can block use after a failed checkpoint."""

    def _checkpoint(self) -> None:
        """Memory-only by default; a subclass commits before returning."""

    def resolve_tag(self, tag: str) -> str | None:
        self._ensure_available()
        return self.tags.get(tag)

    def get_release(self, tag: str) -> FakeRelease | None:
        self._ensure_available()
        release = self.releases.get(tag)
        if release is None:
            return None
        # Return a snapshot: reading server state must not grant a mutable handle.
        return FakeRelease(
            identity=release.identity,
            draft=release.draft,
            assets=dict(release.assets),
        )

    def create_draft(self, identity: ReleaseIdentity) -> None:
        self._ensure_available()
        if identity.tag in self.releases:
            raise ValueError("Release already exists")
        self.releases[identity.tag] = FakeRelease(identity=identity)
        self.mutations.append(("create_draft", identity.tag))
        self._checkpoint()

    def upload_asset(self, tag: str, name: str, content: bytes) -> None:
        self._ensure_available()
        self.upload_attempts += 1
        self._checkpoint()
        release = self.releases[tag]
        if not release.draft:
            raise ValueError("Cannot upload to a published simulated release")
        if name in release.assets:
            raise ValueError("Asset already exists; refusing to overwrite")
        release.assets[name] = bytes(content)
        self.mutations.append(("upload_asset", tag))
        lose_response = self.fail_after_upload_once
        self.fail_after_upload_once = False
        self._checkpoint()
        if lose_response:
            raise UploadResponseLost("Asset stored, but upload response was lost")

    def download_asset(self, tag: str, name: str) -> bytes:
        self._ensure_available()
        return self.releases[tag].assets[name]

    def publish(self, tag: str) -> None:
        self._ensure_available()
        self.publish_attempts += 1
        self._checkpoint()
        release = self.releases[tag]
        if not release.draft:
            raise ValueError("Simulated release is already published")
        release.draft = False
        self.mutations.append(("publish", tag))
        lose_response = self.fail_after_publish_once
        self.fail_after_publish_once = False
        self._checkpoint()
        if lose_response:
            raise PublishResponseLost("Release published, but publish response was lost")


def publish_verified(
    github: FakeGitHub,
    identity: ReleaseIdentity,
    package: bytes,
    verification: Verification,
) -> None:
    """Publish one simulated asset only after checks and byte identity agree.

    The approved digest must come from the verification record; never replace
    it with a freshly calculated digest to make a changed package pass.
    An upload or publication timeout propagates to the caller. Calling it again
    rechecks the gates and queries the fake server without relying on old locals.
    A valid already published release returns normally without further writes.
    """
    if verification.identity != identity:
        raise VerificationFailed("Verification belongs to a different release identity")

    blocked = [name for name in REQUIRED_CHECKS if verification.checks.get(name) != "success"]
    if blocked:
        raise VerificationFailed("Required checks did not succeed: " + ", ".join(blocked))

    if sha256(package).hexdigest() != identity.sha256:
        raise VerificationFailed("Local package differs from the verified bytes")
    if github.resolve_tag(identity.tag) != identity.source_commit:
        raise VerificationFailed("Tag does not point to the verified commit")

    release = github.get_release(identity.tag)
    if release is None:
        github.create_draft(identity)
    else:
        if release.identity != identity:
            raise VerificationFailed("Existing release identity differs from the verified identity")
        if set(release.assets) - {identity.asset_name}:
            raise VerificationFailed("Unexpected assets in this single-asset release")
        if not release.draft and identity.asset_name not in release.assets:
            raise VerificationFailed("Published release is missing the verified asset")

    if release is None or identity.asset_name not in release.assets:
        github.upload_asset(identity.tag, identity.asset_name, package)
    # An existing filename is not proof of correct bytes; always download/check.
    uploaded = github.download_asset(identity.tag, identity.asset_name)
    if sha256(uploaded).hexdigest() != identity.sha256:
        raise VerificationFailed("Uploaded package differs from the verified bytes")
    if release is not None and not release.draft:
        # The previous attempt may have succeeded despite losing its response.
        # A normal return confirms the existing result, without publishing again.
        return
    github.publish(identity.tag)
