"""Integrity and information-boundary checks; no live model or network calls."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import tempfile
import unicodedata
import unittest

from harnesslab.engine import Harness, build_context
from harnesslab.models import Action, Case, Config, canonical
from harnesslab.store import Store


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "datasets" / "live-v1"


def normalize_label(value: str) -> str:
    """The declared data contract, independently of any study implementation."""
    return re.sub(r"[\s-]+", "_", unicodedata.normalize("NFKC", value).casefold().strip())


class NoModelCalls:
    name = "offline-integrity-check"
    model = "no-model"

    def decide(self, context):
        raise AssertionError("Dataset integrity checks must never call a model")


class LiveDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((DATASET / "manifest.json").read_text(encoding="utf-8"))
        cls.raw = {split: json.loads((DATASET / f"{split}.json").read_text(encoding="utf-8"))
                   for split in ("dev", "test")}
        cls.cases = {split: [Case.from_dict(item) for item in rows]
                     for split, rows in cls.raw.items()}

    def test_frozen_dataset_hashes_and_declared_counts(self):
        self.assertEqual(self.manifest["dataset_id"], "harnesslab-live-v1")
        self.assertEqual(set(self.manifest["files"]), {"dev.json", "test.json"})
        for split, count in (("dev", 4), ("test", 12)):
            with self.subTest(split=split):
                entry = self.manifest["files"][f"{split}.json"]
                raw = (DATASET / f"{split}.json").read_bytes()
                self.assertNotIn(b"\r", raw)
                self.assertEqual(entry["sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(entry["case_count"], count)
                self.assertEqual(len(self.raw[split]), count)

    def test_case_document_and_service_identities_are_disjoint(self):
        identifiers, documents, services = [], [], []
        for split, cases in self.cases.items():
            for case in cases:
                self.assertTrue(case.id.startswith(f"live-{split}-"))
                identifiers.append(case.id)
                services.append(case.service)
                documents.extend(doc.id for doc in case.documents)
        for values in (identifiers, documents, services):
            self.assertEqual(len(values), len(set(values)))

    def test_manifest_metadata_matches_cases_without_becoming_case_fields(self):
        metadata = {item["id"]: item for item in self.manifest["cases"]}
        self.assertEqual(len(metadata), 16)
        for split, rows in self.raw.items():
            for item in rows:
                self.assertNotIn("scripted_actions", item)
                self.assertNotIn("diagnosis_aliases", item)
                self.assertNotIn("normalization", item)
                self.assertEqual(set(item) - set(Case.__dataclass_fields__), set())
                self.assertEqual(metadata[item["id"]], {
                    "id": item["id"], "split": split, "tags": item["tags"],
                    "expected_diagnosis": item["expected_diagnosis"],
                    "expected_status": item["expected_status"],
                })
                self.assertEqual(Case.from_dict(item).scripted_actions, [])

    def test_aliases_are_fixed_unambiguous_and_cover_all_labels(self):
        aliases = self.manifest["diagnosis_aliases"]
        expected = {case.expected_diagnosis for cases in self.cases.values() for case in cases}
        self.assertEqual(set(aliases), expected)
        seen = {}
        for label, values in aliases.items():
            self.assertIn(label, values)
            self.assertEqual(len(values), len(set(values)))
            normalized = [normalize_label(value) for value in values]
            self.assertEqual(len(normalized), len(set(normalized)))
            for value in normalized:
                self.assertEqual(seen.setdefault(value, label), label)
        self.assertEqual(aliases["insufficient_evidence"], ["insufficient_evidence"])

    def test_no_expected_label_or_alias_is_given_in_model_visible_documents(self):
        for cases in self.cases.values():
            for case in cases:
                visible = " ".join([case.question, case.service,
                                    *(part for doc in case.documents for part in (doc.id, doc.title, doc.body))])
                text = "_" + normalize_label(visible) + "_"
                for alias in self.manifest["diagnosis_aliases"][case.expected_diagnosis]:
                    with self.subTest(case=case.id, alias=alias):
                        self.assertNotIn("_" + normalize_label(alias) + "_", text)

    def test_completed_cases_have_satisfiable_current_evidence_contracts(self):
        for cases in self.cases.values():
            for case in cases:
                if case.expected_status != "completed":
                    continue
                with self.subTest(case=case.id):
                    docs = {doc.id: doc for doc in case.documents}
                    self.assertEqual(len(case.required_evidence), 2)
                    required = [docs[ref] for ref in case.required_evidence]
                    self.assertTrue(all(doc.current for doc in required))
                    self.assertEqual({doc.kind for doc in required}, {"telemetry", "runbook"})
                    state = {"observed": {doc.id: doc.__dict__ for doc in required}}
                    action = Action("finish", diagnosis=case.expected_diagnosis,
                                    evidence=case.required_evidence)
                    self.assertIsNone(Harness._check_evidence(state, action))

    def test_abstentions_have_a_real_missing_current_evidence_class(self):
        statuses = {split: Counter(case.expected_status for case in cases)
                    for split, cases in self.cases.items()}
        self.assertEqual(statuses["dev"], {"completed": 3, "insufficient_evidence": 1})
        self.assertEqual(statuses["test"], {"completed": 10, "insufficient_evidence": 2})
        for cases in self.cases.values():
            for case in cases:
                if case.expected_status == "insufficient_evidence":
                    self.assertEqual(case.expected_diagnosis, "insufficient_evidence")
                    self.assertEqual(case.required_evidence, [])
                    kinds = {doc.kind for doc in case.documents if doc.current}
                    self.assertFalse({"telemetry", "runbook"}.issubset(kinds))

    def test_attacks_require_task_completion_and_tool_faults_are_bounded(self):
        attacks = [case for case in self.cases["test"] if "prompt-injection" in case.tags]
        self.assertEqual(len(attacks), 2)
        for case in attacks:
            self.assertEqual(case.expected_status, "completed")
            self.assertTrue(case.required_evidence)
        failures = [case.transient_failures for cases in self.cases.values()
                    for case in cases if case.transient_failures]
        self.assertEqual(failures, [{"search": 1}])

    def test_service_search_exposes_documents_without_evaluator_fields(self):
        hidden = {"expected_diagnosis", "expected_status", "required_evidence",
                  "scripted_actions", "diagnosis_aliases", "tags"}
        for cases in self.cases.values():
            for case in cases:
                with self.subTest(case=case.id):
                    result, effect = Harness._execute({"case": case.to_dict()},
                                                     Action("tool", "search", {"query": case.service}))
                    self.assertIsNone(effect)
                    self.assertEqual({item["id"] for item in result["matches"]},
                                     {doc.id for doc in case.documents})
                    for item in result["matches"]:
                        self.assertFalse(hidden.intersection(item))

    def test_context_excludes_oracles_before_and_after_reading_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "checks.sqlite")
            try:
                harness = Harness(store, NoModelCalls())
                for cases in self.cases.values():
                    for original in cases:
                        with self.subTest(case=original.id):
                            case = replace(original, expected_diagnosis="HIDDEN_EXPECTED_LABEL",
                                           required_evidence=["HIDDEN_REQUIRED_REFERENCE"],
                                           tags=["HIDDEN_EVALUATOR_TAG"],
                                           scripted_actions=[Action("finish", diagnosis="HIDDEN_SCRIPT")])
                            run_id = harness.start(case, Config())
                            state = store.get(run_id)
                            for read_documents in (False, True):
                                if read_documents:
                                    for doc in case.documents:
                                        result, effect = Harness._execute(state, Action(
                                            "tool", "read_document", {"document_id": doc.id}))
                                        self.assertIsNone(effect)
                                        state["observed"][doc.id] = result["document"]
                                        state["observations"].append({"tool": "read_document", "result": result,
                                                                      "trust": "untrusted_tool_data"})
                                context = build_context(state)
                                rendered = canonical(context)
                                for sentinel in ("HIDDEN_EXPECTED_LABEL", "HIDDEN_REQUIRED_REFERENCE",
                                                 "HIDDEN_EVALUATOR_TAG", "HIDDEN_SCRIPT"):
                                    self.assertNotIn(sentinel, rendered)
                                self.assertLessEqual(len(rendered), state["config"]["context_chars"])
                                if read_documents:
                                    self.assertEqual(context["omitted_observations"], 0)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
