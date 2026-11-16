# Repeated model studies and cost per successful task

**Status, 2026-11-16: implemented and tested offline. No paid inference was
performed for this extension. There are no new real-model performance results.**

The original `eval` command is retained for compatibility. The new `study-plan`
and `study-run` commands add a frozen protocol, multiple model identities,
randomized repeated trials, task-cluster uncertainty, and failure-inclusive
inference economics. They use the same durable runtime and independent graders.

## Prepare without an API key

From the repository root:

```sh
python -m harnesslab study-plan --config studies/live.example.json --output runs/study-plan.json
```

Open `runs/study-plan.html`. This command performs **zero network calls**. The
example is deliberately a draft: both model snapshots and their token prices
are unset. It plans 12 test cases × 2 models × 2 profiles × 5 trials = **240 task
attempts**, with at most 8 requests each (1,920 planned request slots). This is a
workload description, not a prediction of price or an authorization to spend.

The four separate development cases are for checking prompt compatibility and
the rubric before the main study. All 16 cases are authored public synthetic
fixtures, not real production incidents or an external unseen benchmark. See
[dataset construction and limitations](LIVE_DATASET.md).

## Configure an actual study later

1. Copy `studies/live.example.json` to another file **in the same directory**.
   Paths in the configuration resolve relative to that file.
2. Select supported Responses API model snapshots and replace each `REPLACE_…`
   name. Set the input/output USD-per-million rates that apply to that model and
   service tier; optionally set a cache-read rate. Record the pricing URL and
   date in `pricing`. The configuration supplies prices, not the environment.
3. Choose an authorized spend threshold and per-run limits. For a development
   pilot, point `dataset` to `../datasets/live-v1/dev.json`, keep two trials and
   one model. Review aliases and output limits on development cases only.
4. Freeze a new plan. After the pilot, freeze the main test plan before looking
   at test results. Preserve all failed attempts and any revised protocol as a
   separate experiment; never select only successful runs for publication.

```sh
python -m harnesslab study-plan --config studies/my-study.json --output runs/my-study-plan.json
```

The plan includes the full schedule, rubric, dataset hash, runtime source hash,
prompt hash, rates and limits. Editing it or changing runtime source invalidates
it. Generate a new plan instead. Existing plan files are not overwritten.

Only when live calls are intended and spending is authorized, set
`OPENAI_API_KEY` in the environment or an ignored local `.env` file, then run:

```sh
python -m harnesslab study-run --plan runs/my-study-plan.json --output runs/my-study-001 --env-file .env
```

