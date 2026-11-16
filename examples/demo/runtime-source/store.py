"""SQLite checkpoints, append-only-by-API audit log, and local effect receipts."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid
from typing import Any, Iterator

from .models import Case, Config, canonical, digest


class ConflictError(RuntimeError):
    """Another writer changed this run. Reload rather than overwrite."""


class Store:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, version INTEGER NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                run_id TEXT NOT NULL REFERENCES runs(id), seq INTEGER NOT NULL,
                payload TEXT NOT NULL, hash TEXT NOT NULL,
                PRIMARY KEY (run_id, seq));
            CREATE TABLE IF NOT EXISTS receipts (
                run_id TEXT NOT NULL REFERENCES runs(id), action_id TEXT NOT NULL,
                result TEXT NOT NULL, PRIMARY KEY (run_id, action_id));
            CREATE TABLE IF NOT EXISTS effects (
                run_id TEXT NOT NULL REFERENCES runs(id), action_id TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY (run_id, action_id));
        """)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def create(self, case: Case, config: Config, provider: str, model: str,
               provider_settings: dict[str, Any] | None = None) -> str:
        run_id = uuid.uuid4().hex[:16]
        state = {
            "run_id": run_id, "version": 0, "status": "running",
            "case": case.to_dict(), "config": config.to_dict(),
            "provider": provider, "model": model,
            "provider_settings": provider_settings or {},
            "manifest_hash": digest({"case": case.to_dict(), "config": config.to_dict(),
                                     "provider": provider, "model": model,
                                     "provider_settings": provider_settings or {}}),
            "steps": 0, "model_calls": 0, "decision_index": 0,
            "input_tokens": 0, "output_tokens": 0,
            "estimated_cost_usd": 0.0 if provider == "scripted" else None,
            "reported_cost_usd": 0.0, "usage_incomplete": False, "request_in_flight": False,
            "observed": {}, "observations": [], "feedback": [],
            "pending_action": None, "action_id": None, "approval_id": None,
            "approved_actions": [], "tool_attempts": {}, "fault_counts": {},
            "result": None,
        }
        with self.transaction():
            self.db.execute("INSERT INTO runs VALUES (?, ?, ?)", (run_id, 0, canonical(state)))
            self._event(state, "run_started", {"manifest_hash": state["manifest_hash"]})
        return run_id

    def get(self, run_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown run: {run_id}")
        return json.loads(row["state"])

    def _event(self, state: dict[str, Any], kind: str, data: dict[str, Any]) -> None:
        last = self.db.execute("SELECT seq, hash FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 1",
                               (state["run_id"],)).fetchone()
        payload = {
            "run_id": state["run_id"], "seq": 0 if last is None else last["seq"] + 1,
            "type": kind, "timestamp": datetime.now(timezone.utc).isoformat(),
            "previous_hash": "0" * 64 if last is None else last["hash"],
            "data": data, "state": state,
        }
        self.db.execute("INSERT INTO events VALUES (?, ?, ?, ?)",
                        (state["run_id"], payload["seq"], canonical(payload), digest(payload)))

    def _save(self, state: dict[str, Any], kind: str, data: dict[str, Any]) -> None:
        expected = state["version"]
        snapshot = dict(state, version=expected + 1)
        cursor = self.db.execute("UPDATE runs SET state=?, version=? WHERE id=? AND version=?",
                                 (canonical(snapshot), snapshot["version"], state["run_id"], expected))
        if cursor.rowcount != 1:
            raise ConflictError("Run changed concurrently; use one worker per run")
        self._event(snapshot, kind, data)
        state["version"] = snapshot["version"]

    def save(self, state: dict[str, Any], kind: str, data: dict[str, Any] | None = None) -> None:
        version = state["version"]
        try:
            with self.transaction():
                self._save(state, kind, data or {})
        except BaseException:
            state["version"] = version
            raise

    def receipt(self, run_id: str, action_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT result FROM receipts WHERE run_id=? AND action_id=?",
                              (run_id, action_id)).fetchone()
        return json.loads(row["result"]) if row else None

    def commit_tool(self, state: dict[str, Any], result: dict[str, Any],
                    effect: dict[str, Any] | None = None) -> dict[str, Any]:
        """Receipt, local effect, checkpoint, and audit event share one transaction."""
        with self.transaction():
            cached = self.receipt(state["run_id"], state["action_id"])
            if cached is not None:
                return cached
            self.db.execute("INSERT INTO receipts VALUES (?, ?, ?)",
                            (state["run_id"], state["action_id"], canonical(result)))
            if effect is not None:
                self.db.execute("INSERT INTO effects VALUES (?, ?, ?)",
                                (state["run_id"], state["action_id"], canonical(effect)))
            self._save(state, "tool_committed", {"action_id": state["action_id"], "result": result})
        return result

    def events(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT payload, hash FROM events WHERE run_id=? ORDER BY seq", (run_id,))
        return [dict(json.loads(row["payload"]), hash=row["hash"]) for row in rows]

    def effects(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(json.loads(r["payload"]), action_id=r["action_id"])
                for r in self.db.execute("SELECT action_id, payload FROM effects WHERE run_id=?", (run_id,))]

    def replay(self, run_id: str) -> dict[str, Any]:
        """Verify and reconstruct saved checkpoints. Does not call models or tools."""
        events = self.events(run_id)
        if not events:
            raise ValueError("Run has no events")
        previous = "0" * 64
        for seq, event in enumerate(events):
            payload = {k: v for k, v in event.items() if k != "hash"}
            if (event.get("seq") != seq or event.get("previous_hash") != previous or
                    "state" not in event or digest(payload) != event["hash"]):
                raise ValueError(f"Audit chain mismatch at event {seq}")
            previous = event["hash"]
        state = events[-1]["state"]
        if canonical(state) != canonical(self.get(run_id)):
            raise ValueError("Checkpoint differs from audit log")
        return state
