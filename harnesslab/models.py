"""Contracts shared by providers, runtime, and evaluators. No provider dependencies."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from typing import Any, Protocol


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Action:
    kind: str
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    diagnosis: str = ""
    evidence: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Action:
        if not isinstance(raw, dict) or set(raw) - set(cls.__dataclass_fields__):
            raise ValueError("Action must be an object with known fields")
        obj = cls(**raw)
        if obj.kind not in {"tool", "finish"}:
            raise ValueError("Action kind must be tool or finish")
        if not all(isinstance(v, str) for v in (obj.name, obj.diagnosis, obj.summary)):
            raise ValueError("Action text fields must be strings")
        if not isinstance(obj.arguments, dict) or not isinstance(obj.evidence, list):
            raise ValueError("Action arguments must be an object; evidence must be an array")
        if not all(isinstance(v, str) for v in obj.evidence):
            raise ValueError("Evidence IDs must be strings")
        if obj.kind == "tool" and (not obj.name or obj.diagnosis or obj.evidence or obj.summary):
            raise ValueError("Tool actions need a name and may not include a conclusion")
        if obj.kind == "finish" and (obj.name or obj.arguments or not obj.diagnosis):
            raise ValueError("Finish actions need a diagnosis and may not invoke tools")
        if len(canonical(raw)) > 16_000:
            raise ValueError("Action exceeds 16000 character limit")
        return obj


@dataclass(frozen=True)
class Decision:
    action: Action
    input_tokens: int = 0
    output_tokens: int = 0
    # User-configured rates; None means cost is unknown, never silently free.
    estimated_cost_usd: float | None = None
    # Subsets of input_tokens, never additional tokens. None means unreported.
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    response_id: str | None = None
    response_model: str | None = None
    cost_reason: str | None = None
    usage_incomplete: bool = False


class Provider(Protocol):
    name: str
    model: str

    def decide(self, context: dict[str, Any]) -> Decision: ...


@dataclass(frozen=True)
class Document:
    id: str
    title: str
    body: str
    current: bool = True
    kind: str = "telemetry"


@dataclass(frozen=True)
class Case:
    id: str
    question: str
    service: str
    documents: list[Document]
    expected_diagnosis: str
    required_evidence: list[str]
    expected_status: str = "completed"
    scripted_actions: list[Action] = field(default_factory=list)
    transient_failures: dict[str, int] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Case:
        data = dict(raw)
        data["documents"] = [Document(**d) for d in data["documents"]]
        data["scripted_actions"] = [Action.from_dict(a) for a in data.get("scripted_actions", [])]
        obj = cls(**data)
        if len({d.id for d in obj.documents}) != len(obj.documents):
            raise ValueError("Document IDs must be unique")
        return obj


@dataclass(frozen=True)
class Config:
    profile: str = "hardened"
    max_steps: int = 16
    max_model_calls: int = 20
    max_tokens: int = 24_000
    max_cost_usd: float | None = None
    context_chars: int = 12_000
    max_tool_retries: int = 2
    evidence_gate: bool = True
    approval_gate: bool = True

    def __post_init__(self) -> None:
        for name in ("max_steps", "max_model_calls", "max_tokens", "context_chars"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_tool_retries < 0 or self.max_tool_retries > 10:
            raise ValueError("max_tool_retries must be between 0 and 10")
        if self.context_chars < 2_000:
            raise ValueError("context_chars must be at least 2000")
        if self.max_cost_usd is not None and (not 0 < self.max_cost_usd < float("inf")):
            raise ValueError("max_cost_usd must be finite and positive")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def named(cls, name: str) -> Config:
        if name == "baseline":
            return cls(profile=name, evidence_gate=False, approval_gate=False, max_tool_retries=0)
        if name == "no-evidence-gate":
            return cls(profile=name, evidence_gate=False)
        if name == "no-retries":
            return cls(profile=name, max_tool_retries=0)
        if name != "hardened":
            raise ValueError(f"Unknown profile: {name}")
        return cls()
