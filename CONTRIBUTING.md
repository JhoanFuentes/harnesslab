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
