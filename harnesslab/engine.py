"""Durable agent loop. Policy and completion checks live outside the model."""

from __future__ import annotations

import math
import re
from typing import Any

from .models import Action, Case, Config, Provider, canonical, digest
from .store import Store


class InjectedCrash(RuntimeError):
    """A controlled crash after durable tool commit, before consuming its result."""


TOOLS = {
    "search": {"query": str},
    "read_document": {"document_id": str},
    "restart_service": {"service": str},
}


def build_context(state: dict[str, Any]) -> dict[str, Any]:
    """Bounded model projection; the durable log remains lossless.

    Oracle labels and scripted trajectories are intentionally absent. Keep evidence
    IDs and freshness metadata, then fit recent observations into the byte budget.
    A char bound is explicit; this is not advertised as tokenizer-precise compaction.
    """
    limit = state["config"]["context_chars"]
    context: dict[str, Any] = {
        "task": {"question": state["case"]["question"], "service": state["case"]["service"][:200]},
        "decision_index": state["decision_index"],
        "evidence": [{"id": doc_id, "current": doc["current"], "kind": doc["kind"]}
                     for doc_id, doc in state["observed"].items()],
        "observations": [], "feedback": state["feedback"][-3:],
        "remaining_output_tokens": max(0, state["config"]["max_tokens"] - state["input_tokens"] - state["output_tokens"]),
        "tools": {
            "search": "query: string; search incident documents",
            "read_document": "document_id: string; get a full document by ID",
            "restart_service": "service: string; simulated local effect, needs operator approval",
        },
        "omitted_observations": len(state["observations"]),
    }
    # Trimming is deterministic and visible. Large document content stays available
    # in SQLite; the model can re-read by stable ID (output itself is bounded).
    context["task"]["question"] = context["task"]["question"][:1000]
    while len(canonical(context)) > limit and context["evidence"]:
        context["evidence"].pop(0)
    while len(canonical(context)) > limit and context["feedback"]:
        context["feedback"].pop(0)
    for observation in reversed(state["observations"]):
        candidate = dict(observation)
        context["observations"].insert(0, candidate)
        context["omitted_observations"] -= 1
        if len(canonical(context)) > limit:
            context["observations"].pop(0)
            context["omitted_observations"] += 1
            # Try a clipped representation of the newest observation, rather than
            # silently omit its existence. It is explicitly marked as incomplete.
            if not context["observations"]:
                available = max(0, limit - len(canonical(context)) - 250)
                context["observations"] = [{"tool": observation["tool"], "truncated": True,
                                             "content_preview": canonical(observation)[:available]}]
                while len(canonical(context)) > limit and context["observations"][0]["content_preview"]:
                    context["observations"][0]["content_preview"] = context["observations"][0]["content_preview"][:-100]
            break
    if len(canonical(context)) > limit:
        # Arbitrary user supplied strings can expand when JSON-escaped. Preserve
        # the limit even for pathological fixture contents.
        context["observations"] = []
        context["feedback"] = []
        context["evidence"] = []
        while len(canonical(context)) > limit and context["task"]["question"]:
            context["task"]["question"] = context["task"]["question"][:-100]
        while len(canonical(context)) > limit and context["task"]["service"]:
            context["task"]["service"] = context["task"]["service"][:-50]
    return context


