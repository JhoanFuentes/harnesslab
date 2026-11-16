"""Durability, transaction, and corruption checks for the local store."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harnesslab.cases import get_case
from harnesslab.models import Config, canonical
from harnesslab.store import ConflictError, Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "runs.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.run_id = self.store.create(get_case("pool-saturation"), Config(), "scripted", "scripted-v1")

    def test_stale_writer_cannot_overwrite_newer_checkpoint(self):
        other = Store(self.path)
        self.addCleanup(other.close)
        first = self.store.get(self.run_id)
        stale = other.get(self.run_id)
        first["steps"] = 1
        self.store.save(first, "progress")
        stale["steps"] = 99
        with self.assertRaises(ConflictError):
            other.save(stale, "stale_progress")
        self.assertEqual(self.store.replay(self.run_id)["steps"], 1)
        self.assertEqual(len(self.store.events(self.run_id)), 2)
        self.assertEqual(stale["version"], 0)

    def test_receipt_and_effect_commit_only_once(self):
        state = self.store.get(self.run_id)
        state["action_id"] = "test-action"
        result = {"ok": True}
        effect = {"service": "checkout", "simulation": True, "approved": True}
        self.store.commit_tool(state, result, effect)
        self.store.commit_tool(state, {"ok": False}, effect)
        self.assertEqual(self.store.receipt(self.run_id, "test-action"), result)
        self.assertEqual(len(self.store.effects(self.run_id)), 1)
        self.assertEqual(len(self.store.events(self.run_id)), 2)
        self.assertEqual(self.store.replay(self.run_id), state)

    def test_receipt_and_effect_roll_back_on_stale_checkpoint(self):
        state = self.store.get(self.run_id)
        stale = self.store.get(self.run_id)
        self.store.save(state, "new_checkpoint")
        stale["action_id"] = "uncommitted-action"
        with self.assertRaises(ConflictError):
            self.store.commit_tool(stale, {"ok": True}, {"simulation": True})
        self.assertIsNone(self.store.receipt(self.run_id, "uncommitted-action"))
        self.assertEqual(self.store.effects(self.run_id), [])
        self.assertEqual(len(self.store.events(self.run_id)), 2)

    def test_failed_event_rolls_back_checkpoint_and_caller_version(self):
        state = self.store.get(self.run_id)
        with patch.object(self.store, "_event", side_effect=RuntimeError("simulated disk failure")):
            with self.assertRaises(RuntimeError):
                self.store.save(state, "will_fail")
        self.assertEqual(state["version"], 0)
        self.assertEqual(self.store.get(self.run_id)["version"], 0)
        self.store.save(state, "retry_after_recovery")
        self.assertEqual(self.store.replay(self.run_id)["version"], 1)

    def test_modified_event_payload_is_detected(self):
        self.store.db.execute("UPDATE events SET payload=? WHERE run_id=? AND seq=0",
                              (canonical({"forged": True}), self.run_id))
        with self.assertRaises((ValueError, KeyError)):
            self.store.replay(self.run_id)

    def test_modified_event_hash_is_detected(self):
        self.store.db.execute("UPDATE events SET hash=? WHERE run_id=? AND seq=0", ("f" * 64, self.run_id))
        with self.assertRaises(ValueError):
            self.store.replay(self.run_id)

    def test_checkpoint_tampering_is_detected(self):
        state = self.store.get(self.run_id)
        state["status"] = "completed"
        self.store.db.execute("UPDATE runs SET state=? WHERE id=?", (canonical(state), self.run_id))
        with self.assertRaisesRegex(ValueError, "Checkpoint"):
            self.store.replay(self.run_id)

    def test_missing_middle_event_is_detected(self):
        state = self.store.get(self.run_id)
        self.store.save(state, "first")
        self.store.save(state, "second")
        self.store.db.execute("DELETE FROM events WHERE run_id=? AND seq=1", (self.run_id,))
        with self.assertRaises(ValueError):
            self.store.replay(self.run_id)

    def test_replay_uses_no_provider_or_tool_execution(self):
        state = self.store.replay(self.run_id)
        self.assertEqual(state["model_calls"], 0)
        self.assertEqual(self.store.effects(self.run_id), [])
        self.assertEqual(len(self.store.events(self.run_id)), 1)


if __name__ == "__main__":
    unittest.main()
