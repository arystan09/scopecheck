# ScopeCheck boundaries

The approved baseline has exactly **10 mandatory milestones**. The useful
fake-provider MVP is milestone **4**, not milestone 1. Only milestone 1 is
authorized here: package, settings, liveness, CLI help, and verification scaffolding.
The full later-stage roadmap was not supplied; obtain it before later-stage work.
Do not invent, merge, reorder, or silently implement milestones.

Keep one modular Python package and application image. The approved architecture
uses FastAPI, Pydantic, PostgreSQL 18, SQLAlchemy async/psycopg, Alembic, and a
separate worker. Database code, migrations, worker behavior, UI, and domain tools
are outside milestone 1. Do not create empty future module trees. No Redis,
Celery, Kubernetes, Kafka, RAG, real payments, or generic agent framework.

Read files and git status before edits; preserve unrelated changes. Verify prior
stage prerequisites and report missing ones. Run README checks and report their
actual results, separating failures, unavailable checks, and pending remote CI.
Never claim in-process tests prove container/database/concurrency behavior. Never
weaken assertions, skip failing checks, fabricate evidence, commit, or push for
the user. No paid calls, billable resources, or external publication.

For future authorized domain work: model messages and tool arguments are untrusted.
Scenario data supplies actor identity/policy; operator workspace comes from the API
token. Payloads cannot override either. Never expose expected outcomes or hidden
policy metadata to models. Mutate fresh synthetic case state only, use integer
minor units for money, and reject unexpected fields. No real refunds/shipping,
arbitrary HTTP targets, or imported executable code.