class Harness:
    def __init__(self, store: Store, provider: Provider):
        self.store = store
        self.provider = provider

    def _provider_settings(self) -> dict[str, Any]:
        from .providers import SYSTEM_PROMPT
        return {key: getattr(self.provider, key, None) for key in (
            "input_rate_per_million", "output_rate_per_million", "cached_input_rate_per_million",
            "max_output_tokens", "timeout")
        } | {"prompt_hash": digest(SYSTEM_PROMPT) if self.provider.name == "openai" else None}

    def start(self, case: Case, config: Config | None = None) -> str:
        config = config or Config()
        if config.max_cost_usd is not None and self.provider.name != "scripted":
            if (getattr(self.provider, "input_rate_per_million", None) is None or
                    getattr(self.provider, "output_rate_per_million", None) is None):
                raise ValueError("A cost budget requires explicit input and output token prices")
        return self.store.create(case, config, self.provider.name, self.provider.model, self._provider_settings())

    def _stop(self, state: dict[str, Any], status: str, reason: str) -> dict[str, Any]:
        state["status"] = status
        self.store.save(state, "run_stopped", {"reason": reason})
        return state

    def run(self, run_id: str, *, crash_after_tool: bool = False) -> dict[str, Any]:
        state = self.store.replay(run_id)
        if state["provider"] != self.provider.name or state["model"] != self.provider.model:
            raise ValueError("Resume requires the original provider and model")
        original_settings = dict(state.get("provider_settings", {}))
        if original_settings:
            # Older snapshots did not price cache reads separately. An omitted
            # value is compatible only with the same conservative full rate.
            original_settings.setdefault("cached_input_rate_per_million", None)
        if original_settings and original_settings != self._provider_settings():
            raise ValueError("Resume requires the original provider settings and prompt version")
        if state["status"] == "running" and state.get("request_in_flight"):
            state["request_in_flight"] = False
            self._account(state, 0, 0, None, usage_incomplete=True)
            state["last_model_call"] = {"call_index": state["model_calls"], "status": "interrupted",
                                        **self._usage_metadata(None, failed=True)}
            return self._stop(state, "provider_error", "interrupted_model_request_usage_unknown")
        config = Config(**state["config"])
        while state["status"] == "running":
            if state["steps"] >= config.max_steps:
                return self._stop(state, "budget_exhausted", "step_limit")
            # Re-check after every resume, including the window immediately after
            # model_decided was committed but before the original budget check.
            if state["input_tokens"] + state["output_tokens"] > config.max_tokens:
                return self._stop(state, "budget_exhausted", "token_limit_after_request")
            if (config.max_cost_usd is not None and state["estimated_cost_usd"] is not None and
                    state["estimated_cost_usd"] > config.max_cost_usd):
                return self._stop(state, "budget_exhausted", "cost_limit_after_request")
            # A committed decision is completed after restart without paying for
            # another model call. Tool receipts keep local effects idempotent.
            if state["pending_action"] is None:
                if state["model_calls"] >= config.max_model_calls:
                    return self._stop(state, "budget_exhausted", "model_call_limit")
                if state["input_tokens"] + state["output_tokens"] >= config.max_tokens:
                    return self._stop(state, "budget_exhausted", "token_limit")
                if (config.max_cost_usd is not None and state["estimated_cost_usd"] is not None and
                        state["estimated_cost_usd"] >= config.max_cost_usd):
                    return self._stop(state, "budget_exhausted", "cost_limit")
                context = build_context(state)
                if self.provider.name == "openai" and context["remaining_output_tokens"] < 16:
                    return self._stop(state, "budget_exhausted", "minimum_output_budget")
                # Persist reservation before crossing the network boundary. A
                # crash cannot erase the request count; billing may be unknown.
                state["model_calls"] += 1
                state["request_in_flight"] = True
                state["last_model_call"] = {"call_index": state["model_calls"], "status": "requested",
                                            **self._usage_metadata(None, failed=True)}
                self.store.save(state, "model_requested", {"context_hash": digest(context),
                                                           "context_chars": len(canonical(context))})
                accounted = False
                try:
                    decision = self.provider.decide(context)
                    usage = self._usage_metadata(decision)
                    self._account_usage(state, usage)
                    accounted = True
                    action = Action.from_dict(decision.action.to_dict())
                except Exception as exc:
                    # Provider messages/bodies may contain secrets. Persist code,
                    # never arbitrary exception text or the request headers.
                    state["request_in_flight"] = False
                    if not accounted:
                        usage = self._usage_metadata(exc, failed=True)
                        try:
                            self._account_usage(state, usage)
                        except (ValueError, TypeError):
                            # Invalid custom error metadata must not erase a
                            # reservation or reduce any budget counter.
                            usage = self._usage_metadata(None, failed=True)
                            self._account_usage(state, usage)
                    code = getattr(exc, "code", "invalid_provider_response")
                    if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
                        code = "invalid_provider_response"
                    state["last_model_call"] = {"call_index": state["model_calls"], "status": "failed",
                                                "error": code, **usage}
                    state["feedback"].append({"error": str(code)[:80]})
                    state["status"] = "provider_error"
                    self.store.save(state, "model_failed", state["last_model_call"])
                    return self._stop(state, "provider_error", "provider_request_failed")
                state["request_in_flight"] = False
                state["decision_index"] += 1
                state["pending_action"] = action.to_dict()
                state["action_id"] = digest({"run_id": run_id, "step": state["steps"], "action": action.to_dict()})
                state["last_model_call"] = {"call_index": state["model_calls"], "status": "decided", **usage}
                self.store.save(state, "model_decided", {"action": action.to_dict(), **state["last_model_call"]})
                # Actual usage is authoritative. Prevent acting after a request
                # crosses a soft token/cost threshold.
                if state["input_tokens"] + state["output_tokens"] > config.max_tokens:
                    return self._stop(state, "budget_exhausted", "token_limit_after_request")
                if (config.max_cost_usd is not None and state["estimated_cost_usd"] is not None and
                        state["estimated_cost_usd"] > config.max_cost_usd):
                    return self._stop(state, "budget_exhausted", "cost_limit_after_request")
            action = Action.from_dict(state["pending_action"])
            if action.kind == "finish":
                rejection = self._check_evidence(state, action) if config.evidence_gate else None
                if rejection:
                    self._consume(state, "completion_rejected", {"error": rejection})
                    continue
                state["result"] = action.to_dict()
                state["steps"] += 1
                state["pending_action"] = None
                status = "insufficient_evidence" if action.diagnosis == "insufficient_evidence" else "completed"
                return self._stop(state, status, "model_finished")
            rejection = self._check_tool(state, action)
            if rejection:
                self._consume(state, "tool_denied", {"error": rejection})
                continue
            if (action.name == "restart_service" and config.approval_gate and
                    state["action_id"] not in state["approved_actions"]):
                state["status"] = "waiting_approval"
                state["approval_id"] = digest({"action_id": state["action_id"], "manifest": state["manifest_hash"]})[:24]
                self.store.save(state, "approval_requested", {"approval_id": state["approval_id"], "action": action.to_dict()})
                return state
            result = self.store.receipt(run_id, state["action_id"])
            if result is None:
                if state["tool_attempts"].get(state["action_id"], 0) >= config.max_tool_retries + 1:
                    self._consume(state, "tool_failed", {"error": "retry_limit", "tool": action.name})
                    continue
                attempts = state["tool_attempts"].get(state["action_id"], 0) + 1
                state["tool_attempts"][state["action_id"]] = attempts
                remaining_faults = state["case"]["transient_failures"].get(action.name, 0)
                used_faults = state["fault_counts"].get(action.name, 0)
                if used_faults < remaining_faults:
                    state["fault_counts"][action.name] = used_faults + 1
                    self.store.save(state, "tool_retry", {"tool": action.name, "attempt": attempts,
                                                          "error": "injected_transient_failure"})
                    if attempts <= config.max_tool_retries:
                        continue
                    self._consume(state, "tool_failed", {"error": "retry_limit", "tool": action.name})
                    continue
                result, effect = self._execute(state, action)
                self.store.commit_tool(state, result, effect)
                if crash_after_tool:
                    raise InjectedCrash("Injected crash after durable tool commit")
            if action.name == "read_document" and "document" in result:
                doc = result["document"]
                state["observed"][doc["id"]] = doc
            state["observations"].append({"tool": action.name, "result": result, "trust": "untrusted_tool_data"})
            self._consume(state, "tool_observed", {"tool": action.name, "result": result})
        return state

    @staticmethod
    def _usage_metadata(value: Any, *, failed: bool = False) -> dict[str, Any]:
        def label(name: str) -> str | None:
            raw = getattr(value, name, None)
            return raw if isinstance(raw, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}", raw) else None
        inputs, outputs = getattr(value, "input_tokens", 0), getattr(value, "output_tokens", 0)
        return {"input_tokens": inputs, "output_tokens": outputs,
                "estimated_cost_usd": getattr(value, "estimated_cost_usd", None),
                "cached_input_tokens": getattr(value, "cached_input_tokens", None),
                "cache_write_tokens": getattr(value, "cache_write_tokens", None),
                "response_id": label("response_id"), "response_model": label("response_model"),
                "cost_reason": label("cost_reason"),
                "usage_incomplete": getattr(value, "usage_incomplete", failed and not inputs and not outputs)}

    @classmethod
    def _account_usage(cls, state: dict[str, Any], usage: dict[str, Any]) -> None:
        cls._account(state, usage["input_tokens"], usage["output_tokens"], usage["estimated_cost_usd"],
                     cached_input_tokens=usage["cached_input_tokens"], cache_write_tokens=usage["cache_write_tokens"],
                     usage_incomplete=usage["usage_incomplete"])

    @staticmethod
    def _account(state: dict[str, Any], input_tokens: int, output_tokens: int, cost: float | None,
                 *, cached_input_tokens: int | None = None, cache_write_tokens: int | None = None,
                 usage_incomplete: bool = False) -> None:
        if any(type(n) is not int or n < 0 for n in (input_tokens, output_tokens)):
            raise ValueError("Provider usage must contain nonnegative integer token counts")
        if cost is not None and (type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0):
            raise ValueError("Provider cost must be finite and nonnegative")
        if any(n is not None and (type(n) is not int or not 0 <= n <= input_tokens)
               for n in (cached_input_tokens, cache_write_tokens)):
            raise ValueError("Provider cache token counts must be input subsets")
        if (cached_input_tokens or 0) + (cache_write_tokens or 0) > input_tokens:
            raise ValueError("Provider cached reads and writes exceed input tokens")
        if type(usage_incomplete) is not bool:
            raise ValueError("Provider usage completeness must be boolean")
        previous_input = state["input_tokens"]
        state.setdefault("cost_incomplete", state["model_calls"] > 1 and state["estimated_cost_usd"] is None
                         and state["provider"] != "scripted")
        for key, value in (("cached_input_tokens", cached_input_tokens), ("cache_write_tokens", cache_write_tokens)):
            state[key] = state.get(key, 0) + (value or 0)
            incomplete = key + "_incomplete"
            state[incomplete] = state.get(incomplete, previous_input > 0) or (value is None and state["provider"] != "scripted")
        state["input_tokens"] += input_tokens
        state["output_tokens"] += output_tokens
        state["usage_incomplete"] = state["usage_incomplete"] or usage_incomplete
        if cost is not None:
            state["reported_cost_usd"] += cost
        elif state["provider"] != "scripted":
            state["cost_incomplete"] = True
        state["estimated_cost_usd"] = (None if state["usage_incomplete"] or state["cost_incomplete"]
                                       else state["reported_cost_usd"])

    def _consume(self, state: dict[str, Any], kind: str, data: dict[str, Any]) -> None:
        state["steps"] += 1
        state["pending_action"] = None
        state["action_id"] = None
        if "error" in data:
            state["feedback"].append(data)
        self.store.save(state, kind, data)

    @staticmethod
    def _check_tool(state: dict[str, Any], action: Action) -> str | None:
        schema = TOOLS.get(action.name)
        if schema is None:
            return "unknown_tool"
        if set(action.arguments) != set(schema):
            return "invalid_tool_arguments"
        if any(not isinstance(action.arguments[key], kind) or not action.arguments[key].strip() or
               len(action.arguments[key]) > 500 for key, kind in schema.items()):
            return "invalid_tool_arguments"
        if action.name == "restart_service" and action.arguments["service"] != state["case"]["service"]:
            return "out_of_scope_service"
        return None

    @staticmethod
    def _check_evidence(state: dict[str, Any], action: Action) -> str | None:
        if action.diagnosis == "insufficient_evidence":
            return None
        if not action.evidence:
            return "Cite observed current telemetry and a current runbook, or abstain with insufficient_evidence."
        documents = []
        for doc_id in action.evidence:
            doc = state["observed"].get(doc_id)
            if doc is None or not doc["current"]:
                return "Citation was not read or is obsolete. Read current evidence before finishing."
            documents.append(doc)
        if not {"telemetry", "runbook"}.issubset({doc["kind"] for doc in documents}):
            return "Cite both current telemetry and a current runbook, or abstain."
        return None

    @staticmethod
    def _execute(state: dict[str, Any], action: Action) -> tuple[dict[str, Any], dict[str, Any] | None]:
        docs = state["case"]["documents"]
        if action.name == "search":
            terms = set(re.findall(r"\w+", action.arguments["query"].lower()))
            scored = [(sum(term in (d["title"] + " " + d["body"]).lower() for term in terms), d) for d in docs]
            scored.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
            return {"matches": [{"id": d["id"], "title": d["title"], "current": d["current"],
                                  "excerpt": d["body"][:180]} for score, d in scored[:8] if score > 0]}, None
        if action.name == "read_document":
            for doc in docs:
                if doc["id"] == action.arguments["document_id"]:
                    return {"document": doc}, None
            return {"error": "document_not_found"}, None
        effect = {"service": action.arguments["service"], "simulation": True,
                  "approved": state["action_id"] in state["approved_actions"]}
        return {"simulated_restart": effect}, effect

    def approve(self, run_id: str, approval_id: str) -> dict[str, Any]:
        state = self.store.replay(run_id)
        if state["status"] != "waiting_approval" or state["approval_id"] != approval_id:
            raise ValueError("Approval does not match the pending action")
        state["approved_actions"].append(state["action_id"])
        state["approval_id"] = None
        state["status"] = "running"
        self.store.save(state, "approval_granted", {"action_id": state["action_id"]})
        return state

    def retry_provider(self, run_id: str) -> dict[str, Any]:
        """Explicit operator retry; previous reservations and usage remain charged."""
        state = self.store.replay(run_id)
        if state["status"] != "provider_error":
            raise ValueError("Only provider_error runs can be retried")
        if (state["usage_incomplete"] or state["estimated_cost_usd"] is None) and state["config"]["max_cost_usd"] is not None:
            raise ValueError("Cannot enforce a cost budget after unknown request usage; reconcile externally and start a new run")
        state["status"] = "running"
        self.store.save(state, "provider_retry_authorized")
        return state
