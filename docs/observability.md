# Milestone 8: operational evidence and measurement

This stage instruments existing behavior; it makes no performance optimization or
production capacity claim. Architecture, fake/live authority and the frozen splits
are unchanged. Migration 0006 adds nullable enqueue trace context to `run_jobs`;
old jobs remain runnable with a new worker-root trace when no parent exists.

## Data boundaries and private access

Application logs are JSON on stderr. Explicit spans are `http.request`,
`worker.run`, `case.execute`, `provider.request`, and `budget.reserve`. Only
allowlisted bounded route/status/error/mode fields and validated internal IDs cross
the telemetry boundary. There is no automatic HTTP, database or SDK instrumentation.
Request headers, paths with IDs, queries, bodies, prompts, tool arguments/results,
scenario labels, token values, token counts, SQL and exception messages/stacks are
not emitted. Uvicorn access logs are disabled, and application/runtime log formatting
never interpolates arbitrary messages or traceback text. Avoid adding an external
auto-instrumentation wrapper, which would bypass these capture rules.

The API creates its own root span and ignores incoming traceparent/tracestate and
baggage headers. Queue creation stores only its generated W3C traceparent in the
same run/job transaction. A worker resumes that parent after claim; case execution
and fake/live provider requests are children. A stored context is diagnostic
correlation, never authorization. Idempotent duplicate enqueue retains the original
job's context. Retried/taken-over executions can have multiple sibling worker spans;
this does not imply exactly-once remote execution.

`GET /metrics` is the only new HTTP endpoint. It is disabled (404) unless an API-only
`SCOPECHECK_METRICS_TOKEN` of at least 32 characters is configured. It requires
`Authorization: Bearer <that token>` using constant-time comparison. Operator bearer
tokens do not grant telemetry access. Public demo mode always returns 404, even
with a correct metrics token. Do not expose a local collector/dashboard publicly.

