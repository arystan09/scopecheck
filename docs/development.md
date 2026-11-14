# Development and reproduction

Python 3.12, uv 0.13.7; PostgreSQL 18 for integration/e2e. Recovery tests also need
PG18 pg_dump/pg_restore and age on PATH. No provider key is needed. From repo root:

```sh
uv sync --frozen --all-groups
make check
uv run python scripts/release_evidence.py
# Configure SCOPECHECK_TEST_ADMIN_URL for a local disposable PG18 admin first.
uv run pytest --cov=scopecheck --cov-branch --cov-report=term-missing
uv run coverage json -o coverage.json
uv run python scripts/check_core_coverage.py coverage.json
```

Tests create/drop only uniquely named disposable databases. Native/Compose startup
and `uv run python scripts/demo_smoke.py` are described in README. The process e2e
test invokes that exact smoke script against a fresh migrated DB and separate API
and worker. It asserts API requests, report counts, comparisons and version parity.
It is not container or hosted HTTPS verification. See release-verification.md for
what was actually run, and operations.md for cloud authorization requirements.

## Example operator API calls

Use a local token from bootstrap; never paste it in tracked files. The actual smoke
script exercises these routes and validates responses. JSON IDs below come from
responses; variable names are shell placeholders, not invented valid identifiers.

```sh
curl --fail http://127.0.0.1:8000/health/ready
curl --fail -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  --data-binary @datasets/dev/basic.json http://127.0.0.1:8000/api/v1/datasets
curl --fail -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"fixture":"safe"}' http://127.0.0.1:8000/api/v1/configs
# Set DATASET_ID and CONFIG_ID from responses; use a fresh idempotency key.
curl --fail -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $REQUEST_KEY" \
  -d "{\"dataset_id\":\"$DATASET_ID\",\"config_id\":\"$CONFIG_ID\"}" \
  http://127.0.0.1:8000/api/v1/runs
curl --fail -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8000/api/v1/runs/$RUN_ID/report"
```

Public mode rejects normal writes even with an editor token; only fixed bounded
`POST /demo/runs` admissions are anonymous. Reports still require a scoped reader.
Do not use the operator examples against public mode expecting successful writes.

## Authorship

Implementation, tests and documentation were developed with substantial AI coding
assistance through Codex. Generated suggestions require human review. Tool-run
results are attributed to local verification, not an invented human author or
remote CI. No paid experiment, external publication or hosting is implied.
