# ADR 0007: fixed public fake admission and evaluation boundaries

Status: accepted for milestone 7 of the ten-stage baseline. The milestone-4 fake
MVP remains usable. This decision does not authorize later-stage features or
external deployment.

## Trust and authority

An operator bearer token determines workspace and reader/editor role. Tokens are
random opaque values stored only as hashes; revoked tokens fail authentication on
subsequent requests. Scenario actor/merchant/role data is trusted test setup, not a
model instruction. Every model message, order note, tool name and argument is
untrusted. Unknown tools, invented argument fields and identity overrides fail
closed. The API never accepts a workspace override. Local database owners remain
trusted administrators; this design does not defend against an owner editing code,
credentials, database records or deployment environment.

The three tools operate on fresh synthetic state. They make no arbitrary HTTP,
payment or shipping calls. The structured oracle interprets hand-authored private
permissions and goals, not the authorization gate. Model payloads contain public
input and bounded tool feedback, never initial hidden state, expected outcomes or
private policy metadata. Authorized order reads may include synthetic notes;
notes cannot grant access to another order or change the actor's role.

## Operator route audit

All operator routes require a currently valid token. Missing, invalid and revoked
tokens return 401. Dataset/config/run writes require editor; reader writes return
403. In public demo mode both roles receive 403 on ordinary write APIs.

| Route | Reader/editor reads | Editor writes in local mode | Tenant enforcement |
| --- | --- | --- | --- |
| `/api/v1/datasets` | Scoped list and cursor | Atomic import | Token workspace; immutable workspace/hash version |
| `/api/v1/datasets/{id}` | Yes | None | Owned ID only |
| `/api/v1/configs` | No read route | Fixed-schema fake config | Token workspace; immutable workspace/hash version |
| `/api/v1/runs` | Scoped list and cursor | Fake-only idempotent queue | Both dataset and config must be owned |
| `/api/v1/runs/{id}` | Yes | None | Owned ID only |
| `/api/v1/runs/{id}/cases` | Yes | None | Owned run and case cursor within that run/workspace |
| `/api/v1/runs/{id}/report` | Yes | None | Owned run before results or ledger access |
| `/api/v1/comparisons` | Yes | None | Both runs owned, then terminal compatibility checks |

Foreign IDs/cursors and absent resources use the same 404 code/message, without
revealing foreign existence. Relationship inserts are also defended by composite
workspace foreign keys for runs, jobs, attempts, results and call ledgers.
Integration tests retain direct-insert checks and audit the complete route
inventory; a new operator route must extend the audit. Pagination defaults to 20,
maximum 100. Error responses contain only code/message/request_id. No submitted
payload, database diagnostic or token is echoed. This is not a constant-time
non-enumeration guarantee.

Before JSON parsing, streamed request bodies are capped at 1 MiB for datasets,
16 KiB for other writes, and 64 bytes for the public endpoint. Body receipt has a
five-second deadline. Actual bytes are counted even with chunked transfer or a
false Content-Length. Dataset import additionally permits at most 100 cases.
Unauthenticated oversized input can receive 413 before authentication.

## Public demo

`SCOPECHECK_PUBLIC_DEMO=true` is opt-in and must be set identically for API and
worker against an isolated deployment database. Default/local mode does not admit
anonymous jobs. Public mode refuses startup with either `SCOPECHECK_OPENAI_API_KEY`
or `OPENAI_API_KEY` present, even an empty value. Compose forwards neither key.
Live CLI creation, provider client construction and call reservations are disabled
in public mode. The worker checks mode, reserved workspace, release manifest,
dataset hash, safe fake config, one repetition and fixed limits before executing.
Previously queued ordinary fake/live jobs become failed with `public_demo_only`;
they are not executed. Do not switch a working local/live database into demo mode.

`POST /demo/runs` accepts only an empty body or exactly `{}` (outer whitespace is
allowed) and no query parameters. It imports the frozen 24-case development suite,
selects the safe fake config and queues one repetition. It accepts no model,
prompt, URL, dataset, config, fixture, identity or budget override. It returns a
202 synthetic/fake run receipt. Anonymous report reads are intentionally not added;
a local administrator can issue a reader token for `scopecheck-public-demo` to
inspect results through existing APIs/UI.

Migration 0005 adds one seeded singleton admission row. Its `FOR UPDATE` lock
serializes every admission across processes. In the same transaction the server
checks a lifetime cap of 100 admissions, at most two demo jobs queued/running, and
at least five database-clock seconds between admissions, then creates the run/job
and increments the counter. Rollback consumes nothing. A quota failure returns
429 without creating a job. A database constraint also bounds the counter. Process
restarts do not reset it; there is no public reset API. Completion frees a queue
slot but does not replenish lifetime admissions. Tests isolate queue/lifetime races
from the throttle to exercise each constraint with independent PostgreSQL backends.

This global throttle deliberately does not trust source-IP/forwarded headers and
provides no per-user fairness. An attacker can consume the finite demo allowance
or starve others; operators must restart a disposable demo with a fresh database
when appropriate. It is not general internet denial-of-service protection: total
connections, headers, TLS and reverse-proxy ingress limits remain deployment
responsibilities. No external publication or production capacity claim is made.
PostgreSQL admission/queue serialization favors a small bounded demo over high
throughput. No Redis, broker, identity service or third-party script was added.

## Evaluation limits

See [evaluation.md](evaluation.md) for frozen hashes and grading boundaries.
Canaries test structured order-result disclosure, including foreign-order results
and unexpected data spliced into an authorized result. The oracle checks events,
state transitions and declarative goals; it does not comprehensively grade
unrestricted natural-language semantic leakage, paraphrases, encoded secrets,
persuasion, or all possible prompt injection attacks. Synthetic scripted results
are not live-model safety measurements. No prompt was tuned using holdout outcomes
and no paid provider request was made in this milestone.
