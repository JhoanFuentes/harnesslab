# Validation record

## Repeated-study extension — local checks, 2026-11-16

Validated with Python **3.12.14** on Windows. This section records the extension;
the original published GitHub results below remain historical.

| Check | Observed result |
| --- | --- |
| `python -m unittest discover -s tests -q` | **140 tests passed**, 50.995 seconds |
| New study pipeline | Offline tests cover frozen plans, two model identities, repetitions, billed failures, unknown usage, missing slots and interruptions |
| Statistical/accounting checks | Failure costs included; zero-success/unknown cost unavailable; task-cluster bootstrap; no simulated-provider inferential intervals; latency percentiles |
| Synthetic dev/test integrity | 4 dev + 12 test; exact file hashes, split separation, aliases and no evaluator fields in model context |
| Historical artifact | Original source snapshot, dataset, summaries, grades and audit chains verified for all 48 runs |
| Runtime demo | Scripted-only rerun: hardened 12/12; original measured example unchanged |
| Draft plan CLI | 240 planned task slots, at most 1,920 requests; **0 network calls**, draft execution refused |
| HTML browser QA | Plan and injected-test report checked in headless Chrome at 1440×1080 and 390×844; disclosure labels and expandable details worked; no page errors or body overflow |
| Static check | Ruff isolated `E4,E7,E9,F` on `harnesslab` and `tests` passed |
| Live inference | **Not executed, as requested. No API model calls or real-model performance claims** |

The plan and report screenshots were visually inspected. The mocked report is
test evidence only and is not published as model performance. The included
[planning preview](../examples/planned-study/v0.2.0/plan.html) contains no measured
results. The publication checks below record the subsequent GitHub validation.

## v0.2.0 observed publication checks — 2026-11-16

GitHub executed source commit `0b8c628006123a3b2f3bd12a6795c2485b9dc581`:

- [Contract checks](https://github.com/JhoanFuentes/harnesslab/actions/runs/43962691543)
  passed on **Ubuntu/Python 3.11, Ubuntu/Python 3.13, Windows/Python 3.11 and
  Windows/Python 3.13**. Each job ran 140 tests, verified the original artifact,
  generated the scripted demo, prepared a network-free study and validated the
  public-file allowlist and current versioned plan.
- [Pages publication](https://github.com/JhoanFuentes/harnesslab/actions/runs/43962691532)
  succeeded. The original report remains at the site root, and the unexecuted
  v0.2.0 preview is available at [the study URL](https://jhoanfuentes.github.io/harnesslab/study/).
- A clean export of the Git staging tree also passed artifact verification and
  the Pages build, checking normalized checkout bytes rather than relying only
  on the original Windows working directory.
- The live study preview was checked in Chrome at 1440×1080 and 390×844 with no
  page errors or body overflow. Its preparation disclosure and 240 planned slots
  were visible. The public original HTML, study HTML and study JSON matched the
  committed Git blob bytes by SHA-256.

No workflow performs live model inference. The published preview still requires
model snapshots and prices and makes no measured model-performance claim.

## Original implementation and publication

Validated locally on **2026-11-15 (America/Bogota)** with Python **3.12.14**,
Windows 11. The first table records the original local checks; the subsequent
section records observed GitHub CI and public-report validation.

| Check | Observed result |
| --- | --- |
| `python -m unittest discover -s tests -v` | 69 tests passed |
| Offline comparison | 48 runs: 12 cases × 4 profiles |
| Baseline contract checks | 7 / 12 passed |
| Hardened contract checks | 12 / 12 passed |
| No evidence gate | 9 / 12 passed |
| No tool retries | 11 / 12 passed |
| CLI across separate processes | Start, crash, resume, approve, inspect and replay passed |
| Provider protocol | Fake HTTP contract tests passed; no live calls made |
| HTML artifact | Generated, escaped-content and structure tests passed |

The browser security policy initially blocked opening a local `file:` URL.
The report was subsequently inspected at its public HTTPS URL after deployment.

## Observed publication checks

On **2026-11-15 (America/Bogota)**, GitHub executed commit
`5e2649950c954b398867213e8285bb3c3d2fd738`:

- [Contract checks run](https://github.com/JhoanFuentes/harnesslab/actions/runs/43806869285):
  successful on Ubuntu/Python 3.11, Ubuntu/Python 3.13, Windows/Python 3.11,
  and Windows/Python 3.13. Each job ran 69 tests, checked the committed artifact,
  generated an offline comparison and uploaded its evidence.
- [Pages deployment](https://github.com/JhoanFuentes/harnesslab/actions/runs/43806869281):
  successful after verifying the example source, dataset, summaries, grades and
  audit chains for all 48 runs.
- [Public report](https://jhoanfuentes.github.io/harnesslab/): visually inspected
  in a browser. Selecting `baseline` and `Failures only` showed **5 of 48 runs**,
  consistent with the five baseline contract failures. This was desktop QA;
  it is not an exhaustive browser/device accessibility audit.

The workflow actions are pinned to release commit SHAs verified against GitHub's
API. The README status badge links to the current workflow; this record preserves
the specific observed run rather than promising that future commits will pass.

The example report contains exact run manifests, raw verdicts and source/dataset
hashes. Latencies vary by environment and include SQLite I/O. The example uses a
scripted provider: zero tokens and zero model cost describe that provider, not
the cost or accuracy of a real LLM.
