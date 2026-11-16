# Validation record

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
