"""SQLite checkpoints for the offline FakeGitHub lesson.

One JSON snapshot keeps state, receipts and attempt counters in the same commit.
Asset bytes use hex only for this small teaching fixture, not production wheels.
Cooperating publishers use conditional writes and release_concurrency's bounded
reload loop. Each publisher owns its own instance. This is not a power-loss
guarantee or a real GitHub adapter. Fault-injection flags are local
test controls and reset when reopening; they are not part of the saved state.
Snapshot counters record persisted attempts, not calls rejected by a conflict.
"""

from contextlib import closing
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

from scripts.release_simulation import FakeGitHub, FakeRelease, ReleaseIdentity


class StateError(RuntimeError):
    """The saved fake state is unavailable, invalid, or could not be confirmed."""


class StaleState(StateError):
    """Another publisher changed the snapshot; reopen and revalidate it."""


class StorageBusy(StateError):
    """SQLite reported BUSY; close, back off, then reopen the saved state."""


def _storage_error(error: BaseException, message: str) -> StateError:
    """Classify by SQLite code, never by messages such as 'database is locked'."""
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(error, StorageBusy) or (
        isinstance(error, sqlite3.Error) and type(code) is int
        and (code & 0xFF) == sqlite3.SQLITE_BUSY
    ):
        return StorageBusy(message)
    return StateError(message)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise StateError("Invalid FakeGitHub state: " + message)


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _decode(payload: str) -> tuple[dict, dict, list, int, int]:
    """Validate structure; publication approval remains publish_verified's job."""
    try:
        data = json.loads(payload)
        keys = {"schema_version", "tags", "releases", "mutations", "upload_attempts", "publish_attempts"}
        _require(isinstance(data, dict) and set(data) == keys, "snapshot fields")
        _require(type(data["schema_version"]) is int and data["schema_version"] == 1, "schema version")
        tags = data["tags"]
        _require(isinstance(tags, dict) and all(_text(k) and _text(v) for k, v in tags.items()), "tags")
        _require(isinstance(data["releases"], dict), "releases")
        releases = {}
        for tag, record in data["releases"].items():
            _require(_text(tag) and isinstance(record, dict), "release record")
            _require(set(record) == {"identity", "draft", "assets_hex"}, "release fields")
            identity = record["identity"]
            _require(isinstance(identity, dict), "release identity")
            _require(set(identity) == {"tag", "source_commit", "asset_name", "sha256"}, "identity fields")
            _require(all(_text(value) for value in identity.values()) and identity["tag"] == tag, "identity values")
            _require(type(record["draft"]) is bool and isinstance(record["assets_hex"], dict), "release state")
            assets = {}
            for name, encoded in record["assets_hex"].items():
                _require(_text(name) and isinstance(encoded, str), "asset encoding")
                content = bytes.fromhex(encoded)
                _require(content.hex() == encoded.lower(), "noncanonical asset encoding")
                assets[name] = content
            releases[tag] = FakeRelease(ReleaseIdentity(**identity), record["draft"], assets)
        _require(isinstance(data["mutations"], list), "mutation list")
        mutations = []
        for event in data["mutations"]:
            _require(isinstance(event, list) and len(event) == 2, "mutation record")
            _require(event[0] in ("create_draft", "upload_asset", "publish") and _text(event[1]), "mutation values")
            mutations.append(tuple(event))
        for name in ("upload_attempts", "publish_attempts"):
            _require(type(data[name]) is int and data[name] >= 0, name)
        return tags, releases, mutations, data["upload_attempts"], data["publish_attempts"]
    except (TypeError, ValueError) as error:
        raise StateError("Invalid FakeGitHub state: malformed JSON or asset bytes") from error


