# Historical source snapshot

These Python files are the source used by the published **v0.1.0 scripted
evaluation** in the parent directory. They were extracted from the repository's
original committed runtime before the repeated-model study extension.

`scripts/verify_example.py` recomputes the original runtime hash over these files,
then verifies the original dataset, per-run grades, summaries and SQLite audit
chains. The original `results.json`, HTML and traces have not been regenerated.

The active implementation is `harnesslab/` at the repository root. This archive
is provenance for a historical artifact, not the runtime to use for new studies.
