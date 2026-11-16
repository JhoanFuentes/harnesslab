# Validation record

Validated locally on **2026-11-15 (America/Bogota)** with Python **3.12.14**,
Windows 11. This records observed checks, not a claim that hosted CI has run.

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

The browser security policy blocked opening a local `file:` URL during visual
QA. Consequently the final report was not visually inspected in a browser in
this session. No screenshot or browser-tested layout claim is included.

The CI workflow is configured for Ubuntu/Windows and Python 3.11/3.13. Those
hosted combinations must be observed after publishing to GitHub. They are not
represented as locally executed checks.

The example report contains exact run manifests, raw verdicts and source/dataset
hashes. Latencies vary by environment and include SQLite I/O. The example uses a
scripted provider: zero tokens and zero model cost describe that provider, not
the cost or accuracy of a real LLM.