The optional file reader accepts literal `OPENAI_*` assignments, uses only the
key, and executes no shell code. Never put the key in the plan or commit it.
Without `--env-file`, the key comes from the process environment. The command
refuses draft plans, missing credentials and existing output directories.
Selecting models and filling current tariffs is intentionally left to execution
time; model availability and pricing change. Consult the official
[model catalog](https://developers.openai.com/api/docs/models) and
[pricing](https://developers.openai.com/api/docs/pricing).

## Metric definitions

| Metric | Definition and denominator |
| --- | --- |
| Successful task | Correct frozen diagnosis/abstention label AND required current observed evidence AND safe effect receipts |
| Pass rate | Successful attempts / all attempted tasks in the model/profile group; pending slots are disclosed separately |
| Cost per successful task | Estimated inference cost of **all attempted tasks**, including failures, / successful tasks |
| Zero successes | Cost per success is `null`, with a reason; never `$0` |
| Unknown usage or cost | Total cost and cost per success remain `null`; known reported cost is shown separately |
| All-trials success | Fraction of **complete cases** passing every planned trial; incomplete cases are listed and excluded |
| 95% interval | Percentile bootstrap resampling case IDs, retaining their trials together; 2,000 resamples in the final report |
| Paired profile difference | Candidate minus reference on cases with complete, identical trial grids on both profiles |
| Latency | Wall time from immediately before `Harness.run` through trace extraction and deterministic grading; excludes provider construction, aggregation and HTML rendering |

Five reruns of the same incident do not create five independent incidents.
The seed controls schedule order and bootstrap sampling, not model randomness.
Interrupted attempts also include local stop handling in their measured wall
time. These study timings include more local overhead than the legacy `eval`
timer and should not be compared as if the definitions were identical.
Intervals describe variation over this suite, and can be degenerate on a tiny
or uniform sample. Partial-execution intervals are conditional on observed
attempts. They do not correct selection bias, establish a production SLA or
prove general reliability. Injected offline test providers receive no inferential
intervals. Interim snapshots use 200 bootstrap resamples; final snapshots use
2,000 and record the choice.

The fixed alias rubric normalizes NFKC, case and whitespace/hyphens and performs
exact membership matching. It does not use a fuzzy match or LLM judge. Aliases
and expected outcomes stay outside model context. Valid unlisted paraphrases can
still fail. Publish such errors and revise only a subsequent protocol; do not
silently regrade the original result. In the new suite, stopping for approval
alone does **not** count as task success on an answerable injection case.

## Cost and stop semantics

Costs use provider-reported tokens and the frozen configured prices. Cache reads
are priced separately when both the provider count and rate are available;
otherwise input uses the full input rate, with the reason recorded. Output
tokens already include reasoning tokens and are not counted twice. These are
model-inference estimates, not invoices or total operational cost. See the
official [prompt caching usage contract](https://developers.openai.com/api/docs/guides/prompt-caching).

The current adapter does not price cache-write tiers. If a priced response
reports cache writes, it preserves usage and response metadata, marks cost
unknown and stops the study rather than underestimating it. Future model billing
features require an explicit adapter extension and tests.

Execution is sequential. Before each task, its runtime cost threshold becomes
the smaller of its configured per-run threshold and the remaining study
allowance. Usage is known **after** the response: one in-flight request can
cross the threshold. This is not a provider-enforced billing ceiling. Use the
provider's account controls as appropriate for an actual paid experiment.

Unknown usage/cost halts the schedule. HTTP/transport infrastructure failures
halt it as well. Invalid or incomplete model outputs with known billed usage
remain failed observations; their cost is retained. There are no automatic
provider retries and no approvals of simulated mutations during a study.

## Artifacts and recovery

Each new execution directory contains:

- `plan.json`: frozen protocol, copied unchanged.
- `traces.sqlite`: original durable checkpoints and hashed event chains.
- `results.json`: every attempted slot, remaining slots, rubric, counts, usage,
  response identifiers, actual returned model labels, errors and group metrics.
- `report.html`: standalone comparison and expandable per-run trajectories.

Snapshots update after each attempted task using atomic file replacement.
Keyboard interruption records the active attempt and marks in-flight usage
unknown. An abrupt process kill can leave the last SQLite checkpoint ahead of
the JSON snapshot: inspect the trace before any further inference. This version
does not automatically resume an entire study. Start a separately named study
after reconciling any unknown charges; never silently merge or replace attempts.

The JSON and HTML are each atomically replaced, not one cross-file transaction.
Use the JSON and durable trace as the authoritative artifacts after interruption.
`study-run` exits 0 for a fully executed schedule and 4 for a stopped/interrupted
schedule. Model quality failures are data, not CLI errors. Configuration errors
exit 2. CI exercises offline contracts and planning only; it makes no paid calls.

## Honest portfolio description

> Built a reproducible agent evaluation harness with frozen experiment plans,
> repeated trials, task-cluster bootstrap comparisons, durable trajectories and
> cost per successful task including failed attempts. Implemented provider usage
> accounting and stop rules for ambiguous billing. Validated the infrastructure
> offline; live model accuracy and economics remain to be measured.

After actual execution, replace the final sentence with the named models,
dataset version, task/trial counts, uncertainty, observed cost and limitations.
Do not convert an offline adapter test into a model-performance claim.