Prometheus labels are limited to route templates, status classes/enums and bounded
error categories (`le` is the histogram's fixed bucket label). No run, case,
workspace, request, token or trace ID is a metric label. Unknown paths/errors map
to `unmatched`/`other`. Metric meanings:

| Metric | Meaning and scope |
| --- | --- |
| `scopecheck_http_seconds` | API-process histogram, template + HTTP status class; through response construction, not client transfer time |
| `scopecheck_queue_depth` | Database-wide queued/running job count |
| `scopecheck_cases_total` | Database-wide persisted final results by completed/limited/failed status |
| `scopecheck_provider_failures_total` | Persisted live settlement failures plus fake final-case failures, by bounded error; unknown unfinished remote calls are not fabricated as known failures |
| `scopecheck_lease_takeovers_total` | Sum of durable generations beyond the first claim |
| `scopecheck_reservations` | Count of outstanding/unknown versus settled reservations; zero-cost known settlements are settled |
| `scopecheck_case_seconds_total` | Sum of finished current-attempt durations; excludes unfinished/interrupted attempt time |

Durable metrics query PostgreSQL at scrape time; a failed scrape returns 503, not
made-up zeros. Empty groups have no sample. The HTTP histogram resets on process
restart; scrape each API process separately if using multiple instances. Do not
sum duplicate database-wide metrics across API replicas. Database aggregates can
be expensive at larger scale; no scale/throughput improvement is claimed.

## Local trace artifacts and retention

Set `SCOPECHECK_TRACE_DIRECTORY` to an absolute private directory shared by local
API/worker processes. Each trace is a JSONL file named by its generated trace ID.
New directories/files use modes 0700/0600; symlink destinations are not followed.
Spans append without prompts or payloads. File export is synchronous and best-effort;
a filesystem failure can lose diagnostics but must not fail or repeat evaluation.
It is not a billing ledger or audit-log durability guarantee.

Non-retained diagnostic traces expire after **30 days since the last span write**.
`SCOPECHECK_TRACE_RETENTION_DAYS` accepts 1–3650. The worker prunes on startup and at
most once/hour thereafter; API-only deployments must schedule the same local CLI.
A stopped worker does not execute cleanup. Automatic cleanup failures log a
bounded diagnostic and do not stop evaluation. Local trace retention requires
POSIX file locks (Linux/macOS); other hosts should use the Linux image. These rules do not delete immutable case
results, datasets, cost ledgers or separately published evaluation reports.

```sh
export SCOPECHECK_TRACE_DIRECTORY="$PWD/work/private-traces"
uv run scopecheck telemetry prune
# Explicitly preserve a trace artifact selected for publication/retention:
uv run scopecheck telemetry retain TRACE_ID
```

`retain` creates a persistent `.retained` marker; pruning never removes marked
artifacts, regardless of age. It does not publish anything. Operators must mark an
artifact before publishing/retaining it; the system cannot infer publication
elsewhere. Local prune/retain operations share a process lock. Unrecognized files
and symlinks are left alone. Standard stdout/stderr log rotation is separate from
trace-file retention and remains the host/container runtime's responsibility.

## Optional collector/dashboard

The optional `observability` Compose profile runs one Jaeger collector/query UI
with transient in-memory storage, pinned to 2.23.0. Both exposed ports bind only to
127.0.0.1. It is a local diagnostic display, not the retained artifact store.

```sh
export SCOPECHECK_COLLECTOR=compose
# Keep the generated local POSTGRES_PASSWORD from your existing Compose setup.
docker compose --profile observability up --build -d
# Open http://127.0.0.1:16686 locally.
```

Native API/worker processes may use `SCOPECHECK_COLLECTOR=local` with the local
collector on 127.0.0.1:4318. Default is `disabled`. These two fixed targets are the
only exporter destinations; there is no arbitrary endpoint setting. Ambient
`OTEL_*` configuration is refused when export is enabled to prevent SDK header,
credential-provider or resource overrides. Proxy inheritance is disabled. Provider
calls and telemetry export are independent: no provider key is needed for fake
traces. Collector loss can drop telemetry; it does not authorize/retry model calls.
Docker availability is required to verify this profile; it was unavailable locally.

Implementation references checked against installed interfaces and primary docs:
[OpenTelemetry Python instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/),
[Prometheus custom collectors](https://prometheus.github.io/client_python/collector/custom/),
[pytest-cov configuration](https://pytest-cov.readthedocs.io/en/latest/config.html), and
[Jaeger 2.23 local all-in-one](https://www.jaegertracing.io/docs/2.23/getting-started/).

## Benchmark reproduction and scope

The script requires Python 3.12, the frozen development environment, and a local
PostgreSQL 18 admin URL with create/drop-database privileges. It rejects non-loopback
admin hosts. It creates a dedicated random database, migrates it, starts a real
Uvicorn process, seeds 100 stored fake runs, warms 10 metadata reads, then measures
100 run-metadata GETs and 100 new enqueue POSTs using ten concurrent HTTP clients.
No worker or model is run. Cleanup stops the server and drops that database.

```sh
uv sync --frozen --all-groups
# Set SCOPECHECK_TEST_ADMIN_URL to your local disposable postgresql+psycopg admin URL.
uv run python scripts/benchmark.py --runs 100 --concurrency 10
```

p95 is the nearest-rank 95th percentile of all attempts, including errors. Error
counts/statuses and p50/max/duration are reported alongside p95. Targets are
strictly below 300 ms reads and 500 ms enqueue; missed latency targets remain
reported measurements, not discarded samples. Request errors produce a nonzero
exit. Hardware, versions, database size before/after, cache state, sample size and
concurrency are included. Seeding/warmups are excluded from phase latencies but
included in total elapsed time. This is one warm local trial; OS/PG caches are not
flushed, cold behavior and model execution are not measured, and collector/file
export is disabled during the benchmark. It is not a production SLA or an
optimization comparison.

## Coverage

```sh
uv run pytest --cov=scopecheck --cov-branch --cov-report=term-missing
uv run coverage json -o coverage.json
uv run python scripts/check_core_coverage.py coverage.json
```

The full command requires the disposable PostgreSQL admin URL. The checker enforces
80% **branch** coverage separately for policy, scorer and budget; it does not
substitute aggregate statement coverage. CI runs the same measurement and check.
Existing critical scenario, concurrency, access, fake e2e and process tests remain
required even when a percentage is high. External CLI/process code executes in e2e
but is not merged into the parent pytest coverage data; reported coverage can
understate that execution. Frozen dependency versions are in uv.lock.
