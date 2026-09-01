# Supply Chain Operations Control Tower

> A production-grade portfolio demo that turns fragmented supply-chain signals into a prioritized, explainable exception queue for operations teams.

[Live Dashboard](https://control-tower-dashboard-xo5wjavqmq-ey.a.run.app) · [API docs](https://control-tower-api-xo5wjavqmq-ey.a.run.app/docs) · [Portfolio copy](docs/portfolio-copy.md)

![Operations Control Tower overview](docs/assets/control-tower-overview.jpg)

**Public Demo · Read Only.** The deployed dashboard and API expose the operational
read surface. Lifecycle mutations are available only in local/development
environments and are blocked server-side in the public deployment.

## Recruiter snapshot

This project demonstrates an operations-oriented data product: synthetic OMS, WMS,
ERP/procurement, and carrier signals are normalized into PostgreSQL, evaluated by
deterministic exception rules, and exposed through a versioned FastAPI contract and
Streamlit control-tower UI. Each finding carries severity, business impact, root
cause, recommended action, and lifecycle history so an operator can move from signal
to decision quickly.

## Demo gallery

The public demo is intentionally read-only and uses synthetic operational data.

![Exception queue with operational detail](docs/assets/control-tower-exception-detail.jpg)

![Exception detail and lifecycle history](docs/assets/control-tower-lifecycle.jpg)

## Business problem and solution

Operations teams often have orders, warehouse inventory, procurement, and carrier
signals split across systems. That makes it difficult to identify the few issues
that need intervention, explain their business impact, and track ownership through
resolution.

This portfolio project consolidates synthetic OMS, WMS, ERP/procurement, and carrier
data in PostgreSQL. Deterministic exception rules identify six actionable conditions
with explainability, severity, revenue/order impact, root cause, and recommended
action. A versioned FastAPI API provides the operational read surface and controlled
exception lifecycle; a Streamlit dashboard presents the same contracts to users.
The public read-only demo uses the existing Cloud Run deployment boundary and Neon
PostgreSQL. The complete release flow is also reproducible locally and is backed by
disposable PostgreSQL verification; this repository does not provision or contain
cloud credentials.

## Stack

- Python 3.11+
- PostgreSQL 16 via Docker Compose for local verification; Neon PostgreSQL for the
  existing deployed runtime
- SQLAlchemy 2, Alembic, and psycopg
- FastAPI and Uvicorn
- Streamlit, Pandas, and HTTPX
- Pytest and Ruff

For the product requirements, release evidence, and media-review record, see
[docs/release-review.md](docs/release-review.md). Ready-to-reuse CV, LinkedIn,
GitHub, and interview language is collected in [docs/portfolio-copy.md](docs/portfolio-copy.md).

## Local bootstrap

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
cp .env.example .env
```

`.env` is local and untracked. Replace the placeholder in `POSTGRES_PASSWORD` with
a local-only value. The application and Compose jobs pass `POSTGRES_HOST`,
`POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, and the raw `POSTGRES_PASSWORD`
separately; application settings construct the SQLAlchemy URL safely, so a password
containing URL-reserved characters does not need to be manually encoded for Compose.
`DATABASE_URL` remains an optional, already-encoded explicit override for native
commands; it is not required by the standard `.env` setup.

```bash
set -a
. ./.env
set +a
docker compose up -d postgres
```

The Compose service is PostgreSQL only. Apply the current migration set before
using a new database:

```bash
.venv/bin/python -m alembic upgrade head
```

`head` means the latest migration revision in this repository, not a milestone-
specific or manually selected revision.

## Complete product flow

The release path is intentionally explicit and has no hidden schema creation or
HTTP ingestion/detection endpoint:

1. Start a disposable PostgreSQL service.
2. Run `alembic upgrade head`.
3. Generate a deterministic source bundle from a seed and timezone-aware `as_of`.
4. Ingest the bundle atomically; repeat ingestion to prove source-ID idempotency.
5. Run the six exception rules at an explicit `as_of`; repeat detection to prove
   active-finding deduplication and refresh behavior.
6. Read orders, inventory, procurement, shipments, exceptions, and KPIs through
   `/api/v1`; mutate exception lifecycle only through its controlled status route.
7. Run the dashboard through the API boundary, with no direct database access.

### Deterministic generation and ingestion

Generate a reproducible multi-source bundle (the default output is Git-ignored):

```bash
.venv/bin/python -m control_tower.synthetic generate \
  --output-dir data/generated \
  --seed 20250301 \
  --as-of 2025-01-15T12:00:00Z
```

The bundle contains stable sorted UTF-8 CSVs for `oms/`, `wms/`, `erp/`, and
`carrier/`, plus a manifest with row counts, seed, UTC `as_of`, content identity,
and six ordinary source-data scenarios. It does not create exception rows.

After `alembic upgrade head`:

```bash
.venv/bin/python -m control_tower.ingestion ingest --input-dir data/generated
```

Ingestion validates all artifacts before opening the write transaction, normalizes
enums/timestamps/booleans/Decimal values, checks joins and duplicate conflicts,
and supports identical source-ID re-ingestion. A newer inventory snapshot updates
its natural key; stale or equal-time conflicting snapshots are rejected.

### Detection and lifecycle

Run detection explicitly against the configured PostgreSQL database. `--as-of` is
required and must include a timezone; output is normalized to UTC. `DATABASE_URL`
comes from application settings, or a disposable URL can be supplied directly:

```bash
.venv/bin/python -m control_tower.exceptions \
  --as-of 2025-01-15T12:00:00Z

# Optional explicit database override:
.venv/bin/python -m control_tower.exceptions \
  --database-url "$DATABASE_URL" \
  --as-of 2025-01-15T12:00:00Z
```

The engine implements exactly `SLA_BREACH_RISK`, `INVENTORY_SHORTAGE`,
`STOCKOUT_RISK`, `INVENTORY_MISMATCH`, `SUPPLIER_DELAY`, and `SHIPMENT_DELAY`.
A repeated run refreshes active findings without changing lifecycle state or adding
history noise. A new finding starts `OPEN`; legal paths are
`OPEN -> ACKNOWLEDGED -> IN_PROGRESS -> RESOLVED`, with `DISMISSED` as an alternate
terminal state. `exception_history` is append-only.

### API

Start the API after migration, ingestion, and detection:

```bash
.venv/bin/python -m uvicorn control_tower.api.app:app \
  --host 127.0.0.1 --port 8000 --no-server-header
```

Available versioned routes:

- `GET /api/v1/health`
- `GET /api/v1/livez` and `/readyz`
- `GET /api/v1/orders` and `/orders/{source_order_id}`
- `GET /api/v1/inventory`
- `GET /api/v1/purchase-orders`
- `GET /api/v1/shipments`
- `GET /api/v1/exceptions` and `/exceptions/{exception_id}`
- `PATCH /api/v1/exceptions/{exception_id}/status`
- `GET /api/v1/kpis/summary`

Collections use stable ordering, page sizes from 1 through 100, exact filters,
inclusive timestamp bounds, and repeated status parameters with OR semantics.
Timestamps are timezone-aware RFC3339 values normalized to UTC. Money has two
fractional digits; quantities have three. The status patch requires a nonblank
actor and a reason for terminal states. Invalid input is 422, a missing record is
404, an illegal transition is 409, and database unavailability is 503 without SQL
or credential details.

The KPI response includes the eight operational dashboard fields:
`orders_processed`, `sla_performance_pct`, `open_exceptions`,
`critical_exceptions`, `revenue_at_risk`, `stockout_risks`, `supplier_delays`, and
`shipment_delays` (as well as supporting order counts). Its optional timezone-aware
`as_of` is normalized and echoed. Revenue at risk is a finding-level sum, so an
order in multiple findings may contribute more than once.

### Dashboard

With PostgreSQL, data, and the API ready, run in a second terminal:

```bash
API_BASE_URL=http://127.0.0.1:8000/api/v1 \
  .venv/bin/python -m streamlit run src/control_tower/dashboard/app.py
```

The dashboard renders all eight KPIs, a paginated/filterable Exception Queue,
CSV export of all matching pages, supplier purchase-order context, exception detail,
immutable lifecycle history, and the controlled lifecycle form. It uses only the
FastAPI boundary and invalidates session-scoped read data after a successful status
mutation. The public demo is read-only: it exposes the existing GET/API and dashboard
read surface and does not permit lifecycle mutations. Local development can enable
the controlled lifecycle form. There is no authentication/RBAC, direct database access
from the dashboard, forecasting, external connector framework, ingestion controls,
detection controls, or Excel export.

## Canonical release verification

The release gate uses a fresh, isolated Compose project and volume. It never reads
`.env` implicitly, prints database credentials, or resets an arbitrary database.
After creating a user-owned `.env` from `.env.example` as described in Local bootstrap,
provide the disposable URL and Compose password through that local configuration, then
opt into the destructive migration reset:

```bash
set -a
. ./.env
set +a
ALLOW_DESTRUCTIVE_TEST_DB=1 ./scripts/verify_release.sh
```

`TEST_DATABASE_URL` must be distinct from the runtime target, use PostgreSQL on
`localhost`, `127.0.0.1`, or `::1`, contain no query parameters, and name
`control_tower_m04` or a database ending in `_test`. Its password must match
`POSTGRES_PASSWORD`; because this is a URL value, reserved password characters must
be percent-encoded in this one variable. The script overrides the isolated Compose database name from
the test URL, waits for `pg_isready`, migrates to the current `head`, compares two
bundles byte-for-byte, ingests twice, detects twice, runs API health/KPI,
queue/detail/filter/lifecycle/history/export checks, and runs the PostgreSQL
integration suite. Cleanup is trapped even on failure.

Expected successful output is one concise pass line covering deterministic bundle
generation, ingestion idempotency, detection deduplication, API checks, live export,
and lifecycle history. The individual command logs stay in a temporary directory and
are removed during cleanup.

## Known limitations and future improvements

- The current source data is deterministic synthetic data, not a production
  connector framework; future work could add authenticated OMS/WMS/ERP/carrier
  adapters and incremental ingestion scheduling.
- Authentication, authorization/RBAC, multi-tenant isolation, and audit access
  controls are intentionally out of scope for the current public product.
- Detection is rule-based and synchronous; future releases could add scheduling,
  richer prioritization, notification routing, and explainable forecasting while
  keeping the deterministic rule contracts.
- The existing public deployment is intentionally read-only and anonymous; identity,
  authorization/RBAC, multi-tenant isolation, and production-grade observability are
  not provided by this repository.
- The release script assumes Docker, Docker Compose, `curl`, a repository `.venv`,
  and an available local API port (8000 by default; override with `API_PORT`).
- Docker base images remain tag-pinned rather than digest-pinned, and Python
  dependencies are not yet committed to a lockfile. Reproducible digest/lockfile
  hardening is an optional, non-blocking follow-up to this local release gate.

## Current deployment and operations

The current product boundary is an existing Cloud Run deployment for the FastAPI API
and Streamlit dashboard, backed by Neon PostgreSQL. The public demo sets
`PUBLIC_DEMO_READ_ONLY=true`; no lifecycle write is available there. The public
entry points are the [dashboard](https://control-tower-dashboard-xo5wjavqmq-ey.a.run.app)
and [API docs](https://control-tower-api-xo5wjavqmq-ey.a.run.app/docs). Deployment
configuration, IAM, secrets, and Neon connection details remain external to this
repository and are not asserted here.

The local equivalent keeps production operations explicit and reversible. The API and
Streamlit dashboard have independent non-root images, bind only to configured ports,
honor the provider-style `PORT` variable, and start without reload or debug mode.
Application images contain no generated data or runtime filesystem state. `migrate`
and `bootstrap` are separate one-shot Compose jobs; neither schema migration nor demo
generation runs during web application startup.

```bash
export POSTGRES_PASSWORD=synthetic-local-password
docker compose build api dashboard
./scripts/bootstrap_demo.sh
docker compose up -d api dashboard
```

Compose exposes the API at `http://127.0.0.1:${API_PORT:-8000}` and Streamlit at
`http://127.0.0.1:${DASHBOARD_PORT:-8501}`. The PostgreSQL volume is the only
persistent local service state. Re-running the bootstrap job regenerates the same
bundle and uses existing ingestion/detection idempotency contracts.

For a public demo, set `PUBLIC_DEMO_READ_ONLY=true` in the service environment.
FastAPI rejects lifecycle PATCH requests with 403 before invoking the lifecycle
service; the dashboard also omits its lifecycle form. Local development defaults
to `false`, so existing controlled lifecycle behavior remains available. Anonymous
read access is intentionally limited to the existing dashboard/KPI/queue/detail
and operational GET routes; authentication, SSO, and enterprise RBAC are not
provided by the current product.

The liveness endpoint (`/api/v1/livez`) is process-only. Readiness (`/api/v1/readyz`)
and the compatibility health endpoint (`/api/v1/health`) execute a lightweight
database query and return 503 when PostgreSQL is unavailable. Every request gets a
safe `X-Request-ID` response header and a structured JSON log containing only method,
path, status, duration, and correlation ID—never query strings, bodies, or secrets.

Run the complete local gate with synthetic disposable values:

```bash
export POSTGRES_PASSWORD=synthetic-release-password
export TEST_DATABASE_URL=postgresql+psycopg://control_tower:synthetic-release-password@127.0.0.1:5432/control_tower_test
export ALLOW_DESTRUCTIVE_TEST_DB=1
./scripts/verify_m07.sh
```

The gate builds both images, migrates, bootstraps twice, checks API liveness/readiness,
KPI/queue/detail, public mutation rejection and unchanged state/history, development
lifecycle behavior, restart persistence, dashboard HTTP startup, and CSV export.
`./scripts/security_check.sh` separately checks tracked secret-bearing filenames,
dependency audit availability, and built-image hardening. `./scripts/backup_restore_drill.sh`
separately performs a disposable `pg_dump` / `pg_restore` schema-and-data check. These
M07 commands never source `.env` implicitly and clean up only their unique Compose
project and volume.

Rollback is local and non-destructive: stop the API/dashboard, keep the PostgreSQL
volume, check out the reviewed prior image or source revision, run only migrations
approved for that revision (downgrade only when the migration is explicitly
reversible), then restart the independent services. Do not run destructive bootstrap
as an application health hook. See [docs/operations.md](docs/operations.md) for the
runbook and the current Cloud Run/Neon boundary.

## Verification shortcuts

Focused unit tests:

```bash
.venv/bin/python -m pytest tests/unit/test_exception_cli.py tests/unit/test_dashboard_ui.py -q
.venv/bin/ruff check .
```

The complete suite without a PostgreSQL URL skips PostgreSQL integration tests by
design. For authoritative integration evidence, use the canonical release command above
with an isolated `TEST_DATABASE_URL` and `ALLOW_DESTRUCTIVE_TEST_DB=1`.

## License

[MIT License](LICENSE)
