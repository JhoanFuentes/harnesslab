# Research basis and scope

Research cutoff: **2026-11-15**. This is a curated set of primary sources, not a systematic literature review or a claim to cover every release. Dates below are publication dates shown by the sources. HarnessLab's design choices are our interpretation of that evidence.

HarnessLab explores a narrow question: **can an incident-diagnosis agent preserve explicit contracts when tools fail, execution stops, context is bounded, and a proposed action requires approval?** The runtime is implemented in Python with the standard library. Its tools operate on local synthetic fixtures and simulated changes. This makes failure modes inspectable without providing production infrastructure credentials.

## Sources mapped to implementation decisions

| Primary source | Publication date | Finding relevant to HarnessLab | Design consequence |
| --- | --- | --- | --- |
| [Effective harnesses for long-running agents — Anthropic](https://www.anthropic.com/research/effective-harnesses-for-long-running-agents) | 2025-11-26 | Continuity across sessions needs explicit progress artifacts and verification; compaction alone does not ensure completion. | Persist the session outside the loop's process and reconstruct bounded working context from durable state. |
| [Demystifying evals for AI agents — Anthropic](https://www.anthropic.com/research/demystifying-evals-for-ai-agents) | 2026-01-09 | Tasks, trials, graders, trajectories, and actual outcomes are different objects. A successful-looking answer is not the same as a successful environment state. | Grade diagnoses, evidence references, terminal states, and policy behavior separately. Keep trajectories available for inspection. |
| [Quantifying infrastructure noise in agentic coding evals — Anthropic](https://www.anthropic.com/research/infrastructure-noise) | 2026-02-05 | Resource limits and execution conditions can change agent benchmark results and confound comparisons. | Record execution configuration and distinguish tool/infrastructure failures from incorrect diagnoses. Treat resource limits as experimental variables. |
| [Harness engineering: leveraging Codex in an agent-first world — OpenAI](https://openai.com/index/harness-engineering/) | 2026-02-11 | Agent-readable environments, repository knowledge, and enforceable constraints change how engineering teams work with agents. | Keep a short repository map, explicit contracts, reproducible checks, and architecture rationale in the repository. |
| [Harness design for long-running application development — Anthropic](https://www.anthropic.com/engineering/harness-design-long-running-apps) | 2026-03-24 | Planning and evaluation can improve a workflow, but harness components must be reassessed as model capabilities change. | Start with a single loop and replaceable provider. Use ablations to investigate each mechanism before adding orchestration complexity. |
| [Scaling Managed Agents: Decoupling the brain from the hands — Anthropic](https://www.anthropic.com/research/managed-agents) | 2026-04-08 | Session persistence, the harness process, and execution tools can be separated so they fail and recover independently. | Store sessions/events in SQLite; keep provider decisions, runtime control, and local tool execution behind distinct interfaces. |
| [The Anatomy of Harness Engineering — Google Developers](https://developers.googleblog.com/the-anatomy-of-harness-engineering-how-to-evaluate-iterate-and-guard-ai-coding-agents/) | 2026-09-09 | Behavioral checks on observable intermediate actions complement end-to-end outcome benchmarks. | Assert approval, retry, evidence, and termination behavior in addition to the final answer. |
| [Doc2Harness: Benchmarking LLMs on Long-Horizon Harness Engineering of Coding Agents — Ge et al.](https://arxiv.org/abs/2610.20344) | 2026-10-14 | This preprint evaluates harness implementations with a scripted model and observable effects, using positive controls to reject vacuous passes. | Separate deterministic runtime conformance tests from live model evaluations, and include successful permitted behavior alongside blocked behavior. |

The company articles are engineering reports, not universal causal proofs. Doc2Harness is a preprint. HarnessLab is an independent educational implementation inspired by these ideas; it does not reproduce those systems or their reported results.

## What the experiments mean

### 1. Runtime conformance

A scripted provider supplies a known sequence of actions. The question is whether the runtime handles that sequence correctly: rejects an unsupported completion, preserves an approval boundary, retries a transient failure within limits, or resumes a durable session. This isolates engineering behavior from model nondeterminism and makes offline CI possible.

An unsafe scripted action is an injected test stimulus. It is not evidence that a real LLM would choose that action. Likewise, a successful scripted diagnosis does not show that a model can diagnose an unfamiliar incident.

### 2. Applied AI quality

The optional OpenAI Responses adapter replaces scripted decisions with a live model. The resulting evaluation measures the combination of the chosen model, prompt, context construction, tools, fixtures, and harness configuration. Live results require credentials and actual execution; the repository must not present an unexecuted live experiment as a measured result.

Keep the model identifier, prompt/configuration, fixture revision, limits, timestamps, usage, and errors with published results. If pricing is supplied by configuration, call the resulting value an **estimated cost**, not an invoice total. Unknown cost is not zero cost.

### 3. Ablations

| Profile | Intended contrast | Interpretation limit |
| --- | --- | --- |
| `hardened` | Evidence gate, explicit local approval, and bounded tool retries enabled. | These controls address the included contracts; the name is not a security certification. |
| `baseline` | Evidence and approval gates disabled, retries disabled. | Multiple variables change together; differences cannot identify one mechanism's causal contribution. |
| `no-evidence-gate` | Remove only the evidence gate relative to `hardened`. | Shows the effect on this fixture set, not general factual accuracy. |
| `no-retries` | Remove tool retries relative to `hardened`. | Retry benefits depend on the injected failure distribution and must be weighed against extra attempts. |

Use the single-feature ablations to explain mechanisms. Use the baseline only as a deliberately weaker reference configuration. It is not a competitive implementation or an external benchmark.

## Important boundaries

- **Evidence provenance is not semantic truth.** Referencing observed, current evidence IDs can prevent unsupported references. It cannot prove that a diagnosis logically follows from the cited content, detect every misleading source, or validate real-world freshness beyond fixture metadata.
- **Local approval is not enterprise authorization.** The explicit CLI interaction demonstrates a control point. It does not establish identity, separation of duties, delegated permissions, or an organization-wide audit policy.
- **Local transactions are not arbitrary external exactly-once delivery.** SQLite can make related local state changes atomic. An external service needs its own idempotency or reconciliation protocol; a process crash can otherwise leave an ambiguous outcome.
- **A controlled tool set is not an operating-system sandbox.** The simulation does not execute arbitrary model-written code or remediate real systems. Adding shell access or network writes would materially change the threat model.
- **Context size is a tradeoff.** Bounded context reduces growth but may discard useful detail. Character limits and token usage are different measurements. A bounded context does not prove robust long-horizon reasoning.
- **Retries have costs.** They help selected transient failures, but can amplify load or duplicate effects when applied to unsafe operations. A retry cap is part of the contract.
- **Durability has an operating envelope.** This is a local research system, not a multi-tenant service with a distributed scheduler, independently secured audit store, or production SLO.

## A defensible experiment protocol

1. Freeze the case set, expected outcomes, and grader rules before comparing profiles. Inspect that success cannot be obtained simply by refusing all work.
2. Run every profile on the same cases. Save the report and enough metadata to reproduce the configuration.
3. Inspect a success, a rejected action, a retry, and a resumed session directly in the trace and persisted state. Do not rely only on the aggregate score.
4. For live evaluation, repeat independent model trials and disclose sample size, failures, and missing runs. Preserve per-case results; a single aggregate hides regressions.
5. Do not treat identical deterministic reruns as independent samples. Confidence intervals from a handful of correlated fixtures are not evidence of production reliability.
6. Use a development set for iteration and a separately authored holdout for a later quality study. Do not call a visible fixture set an unseen benchmark.
7. Publish failures and the smallest conclusion supported by the data. Claim contract coverage or measured behavior on named cases, not SOTA performance, general prompt-injection resistance, or hiring impact.
