# Supply Chain Operations Control Tower

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
The complete release flow is reproducible locally and is backed by disposable
PostgreSQL verification.

## Stack

- Python 3.11+
- PostgreSQL 16 via Docker Compose (application processes run natively)
- SQLAlchemy 2, Alembic, and psycopg
- FastAPI and Uvicorn
- Streamlit, Pandas, and HTTPX
- Pytest and Ruff

For the product requirements, release evidence, and safe portfolio screenshot/demo
checklist, see [docs/release-review.md](docs/release-review.md).

## Local bootstrap

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
cp .env.example .env
```

`.env` is local and untracked. Replace the placeholder in
`POSTGRES_PASSWORD` and `DATABASE_URL` with a local-only value; the two passwords
must match, and URL-reserved characters must be encoded in `DATABASE_URL`.
`DATABASE_URL` points to the runtime database `control_tower`.

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
.venv/bin/python -m uvicorn control_tower.api.app:app --reload
```

Available versioned routes:

- `GET /api/v1/health`
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
mutation. This is a local MVP with no authentication/RBAC, direct database access,
forecasting, external integrations, ingestion controls, detection controls, or
Excel export.

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

`TEST_DATABASE_URL` must be distinct from `DATABASE_URL`, use PostgreSQL on
`localhost`, `127.0.0.1`, or `::1`, contain no query parameters, and name
`control_tower_m04` or a database ending in `_test`. Its password must match
`POSTGRES_PASSWORD`. The script overrides the isolated Compose database name from
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
  controls are intentionally out of scope for this local portfolio release.
- Detection is rule-based and synchronous; future releases could add scheduling,
  richer prioritization, notification routing, and explainable forecasting while
  keeping the deterministic rule contracts.
- The dashboard is a local Streamlit presentation layer; production deployment
  would need identity, observability, scaling, and a stronger UI/API delivery model.
- The release script assumes Docker, Docker Compose, `curl`, a repository `.venv`,
  and an available local API port (8000 by default; override with `API_PORT`).

## Verification shortcuts

Focused unit tests:

```bash
.venv/bin/python -m pytest tests/unit/test_exception_cli.py tests/unit/test_dashboard_ui.py -q
.venv/bin/ruff check .
```

The complete suite without a PostgreSQL URL skips PostgreSQL integration tests by
design. For authoritative integration evidence, use the canonical release command above
with an isolated `TEST_DATABASE_URL` and `ALLOW_DESTRUCTIVE_TEST_DB=1`.
