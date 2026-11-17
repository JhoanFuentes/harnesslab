# Contributing

Use Python 3.11 or newer. Runtime and tests use the standard library.

```sh
python -m unittest discover -s tests -v
python -m harnesslab demo --output runs/change-review
```

For a behavioral change, add the smallest failing reproduction first. Prefer
assertions on observable effects, checkpoints and results over implementation
details. Include a positive control when adding a blocking mechanism.

Keep changes within the module boundaries in [architecture](docs/ARCHITECTURE.md).
Do not add a framework, service or dependency without explaining the problem it
solves and how its value will be measured. Preserve unknown-cost accounting and
never put evaluator labels or credentials in the model context.

A useful pull request explains the failure, new behavior, relevant tests and
remaining tradeoffs. When changing the recorded experiment, regenerate its report
and explain why verdicts changed. No live API call is required for ordinary CI.

Good first extensions:

- Add held-out incident fixtures with reviewed labels and no answer hints.
- Compare a semantic-support grader with the existing citation-provenance grader.
- Add a provider adapter while preserving usage/error and resume contracts.
- Implement a mock external idempotency endpoint and test ambiguous failures.
- Add randomized evaluation order with the seed recorded in the manifest.

Do not describe planned features as implemented. Disclose AI assistance and
validate generated changes just as you would any other contribution.

## Native interrupt checks in CI

The `Contract checks` workflow runs the whole suite once per matrix cell:
Ubuntu and Windows, each with Python 3.11 and 3.13. Reproduce its test step from
the repository root with no third-party packages or API credentials:

```sh
python scripts/run_ci_tests.py --output runs/ci-tests
```

The runner requires all three native-interrupt cases to be discovered, executed
exactly once, and passed. Missing, skipped, duplicated, or interrupted cases
fail the check, as do failures in the rest of the suite. The cases cover normal
publication, cancellation during backoff followed by recovery, and cancellation
after commit followed by confirmation without another publication call.

Windows workers use a new hidden console and the base interpreter to avoid a
venv redirector sharing that console. Before delivering `CTRL_C_EVENT`, the
worker verifies that only its own PID is attached. Linux workers use a new
session/process group and deliver `SIGINT` to themselves. The parent coordinates
readiness over pipes and never broadcasts a signal from the runner's console.
Failure to establish isolation is an error, not a skip or a simulated fallback.

`runs/ci-tests/tests.log` contains the unittest output and tracebacks;
`runs/ci-tests/summary.json` records the platform, Python version, counts, and
native-case results. GitHub's step summary also shows the result. The workflow
uploads these files in `evidence-<os>-<python>` even after a test failure, with
seven-day retention. Matrix fail-fast is disabled so other platforms can finish;
each job has a ten-minute timeout. A forcibly terminated runner might not finish
writing its reports, so an absent artifact is not evidence of a passing test.

The workflow has only `contents: read`, uses no model-provider secrets, and
performs no GitHub release or package publication. Its later demo and study-plan
steps remain offline. Normal Actions checkout/setup/artifact transfers still
use GitHub's services. Keep the fake publication scripts and their test workers
in the same commit as the workflow and CI runner; do not commit temporary SQLite
databases or `runs/` output. A local Windows result is not evidence of a green
Linux job: inspect all four jobs for the exact commit before reporting CI success.

Push and pull-request events run the checks automatically once the files are in
GitHub. The workflow also supports manual dispatch after its updated definition
is available on the default branch. Re-running checks creates fresh temporary
fake state; it does not repeat a real release publication.
