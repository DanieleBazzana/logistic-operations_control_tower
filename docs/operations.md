# M07 local operations runbook

## Service boundaries

The local production-like topology is five services: PostgreSQL, an explicit
`migrate` job, an explicit `bootstrap` job, FastAPI, and Streamlit. Only PostgreSQL
has a persistent volume. FastAPI and Streamlit are independent, non-root images;
application startup does not create schema, generate data, ingest, or detect.

The future deployment shape is the same boundary on separate Google Cloud Run
services with `min instances=0` and Neon PostgreSQL. Cloud/provider configuration,
credentials, IAM, and provisioning are deliberately not included in this local
milestone. Alembic remains a deployment job and deterministic bootstrap remains a
separate operator-approved step.

## Cost guardrails before any cloud deployment

Treat cloud deployment as an operator-owned billing decision, not as a promise of
free hosting. Cloud Run requires a billing-enabled Google Cloud project; the intended
baseline is `min instances=0` for scale-to-zero, but request compute, logs, network
traffic/egress, and related services may still be billable. Neon Free is only an
evaluation assumption. Current prices, quotas, connection limits, storage, retention,
and egress rules are dynamic and are `PRICE_NOT_VERIFIED` here; check the [Cloud Run
pricing page](https://cloud.google.com/run/pricing), [Cloud Run billing guidance](https://cloud.google.com/run/docs/troubleshooting#billing-account),
and [Neon pricing](https://neon.tech/pricing) immediately before provisioning.

Before approval, record the dated price/limit verification, region choice, expected
request/database traffic, and egress/overage risks. Configure budget alerts using
[Google Cloud budgets](https://cloud.google.com/billing/docs/how-to/budgets), while
remembering that alerts do not cap spend. There is no guaranteed `$0` outcome and
this repository makes no paid commitment or cloud resource change.

## Start and stop

Use synthetic values in the shell; do not source an unknown `.env` file:

```bash
export POSTGRES_PASSWORD=synthetic-local-password
./scripts/bootstrap_demo.sh
docker compose up -d api dashboard
docker compose ps
```

Stop services without deleting the database volume:

```bash
docker compose stop api dashboard
```

Delete only the local stack and its volume when it is disposable:

```bash
docker compose down --volumes
```

## Health and logs

- API `/api/v1/livez` is process liveness and does not touch PostgreSQL.
- API `/api/v1/readyz` and `/api/v1/health` run `SELECT 1` and return 503 if the
  database is unavailable.
- Streamlit `/_stcore/health` is the dashboard process probe.
- API responses contain `X-Request-ID`; JSON request logs contain only method,
  path, status, duration, and the request ID. Query strings, request bodies,
  authorization values, database URLs, and passwords are not logged.

Inspect local container logs only when needed, and do not paste logs containing
local environment details into public artifacts:

```bash
docker compose logs --tail=100 api dashboard
```

## Public demo safety boundary

Set `PUBLIC_DEMO_READ_ONLY=true` for the API and dashboard services. FastAPI is
the authoritative server-side boundary: every lifecycle PATCH is rejected with
403 before the lifecycle service is called. The dashboard independently hides the
lifecycle form. GET dashboard, KPI, queue, detail, and operational endpoints remain
available anonymously. This flag is not authentication or RBAC.

Keep the flag `false` only for a private local development workflow where controlled
lifecycle transitions are intentionally being exercised.

## Migration and bootstrap procedure

1. Build or select the reviewed API image.
2. Wait for PostgreSQL health.
3. Run `alembic upgrade head` through the one-shot `migrate` service.
4. Run the deterministic `bootstrap` service with explicit seed and `AS_OF`.
5. Start API and dashboard only after both jobs succeed.

A repeat bootstrap is expected to be safe: identical source identifiers are skipped
by ingestion and active exception findings are refreshed without duplicate history.
Never add these jobs to a web container entrypoint or healthcheck.

## Backup and restore drill

Against an explicitly disposable loopback PostgreSQL target, export:

```bash
export POSTGRES_PASSWORD=synthetic-backup-password
export TEST_DATABASE_URL=postgresql+psycopg://control_tower:synthetic-backup-password@127.0.0.1:5432/control_tower_test
export ALLOW_DESTRUCTIVE_TEST_DB=1
./scripts/backup_restore_drill.sh
```

The script validates PostgreSQL, loopback, query-free, test-named target constraints,
creates a unique Compose project, runs `pg_dump -Fc`, restores into a sibling database,
checks the restored public table count, and removes only its own project/volume. It
never prints the URL or password. This is a drill against disposable local resources,
not a production backup policy.

## Rollback

1. Stop API and Streamlit; keep PostgreSQL running and preserve the volume.
2. Select the last reviewed image/source revision. Do not mutate `main` or remote
   state as part of a local rollback.
3. Inspect its Alembic head and run only the migration path approved for that revision.
   Prefer forward-compatible migrations; use `alembic downgrade` only when the
   migration explicitly supports safe reversal and the data-loss impact is accepted.
4. Restart API and dashboard with the prior image and run `/readyz`, KPI, queue, and
   detail checks.
5. Re-run the public mutation rejection check if the deployment is public.
6. Record the exact revision, migration result, and remaining data compatibility risks.

There is no automatic destructive rollback, schema creation at startup, or cloud
provisioning in this repository.