class SQLiteFakeGitHub(FakeGitHub):
    """Reopen an existing database; use create() explicitly for a new one."""

    def __init__(self, path: str | Path) -> None:
        super().__init__(tags={})
        self._path = Path(path).resolve()
        self._available = True
        try:
            # SQLite may need to roll back a hot journal before it can read.
            # mode=rw permits recovery but still refuses a missing database.
            with closing(self._connect(write=True)) as connection:
                rows = connection.execute("SELECT id, payload FROM fake_state").fetchall()
            _require(len(rows) == 1 and rows[0][0] == 1 and isinstance(rows[0][1], str), "snapshot row")
            state = _decode(rows[0][1])
        except (sqlite3.Error, OSError, StateError) as error:
            self._available = False
            raise _storage_error(error, "Cannot load FakeGitHub state; no empty state was created") from error
        self.tags, self.releases, self.mutations, self.upload_attempts, self.publish_attempts = state
        self._expected_payload = rows[0][1]

    @classmethod
    def create(cls, path: str | Path, tags: dict[str, str]) -> "SQLiteFakeGitHub":
        instance = cls.__new__(cls)
        FakeGitHub.__init__(instance, tags=tags)
        instance._path = Path(path).resolve()
        instance._available = True
        payload = instance._payload()
        _decode(payload)
        # Exclusive creation prevents accidentally replacing a previous history.
        with instance._path.open("xb"):
            pass
        try:
            with closing(instance._connect(write=True)) as connection:
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("CREATE TABLE fake_state (id INTEGER PRIMARY KEY CHECK(id = 1), payload TEXT NOT NULL)")
                connection.execute("INSERT INTO fake_state (id, payload) VALUES (1, ?)", (payload,))
                connection.commit()
                instance._expected_payload = payload
        except (sqlite3.Error, OSError) as error:
            instance._available = False
            raise _storage_error(error, "Cannot initialize FakeGitHub state; file was not reset") from error
        return instance

    @classmethod
    def open(cls, path: str | Path) -> "SQLiteFakeGitHub":
        return cls(path)

    def _connect(self, *, write: bool) -> sqlite3.Connection:
        mode = "rw" if write else "ro"
        # The wrapper owns the bounded wait policy; do not hide extra waits here.
        return sqlite3.connect(self._path.as_uri() + "?mode=" + mode, uri=True, timeout=0.0)

    def _payload(self) -> str:
        return json.dumps({
            "schema_version": 1,
            "tags": self.tags,
            "releases": {
                tag: {
                    "identity": asdict(release.identity),
                    "draft": release.draft,
                    "assets_hex": {name: content.hex() for name, content in release.assets.items()},
                }
                for tag, release in self.releases.items()
            },
            "mutations": self.mutations,
            "upload_attempts": self.upload_attempts,
            "publish_attempts": self.publish_attempts,
        }, sort_keys=True, separators=(",", ":"))

    def _ensure_available(self) -> None:
        if not self._available:
            raise StateError("Checkpoint outcome unavailable; reopen the saved state before continuing")

    def assert_current(self) -> None:
        """Confirm snapshot freshness even when publication needed no writes.

        This checks one instant; it cannot prohibit later changes by a writer.
        """
        self._ensure_available()
        try:
            with closing(self._connect(write=True)) as connection:
                rows = connection.execute("SELECT id, payload FROM fake_state").fetchall()
            _require(len(rows) == 1 and rows[0][0] == 1 and isinstance(rows[0][1], str), "snapshot row")
            if rows[0][1] != self._expected_payload:
                raise StaleState("Snapshot changed; reopen and revalidate before confirming")
        except StaleState:
            self._available = False
            raise
        except (sqlite3.Error, OSError, StateError) as error:
            self._available = False
            raise _storage_error(error, "Cannot confirm the current snapshot; reopen before continuing") from error

    def _checkpoint_boundary(self, point: str) -> None:
        """No-op test hook: before_commit follows UPDATE; after_commit follows commit.

        The restart worker overrides this to stop its own process at a selected
        publication checkpoint. Fault controls are never saved in SQLite.
        """

    def _checkpoint(self) -> None:
        self._ensure_available()
        try:
            payload = self._payload()
            _decode(payload)
            with closing(self._connect(write=True)) as connection:
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("BEGIN IMMEDIATE")
                # Compare and replace in the same write transaction. Locking
                # alone cannot make a snapshot loaded earlier current again.
                cursor = connection.execute(
                    "UPDATE fake_state SET payload = ? WHERE id = 1 AND payload = ?",
                    (payload, self._expected_payload),
                )
                if cursor.rowcount != 1:
                    raise StaleState("Snapshot changed; reopen and revalidate before writing")
                self._checkpoint_boundary("before_commit")
                connection.commit()
                self._expected_payload = payload
                self._checkpoint_boundary("after_commit")
        except StaleState:
            self._available = False
            raise
        except (sqlite3.Error, OSError, StateError, TypeError, ValueError, AttributeError) as error:
            # An error may occur before OR after commit. Reopen to discover which
            # state survived; never infer rollback solely from the exception.
            self._available = False
            raise _storage_error(error, "Cannot confirm FakeGitHub checkpoint; reopen before continuing") from error
        except BaseException:
            # Interrupts and unclassified failures can leave memory ahead of disk
            # too. Invalidate the instance without swallowing/retyping the error.
            self._available = False
            raise
