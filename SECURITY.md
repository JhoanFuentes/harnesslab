# Security model and limitations

This project operates on synthetic local incident documents. Its only mutation
is a simulated service restart recorded in SQLite. There is no shell executor,
arbitrary HTTP tool, external infrastructure write or production credential store.

## Trust boundaries

| Boundary | Control | Remaining limitation |
| --- | --- | --- |
| Model output → runtime | Typed action, exact tool arguments, allowlist | Model can still waste its bounded requests or make a wrong diagnosis |
| Document → context | Untrusted-data label and system instruction | A label is not an injection-proof boundary |
| Action → effect | Service scope, exact pending-action approval | Local CLI caller is trusted; no user identity, RBAC or authentication |
| Tool result → completion | Read/current citation and document-type checks | No semantic truth or entailment proof |
| Process → state | Atomic local receipts, versions, durable events | Single worker per run; not distributed execution |
| Provider → logs | Sanitized error codes, no request headers/bodies | Incident text and tool data remain in local traces |
| Trace → HTML | Escaping, no remote assets, text-only rendering | Reports still contain the data you chose to evaluate |

OpenAI credentials are read from environment/constructor and never put into the
model context or SQLite state. The HTTP endpoint is fixed to OpenAI and redirects
are disabled to avoid forwarding Authorization to a different host. `store=false`
is requested; that flag is not a claim about all provider retention policies.

Do not put production secrets or private incidents into public fixture files,
GitHub reports, or shared databases. `.gitignore` excludes `.env`, runtime folders
and SQLite files. Review generated artifacts before sharing real-data evaluations.

Event hashes detect partial corruption/tampering; a database administrator can
rewrite a complete chain. They are not signatures, immutable storage or proof of
who approved an action. The approval ID is a correlation identifier, not a secret
or authentication token. The API intentionally trusts the local operator.

Before connecting a real external tool, design authentication, authorization,
egress controls, target-side idempotency, reconciliation, data retention and
operational monitoring. None are implied by the local demo's receipt guarantee.

To report a defect after publishing your fork, use the repository owner's chosen
private security reporting channel. Do not post credentials or private traces
in a public issue. This template does not advertise an unconfigured contact.
