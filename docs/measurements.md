# Local measurement record — 2026-11-12

This is measured milestone-8 evidence, not a production SLA or a performance
improvement claim. All data is synthetic. No paid requests were made.

## Environment and benchmark

Actual command:

```sh
uv run python scripts/benchmark.py --runs 100 --concurrency 10
```

| Environment | Observed value |
| --- | --- |
| Hardware | Apple M5, arm64, 10 logical CPUs, 17,179,869,184 bytes RAM (16 GiB) |
| OS | macOS 26.6 |
| Python / PostgreSQL | 3.12.14 / native local PostgreSQL 18.6 |
| App / FastAPI / Uvicorn | 0.1.0 / 0.162.2 / 0.54.0 |
| SQLAlchemy / psycopg / OpenTelemetry SDK | 2.0.57 / 3.3.8 / 1.45.2 |
| Transport | Real loopback HTTP, one Uvicorn process, local PostgreSQL |
| Dataset / initial stored runs | Basic 12-case fake fixture / 100 |
| Final stored runs | 200 (100 measured enqueues added) |
| Database size before / after measured phases | 8,943,295 / 9,172,671 bytes |
| State | Warm from sequential seed enqueues and 10 metadata-read warmups; OS/PG caches not flushed |
| Instrumentation | JSON logs and in-process spans enabled; file/OTLP export disabled |
| Worker/model execution | None; excluded deliberately |
| Total elapsed including setup/warmup | 1.286478 seconds |

| Phase | Samples / concurrent clients | Phase duration | p50 | p95 | Maximum | Errors | Target |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Run metadata reads | 100 / 10 | 0.112027 s | 8.236 ms | 22.804 ms | 29.445 ms | 0 | <300 ms: met |
| New run enqueue | 100 / 10 | 0.206992 s | 16.766 ms | 29.666 ms | 35.907 ms | 0 | <500 ms: met |

Nearest-rank p95 includes every attempted request, including errors if any. This
single warm local trial has a small sample, no WAN, no collector/file-export cost,
no cold-cache experiment and no model execution. There is no baseline optimization
comparison or throughput/capacity guarantee. The raw report is generated at
`outputs/milestone8-benchmark.json` locally; this checked-in record preserves the
actual numbers. A missed target would be reported without dropping slow/error
samples; neither latency target was missed in this trial.

## Branch coverage

The exact full-suite command is:

```sh
uv run pytest --cov=scopecheck --cov-branch --cov-report=term-missing
uv run coverage json -o outputs/milestone8-coverage.json
uv run python scripts/check_core_coverage.py outputs/milestone8-coverage.json
```

Policy: 6/6 branches, **100%**. Scorer: 26/26 branches, **100%**. Budget:
21/22 branches, **95.45%**. Each exceeds the 80% per-module branch target.
The first measurement was 77.27% for budget and failed the checker. We added real
PostgreSQL regression tests for deadline refusal, missing active attempts,
reservation configuration mismatch and settlement configuration mismatch; those
prove rejected calls do not consume/release budget. No assertions were weakened
and the target was not lowered. The remaining uncovered budget branch rejects
missing/invalid owner-budget metadata on a manually malformed durable run.

Coverage percentage is not exhaustive security proof, and compound boolean
conditions may require more scenarios than the branch count suggests. Critical
role, cross-workspace, canary, deny-all, recovery, reservation-race and fake process
tests remain mandatory. Separate CLI/process e2e execution is not merged into the
parent pytest coverage report; it is verified behavior but can be undercounted.

## Linked trace and operational failures

The process e2e launches real Uvicorn and a separate worker. It selects the exact
HTTP-created safe run returned by the fake smoke and verifies this chain has one
trace ID and matching parent span IDs:

`http.request → worker.run → case.execute → provider.request`

The API-to-worker edge comes from stored PostgreSQL job context, not shared memory.
Sanitized evidence is saved locally to `outputs/milestone8-process-trace.json`.
CLI-seeded jobs legitimately start worker-root traces because they have no HTTP
parent. An initial test selected an arbitrary provider span and sometimes chose
such a CLI job; selection now uses the specific HTTP run ID, preserving the full
linkage assertion.

A deliberate fake-provider timeout experiment runs all 12 basic cases with a
provider stub raising `TimeoutError`. The spans show provider `error=timeout`,
case `status=limited`, and the API report shows `completed_with_errors`; failures
are not passes. Prometheus reports 12 persisted timeout failures. Payload-bearing
exception text is absent from logs/spans. This is controlled fault injection,
not an observed production outage or a real model timeout. The initial metrics
query missed these errors because JSON-null provenance is distinct from SQL NULL;
the regression test caught it, and the query now handles both. Evidence is in
`outputs/milestone8-linked-trace.json`.

A separate local readiness experiment targets a closed PostgreSQL port. Readiness
returns 503 `database_unavailable` and records that bounded error on the HTTP span,
while liveness remains 200. Database credentials and raw connection diagnostics
are absent from the response and trace. This demonstrates the dependency failure
without leaking payloads or pretending the service is ready.

## Remaining gates

Docker is not installed locally: Compose validation, image build/non-root health,
and the optional Jaeger collector/dashboard execution are unavailable. Native
HTTP/process evidence does not substitute for these container checks. The remote
CI configuration includes the full real PostgreSQL coverage run and core threshold
check, but a fresh remote CI run has not been observed. No live/paid smoke was run.

Final full-suite result: **333 passed**, no selected tests skipped, with **85.10%**
aggregate statement-and-branch coverage across the package. This aggregate is not
the per-module branch target above. The final run includes 229 unit tests,
102 real PostgreSQL integration tests and two process e2e tests. Frozen sync,
Ruff lint/format, mypy, CLI help, frozen dataset validation/replay and source/wheel
builds also passed. The earlier failed trace-selection test and below-target initial
budget measurement are recorded above rather than concealed.
