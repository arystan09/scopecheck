# Milestone 9 — deployment and recovery runbook

**Hosting acceptance is pending.** No VM, bucket, domain, image publication or
hosted HTTPS test is authorized by preparing these artifacts. Do not run cloud
mutation commands until the owner approves the resources below and supplies access.
No schema migration is added. Keep the ten-stage baseline; stage 10 is not included.

## Resource proposal and cost

Proposed region: **eu-central-1 (Frankfurt)**. One Linux/x86_64 Lightsail instance,
provisionally 2 GiB RAM, 2 vCPU, 60 GB SSD, public IPv4 and attached static IP.
The listed bundle is **USD 12/month**, excluding taxes; verify availability and price
at authorization time. A 2 GiB choice is provisional, not a measured capacity claim.
[Lightsail bundle pricing](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-bundles.html).

Budget estimate: **USD 15/month plus domain registration/taxes**, assuming under
1 GiB retained encrypted backups and low traffic. The USD 3 allowance is a planning
margin, not an S3 quotation. Transfer overages, larger backups and snapshots are extra.
Use an existing owned domain/subdomain where possible. Approve a separate private S3
bucket in the EU (preferably a separate backup account), seven-day Object Lock,
versioning, block-public-access, TLS-only bucket policy and SSE-S3. The reviewed
default retention configuration is `deploy/backup-object-lock.json`: seven days in
COMPLIANCE mode, which cannot be shortened even by the account root user. Apply it
only to the approved backup bucket; do not apply cloud configuration during local tests.
Give the VM only
PutObject to `scopecheck/*`, no read/delete/retention-bypass permission; keep recovery
credentials and the age private identity off-host. Retain at least seven daily backups.
Apply `deploy/backup-lifecycle.json` (expire current objects after eight days, noncurrent
versions after one more day); Object Lock prevents premature deletion. Lifecycle
expiry is asynchronous, so storage can exceed eight days.
[Object Lock](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock-configure.html).

## Release and bootstrap (only after approval)

Configure the GitHub `release` environment with required reviewers. The manual
Release image workflow requires `authorize_publication=true`, runs the reusable
Checks workflow, rebuilds/tests the image to push, and records its GHCR digest.
It does not deploy. Do not dispatch it as part of local verification. The target is
Linux amd64; no multiarchitecture claim. Retain the prior digest in GHCR and the
previous deployment env file in the operator's private configuration store.
[GHCR publication](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images).

On the approved Linux VM, install Docker Engine/Compose from official packages;
use a dedicated SSH administrator, OS security updates and disk monitoring. Firewall:
80/443 public, SSH restricted to owner IP, no 5432/8000/metrics/collector ingress.
Point the owned DNS A record at the static IP; add AAAA only if correctly configured.
Copy this reviewed repository revision to `/opt/scopecheck`, then:

```sh
cd /opt/scopecheck
cp deploy/.env.example deploy/.env
chmod 600 deploy/.env
# Edit private deploy/.env: lowercase GHCR_REPOSITORY, validated IMAGE_SHA256,
# owned DEMO_DOMAIN, ACME_EMAIL and POSTGRES_PASSWORD from openssl rand -hex 32.
# No OPENAI_API_KEY or SCOPECHECK_OPENAI_API_KEY anywhere on this host.
docker compose -f deploy/compose.production.yaml config --quiet
uv run python scripts/check-production.py
docker compose -f deploy/compose.production.yaml pull
docker compose -f deploy/compose.production.yaml run --rm --no-deps caddy caddy validate --config /etc/caddy/Caddyfile
docker compose -f deploy/compose.production.yaml up -d
curl --fail https://YOUR_DOMAIN/health/live
curl --fail https://YOUR_DOMAIN/health/ready
```

Python 3.12/uv with `uv sync --frozen --all-groups` is required for host operational
scripts; do not build images on the small VM. Authenticate read-only to private GHCR
if needed. Compose uses `deploy/.env` automatically; never share resolved `config`
output, which includes the DB password. API/worker/migrate use the same immutable
image. PostgreSQL remains major-pinned to 18; Caddy to 2 (record their resolved digests
in the deployment record and review updates). API/worker have no external network,
provider key or token. Caddy handles ACME/HTTPS and blocks `/metrics`; the app itself
also denies metrics in public mode. Caddy admin is disabled; no access log is enabled.

Memory limits: API 384 MiB, worker 384 MiB, DB 512 MiB, Caddy 128 MiB; migration 256
MiB runs before API/worker. These are bounds, not measurements. Observe `docker stats
--no-stream`, `free -m`, OOM counters and disk use during the fake demo and a backup
before approving 2 GiB. Save UTC timestamp, URL, commit, image digest, all container
versions, peak memory and monthly estimate in the deployment record. Do not silently
upgrade the paid bundle if memory is insufficient.

## Hosted acceptance and read-only inspection

```sh
curl --fail https://YOUR_DOMAIN/ui
curl --fail -X POST https://YOUR_DOMAIN/demo/runs -d '{}'
# Save returned ID; wait with a 60-second deadline using the reader token below.
docker compose -f deploy/compose.production.yaml exec app scopecheck db token-create \
  --workspace scopecheck-public-demo --role reader
```

Give the one-time reader token to the reviewer privately; paste it into `/ui`.
Tokens remain in browser memory. Inspect the completed synthetic report (24 cases,
10 legitimate goals); verify real rendering, not just HTTP 200. Anonymous report
reads are intentionally unavailable: the pre-existing public path admits fixed work
but operator reports require a scoped reader. No anonymous read API is added.
Verify `/metrics` returns 404, `POST /demo/runs` with `{"model":"override"}` returns
422, and authenticated normal write APIs return 403. Check DB port unreachable from
outside. Use no live key and make no live request. Restart app/worker, verify readiness
and retained run/report again. Quotas remain two queued/running, one admission per
five seconds and 100 lifetime admissions; restart does not reset them.

