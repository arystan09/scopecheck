# ScopeCheck

Milestone 1 of the approved ten-stage baseline: a Python 3.12 package, keyless
settings, FastAPI liveness endpoint, and CLI help. The useful fake-provider MVP is
milestone 4. No provider, domain tool, database client, table, migration, worker,
or UI is implemented yet. The full later-stage roadmap was not present in the
starting workspace; this foundation does not redefine it.

## Local quickstart

Prerequisites: Python 3.12, uv 0.13.7, and network access for the first install.
Run from the project root:

```sh
uv sync --frozen --all-groups
uv run scopecheck --help
uv run uvicorn scopecheck.app:create_app --factory --host 127.0.0.1 --port 8000
```

In another terminal:

```sh
curl --fail http://127.0.0.1:8000/health/live
# {"status":"ok"}
```

Stop with Ctrl-C. Liveness asserts the HTTP process is alive; it does not check
database or provider readiness. `SCOPECHECK_MODE` defaults to `fake`; any other
value prevents app creation. No provider keys or default secrets are configured.
The app reads environment variables, not `.env` files automatically; `.env.example`
documents the only setting. Unknown constructor fields are rejected. Ordinary
process environment variables are ignored unless they match a declared setting.
Validation error text hides input values; raw Pydantic `errors()` data can contain
inputs and must not be logged or returned to clients.

## Checks

```sh
uv sync --frozen --all-groups
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -m "not integration and not e2e"
uv run scopecheck --help
```

`make check` also checks lock freshness. The lock freezes the resolved runtime
and development dependency versions. Unit tests exercise invalid settings,
credential non-exposure, HTTP behavior in-process, and the installed CLI.
`unit`, `integration`, and `e2e` are strict registered pytest markers. There are
no database or end-to-end pytest tests yet; no database claims are made.

## Container quickstart and verification

Requires Docker Engine/Desktop running, Compose v2, curl, and OpenSSL. Supply a
fresh local database password; there is intentionally no default:

```sh
export POSTGRES_PASSWORD="$(openssl rand -hex 24)"
docker compose config
docker build -t scopecheck:local .
sh scripts/check-container.sh
docker compose up -d
curl --fail http://127.0.0.1:8000/health/live
docker compose down
unset POSTGRES_PASSWORD
```

`docker compose config` prints the resolved local password: do not share its output.
Keep the password in a local secret store if reusing the database volume; changing
it does not change an already initialized database's password. Compose preserves
the database volume on shutdown. The app does not use or depend on `db` yet.
PostgreSQL is not published to the host. The app port binds only to loopback.

The smoke script starts the built image on an ephemeral loopback port, asserts UID
10001, requests liveness over real HTTP, and removes only its own container.
`make container-check` runs Compose validation, build, and this smoke check.
The Dockerfile installs the frozen production dependencies and runs as UID/GID
10001. Python's standard library supplies the image's health probe.

The multi-stage install follows the [uv Docker guide](https://docs.astral.sh/uv/guides/integration/docker/).
The PostgreSQL 18 volume uses `/var/lib/postgresql`, as required by the
[official image](https://hub.docker.com/_/postgres).

GitHub Actions runs the same checks and container smoke test. Remote CI status
is pending until an actual run; a workflow file alone is not execution evidence.

## Verification recorded for this delivery

On 2026-11-08, using Python 3.12.14 and uv 0.13.7: frozen sync, lock freshness,
Ruff lint/format, mypy, all 11 collected unit tests, and installed CLI help passed.
The local Uvicorn quickstart returned HTTP 200 with `{"status":"ok"}` over
loopback; the server was then stopped. No tests were skipped.

Docker was not installed on the verification host. Both requested Docker commands
were attempted and failed with `command not found`; Compose validation, image
build, runtime non-root identity, and container HTTP remain unverified. The
container commands above are the intended reproduction path, not a claimed pass.
CI remains pending. Milestone 1 acceptance is incomplete until those gates pass.
The starting directory was empty and had no Git metadata or remote; no existing
changes were overwritten, and nothing was committed or pushed.
