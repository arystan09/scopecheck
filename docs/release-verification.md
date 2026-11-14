# Milestone-10 release evidence — 2026-11-14

Required gates remain incomplete: live experiment, hosted HTTPS demo, real remote
CI evidence, published digest and screenshot/video. Nothing was committed, pushed,
published or provisioned. Existing milestone-9 changes were preserved.

Local verification used macOS arm64, Python 3.12.14, uv 0.13.7 and PostgreSQL 18.6.

- Frozen sync and `make check` passed (241 unit tests; dataset/CLI/lint/type checks).
- Full `pytest --cov=scopecheck --cov-branch --cov-report=term-missing`: **346 passed
  in 62.01 seconds**, combined coverage **85.10%**. Includes real PostgreSQL
  integration, encrypted restore and two separate-process e2e tests. The e2e runs
  `uv run python scripts/demo_smoke.py` and validates API/report comparisons.
- Development evidence generation repeated byte-for-byte after final formatting;
  expected counts, prompt hashes, artifact scans and new local links passed. No holdout
  model outputs were consulted or paid requests made.
- Ruff initially found one long line in the new generator. Formatting fixed it;
  lint, format and mypy passed afterward. No assertion or test was weakened.
- Docker build and Compose commands were attempted: `docker` is unavailable.
  No container behavior, Caddy/HTTPS, hosted memory or restart check is claimed.
- Dependency metadata reviewed for 59 installed distributions; native/image license
  review remains pending an actual image. Artifact content is synthetic; source
  paths are repository-relative. No credentials or local environment files are
  included. Public artifact publication is not authorized by preparing files.

See experiment.md for denominators and operations.md for pending hosting gates.
A fresh source export check is recorded below separately from a Git clean-checkout
claim: current work is uncommitted, and the user must review and commit it.

Fresh source export: copied only source, datasets, migration/test/script files and
package metadata into an isolated directory with no `.env` or existing virtualenv.
`uv sync --frozen --all-groups --offline` passed using the local dependency cache.
A new disposable PG18 database, API and worker then passed the exact demo smoke
script: five runs, comparison, repeatability and package 0.1.0 parity. All temporary
processes/database were cleaned up. This is not a network-from-scratch install or
post-commit Git checkout test; both distinctions are intentional.

Core branch checker passed: policy 100%, scorer 100%, budget 95.45%.
GitHub connector query for pull-request-triggered runs of commit
`4ffdfa3d172690b7e5701098bd4c1a298e89a201` returned an empty list. That query does not
cover push-triggered runs and cannot establish success for these uncommitted files.