## Encrypted backup

Install PostgreSQL **18** client tools, age, AWS CLI and the frozen Python environment
on the host. Create an age identity on a separate recovery machine:

```sh
umask 077
age-keygen -o recovery.agekey
age-keygen -y recovery.agekey
```

Only copy the public `age1...` recipient to the VM. Configure a mode-600 libpq service
and password file for the private DB using the Docker bridge address (refresh it
before each backup, since recreation can change it), no published DB port:

```sh
cd /opt/scopecheck
export PGHOST="$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$(docker compose -f deploy/compose.production.yaml ps -q db)")"
export PGPORT=5432 PGUSER=scopecheck PGDATABASE=scopecheck
export PGPASSFILE=/etc/scopecheck/pgpass
# pgpass: bridge-address:5432:scopecheck:scopecheck:password (chmod 600)
export AGE_RECIPIENT='OWNER_PUBLIC_AGE_RECIPIENT'
export BACKUP_BUCKET='OWNER_APPROVED_BUCKET' AWS_DEFAULT_REGION=eu-central-1
sh deploy/backup-upload.sh
```

Prefer a wildcard host in the private pgpass entry if refreshing addresses, with
scope limited by port/database/user. Schedule daily via a root-owned systemd timer
(or cron) that refreshes PGHOST, loads these private settings, executes the script
from the repository and alerts on nonzero exit/missed backup. Example cron:
`0 2 * * * /etc/scopecheck/daily-backup` where that mode-700 wrapper executes the
commands above. Test it manually before enabling the timer. AWS credentials must be
restricted to the approved bucket prefix. No cloud destination is created by scripts.

The dump and row inventory share one exported repeatable-read snapshot, so worker
writes during backup cannot produce mismatched hashes. Plaintext dump/manifest exist
briefly in a mode-700 temp directory and are removed on normal exit; put TMPDIR on
an encrypted filesystem, restrict host access, and clean abandoned temp files after
crash. Hashes cover every public table including immutable data and token hashes.
Age encryption happens before upload; the uploader sends only ciphertext. It never
prunes backups itself. Monitor S3 lifecycle, Object Lock and last successful upload
independently; a successful local encryption is not proof of off-VM durability.

## Fresh disposable restore drill

Download a trusted archive using separate recovery credentials. **Never restore an
untrusted SQL archive**: pg_restore can execute SQL. Run on a recovery workstation
with PostgreSQL 18 clients, age, Python/uv and an empty disposable PG18 server. A local
Docker destination can be started separately (generated password, no active volume):

```sh
export PGPASSWORD="$(openssl rand -hex 32)"
docker run -d --name scopecheck-recovery -p 127.0.0.1:55433:5432 \
  -e POSTGRES_PASSWORD="$PGPASSWORD" postgres:18
export PGHOST=127.0.0.1 PGPORT=55433 PGUSER=postgres PGDATABASE=postgres
# Wait until pg_isready succeeds before continuing.
export RESTORE_DB="scopecheck_restore_$(uv run python -c 'import uuid; print(uuid.uuid4().hex)')"
uv run python scripts/restore_check.py --archive /PRIVATE/backup.age \
  --identity /PRIVATE/recovery.agekey --database "$RESTORE_DB" \
  --confirm-disposable "$RESTORE_DB"
```

The checker requires loopback, PG18, admin database `postgres`, matching confirmation
and a new `scopecheck_restore_<32 hex>` database. Existing names are refused. It
verifies ciphertext integrity through age, dump SHA-256, then all restored table
counts/hashes. It records elapsed decrypt/create/restore/verify seconds; download,
VM creation and incident diagnosis are excluded. It never drops a database; failed
restores remain for inspection. Remove only the explicitly disposable container
with `docker rm -f -v scopecheck-recovery` after evidence review. Also test a wrong
identity and an existing destination; both must fail. Record off-VM download/restore
separately from any local drill. Daily cadence implies up to 24 hours lost updates;
no recovery-time SLA is claimed.

## Restart, upgrade, rollback and shutdown

`docker compose -f deploy/compose.production.yaml restart app worker` preserves data.
Before an upgrade, take/verify an off-VM backup, record previous image digest and
schema revision (`exec app alembic current`), and review migration compatibility.
Then update IMAGE_SHA256, pull, stop app/worker, run `run --rm migrate`, and `up -d`.
Repeat hosted acceptance. Keep the old digest; do not remove it from GHCR.

Image rollback **does not reverse migrations**. Only set the previous digest and
restart if its code is verified compatible with the current schema. Otherwise stop
writes and restore a pre-upgrade backup to a fresh database on a recovery host,
validate it, and explicitly switch over after owner approval; never restore over
the active DB or blindly `alembic downgrade`. This stage adds no schema changes.

`docker compose -f deploy/compose.production.yaml down` stops services, preserves
volumes and does not stop AWS billing. Do not use `down -v` on the active deployment.
For final shutdown, verify/export the retained encrypted backup and recovery key,
then owner deletes the VM, unused static IP, snapshots and other billable resources;
remove DNS and review the billing console. Locked backups continue billing until
retention permits deletion. A stopped Lightsail instance remains billable.

## Verification record

See [local verification](deployment-verification.md) for checks actually executed. Until explicitly recorded,
HTTPS URL, GHCR digest, observed VM memory, off-VM upload/restore, hosted restart and
rollback are **pending**, not passed. This file is a runbook, not execution evidence.
