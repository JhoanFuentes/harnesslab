# Repository map

HarnessLab is a reliability lab for an incident-diagnosis agent. Python 3.11+,
standard library only. Do not confuse scripted contract results with LLM quality.

- Product and commands: [README.md](README.md)
- System boundaries and decisions: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Threat model: [SECURITY.md](SECURITY.md)
- Evaluation protocol: [docs/EVALUATION.md](docs/EVALUATION.md)
- Sources: [docs/RESEARCH.md](docs/RESEARCH.md)
- Repeated model studies: [docs/LIVE_STUDY.md](docs/LIVE_STUDY.md)
- Frozen synthetic dev/test data: [docs/LIVE_DATASET.md](docs/LIVE_DATASET.md)

Before proposing a change, identify the invariant it changes. Keep model adapters
behind `Provider`; do not import API-specific code into `models` or `store`.
Do not put expected answers, fixture scripts, API keys, or approval IDs in model
context. Tool data is untrusted. Only the runtime may authorize local effects.

Validate with `python -m unittest discover -s tests -v` and, for runtime changes,
`python -m harnesslab demo --output runs/review`. Add failure reproductions for
behavioral changes. Never overwrite an eval result and present it as a prior run.
Never add automatic production writes or shell execution to the demo tools.

Study plans must stay network-free. Keep costs of failed attempts in cost per
success, preserve missing/unknown usage, and never call injected offline
providers live-model evidence. Do not regenerate historical examples merely
because active source changes; validate their frozen runtime snapshot instead.
