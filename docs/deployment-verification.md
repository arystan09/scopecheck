# Local milestone-9 verification — 2026-11-13

Hosting acceptance remains pending. These results are local, not a deployment
record. No image was published, cloud resource provisioned, or paid model called.

Environment: macOS 26.6 arm64, Python 3.12.14, local PostgreSQL 18,
PostgreSQL dump/restore clients 18.1 and age 1.2.1. Docker Engine/CLI is absent.
The existing local cluster uses the `scopecheck` administrator role; an initial
attempt with `postgres` failed because that role does not exist. The corrected
run passed without changing database permissions or test assertions.

Passed:

- `uv sync --frozen --all-groups` and `make check`: frozen lock, all three dataset
  validations/replays, Ruff lint/format, mypy, both CLI help checks, 241 unit tests.
- `uv run pytest --cov=scopecheck --cov-branch --cov-report=term-missing`:
  **346 passed**, including real PostgreSQL and separate-process e2e tests,
  in 63.32 seconds. Overall combined statement/branch coverage: 85.06%.
- `uv run coverage json -o coverage.json` followed by
  `uv run python scripts/check_core_coverage.py coverage.json`: policy/scorer
  branch coverage 100%; budget 95.45%; all exceed 80%.
- `uv run pytest tests/integration/test_restore.py -q -s`: encrypted dump and
  restore subprocesses passed against a freshly created disposable database.
  A repeat measured **0.084573 seconds** for decrypt/create/restore/verify.
  Counts and SHA-256 hashes matched for all 12 public tables, including one dataset,
  one config, one completed run, 12 attempts/results, two workspaces and three
  hashed tokens. Wrong identity failed before database creation; existing
  destination was refused. The test removed only its own disposable databases.
- Production Compose resolved using standalone Docker Compose v5.7.1 and passed
  `scripts/check-production.py`'s validation function. Dummy domain/digest and
  non-secret configuration values were used. This did not launch containers.
- Backup/restore CLI help, uploader shell syntax and Git whitespace checks.

Unavailable: `docker build -t scopecheck:local .` returned command not found.
Container smoke, Caddy validation/HTTPS, production resource measurements,
API/worker restart and hosted boundary checks were not run. Remote CI and the
manual release workflow are pending. Local restore excludes backup download,
provisioning and incident diagnosis; its small warm fixture is not a recovery SLA.

The proposed Frankfurt 2 GiB VM remains provisional. Before spending or publishing,
the owner must approve the VM, protected EU backup bucket, domain and GHCR release,
and supply access. Record the actual URL, UTC timestamp, immutable image digest,
memory observation, off-VM backup/restore result and final cost after deployment.
