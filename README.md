# Supply Chain Operations Control Tower

## Local bootstrap

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
cp .env.example .env
# Replace the local password placeholder in both POSTGRES_PASSWORD and DATABASE_URL.
docker compose up -d postgres
.venv/bin/python -m pytest tests/unit/test_config.py -q
.venv/bin/ruff check .
```

`.env` is untracked local configuration. Before starting PostgreSQL, copy
`.env.example` to `.env` and replace `replace-with-a-local-password` with a
local-only password in both `POSTGRES_PASSWORD` and `DATABASE_URL`. If the
password contains URL-reserved characters, URL-encode it in `DATABASE_URL`.
Alternatively, pass `POSTGRES_PASSWORD` explicitly when invoking Compose, for
example `POSTGRES_PASSWORD=local-test-password docker compose up -d postgres`,
and provide a matching `DATABASE_URL` to the native Python application. The
Python application runs natively; Docker Compose provides PostgreSQL only.

## Database migration and health gate

With the local PostgreSQL service running, apply the M01 schema migration:

```bash
.venv/bin/python3 -m alembic upgrade head
```

The PostgreSQL migration/health gate uses an explicit `TEST_DATABASE_URL` and
checks both migration success and database health. After replacing the local
password placeholder in `.env`, load that configuration and run the gate
against an isolated, disposable database:

```bash
set -a
. ./.env
set +a
TEST_DATABASE_URL="$DATABASE_URL" \
  .venv/bin/python3 -m pytest tests/integration/test_postgres.py -q
```

The other PostgreSQL integration tests reset the migration-managed schema before
running. Because that reset is destructive, run the complete integration gate
only against an isolated disposable database and opt in explicitly:

```bash
ALLOW_DESTRUCTIVE_TEST_DB=1 TEST_DATABASE_URL="$DATABASE_URL" \
  .venv/bin/python3 -m pytest tests/integration -q
```

Those tests fail clearly when `TEST_DATABASE_URL` is PostgreSQL but the opt-in is
missing, remote, or not a disposable test target. The reset guard only accepts
`localhost`, `127.0.0.1`, or `::1`, with database `control_tower_m04` or a name
ending in `_test`; it also rejects any URL query parameters because they can
override the effective connection target. Never point it at a shared or
production database.

## M02 synthetic data and ingestion

Generate a reproducible multi-source fixture (the default output is ignored by
Git):

```bash
.venv/bin/python -m control_tower.synthetic generate \
  --output-dir data/generated --seed 20250301 \
  --as-of 2025-01-15T12:00:00+00:00
```

The bundle contains stable, sorted UTF-8 CSVs for `oms/`, `wms/`, `erp/`, and
`carrier/`, plus `manifest.json`. Source identifiers join products, orders and
items, warehouses and inventory, suppliers and purchase orders, and shipments
back to orders. The manifest records row counts, seed, UTC `as_of`, a stable
identity, and six ordinary source-data scenario fixtures: `SLA_BREACH_RISK`,
`INVENTORY_SHORTAGE`, `STOCKOUT_RISK`, `INVENTORY_MISMATCH`, `SUPPLIER_DELAY`,
and `SHIPMENT_DELAY`. It does not create exception rows or run detection.

After applying the M01 migration, ingest atomically into PostgreSQL:

```bash
.venv/bin/python -m control_tower.ingestion ingest --input-dir data/generated
```

Ingestion trims strings, canonicalizes enums, parses timezone-aware UTC
instants and `Decimal` quantities/money, and rejects invalid types, bounds,
relationships, duplicate conflicts, stale inventory snapshots, and equal-time
inventory conflicts. Every artifact is prevalidated before the outer
transaction writes anything. Existing source IDs are idempotent: identical
rows are skipped, conflicting rows reject the bundle; a newer inventory
snapshot updates its product/warehouse natural key while an older snapshot is
rejected. The command prints per-source read/accepted/rejected/inserted/
updated/skipped/conflicted/final counts and rejection details.

## M03 exception intelligence

M03 evaluates the ingested operational state at an explicit, timezone-aware
UTC `as_of` instant. The deterministic engine implements exactly six exception
types: `SLA_BREACH_RISK`, `INVENTORY_SHORTAGE`, `STOCKOUT_RISK`,
`INVENTORY_MISMATCH`, `SUPPLIER_DELAY`, and `SHIPMENT_DELAY`.

Detection produces explainable business impact, risk metrics, root cause,
recommended action, and confidence. The persistence service deduplicates
active exceptions, preserves lifecycle state, and appends one immutable
`exception_history` row for each initial detection or valid transition. The
supported lifecycle is `OPEN` -> `ACKNOWLEDGED` -> `IN_PROGRESS` ->
`RESOLVED`, with `DISMISSED` as an alternate terminal state.

M03 is a PostgreSQL-first domain service only. Authentication, dashboards,
exports, forecasting, ML/LLM, and external integrations remain outside the
milestones described here; the M04 API and KPI read surface is documented
below.

## M04 Operations API

The versioned FastAPI surface is available under `/api/v1`:

- `GET /health`
- `GET /orders` and `GET /orders/{source_order_id}`
- `GET /inventory`
- `GET /purchase-orders`
- `GET /shipments`
- `GET /exceptions` and `GET /exceptions/{exception_id}`
- `PATCH /exceptions/{exception_id}/status`
- `GET /kpis/summary`

Start the native API process after applying migrations (PostgreSQL is still
provided by Compose):

```bash
.venv/bin/python -m uvicorn control_tower.api.app:app --reload
```

Collection endpoints use `page` (minimum 1) and `page_size` (1--100,
default 25), return stable ordering, and expose `items`, `page`, `page_size`,
and `total`. Repeated `status` query parameters are combined with OR
semantics. Resource filters use exact matching and timestamp bounds are
inclusive; timestamp filters must be timezone-aware RFC3339 values. Responses
use source identifiers for operational resources and numeric IDs for
exceptions. UTC timestamps are RFC3339 strings, quantities are Decimal strings
with three fractional digits, and money uses two fractional digits.

Inventory supports SKU, available-quantity, and inclusive observed-time
filters. Purchase orders support supplier, warehouse, ordered/expected date,
and item-derived remaining-quantity filters. Shipment warehouse filtering is
resolved through the related order. Exception collections support entity,
product, warehouse, type, severity, status, and inclusive detected-time
filters. Order details include eager-loaded item and shipment summaries.

The exception status patch accepts `status`, a nonblank `actor`, and a required
`reason` for terminal states. It delegates lifecycle validation and immutable
history creation to the M03 `transition_exception` service. Validation failures
return 422, missing resources 404, invalid lifecycle transitions 409, and
unavailable PostgreSQL 503 without SQL or credential details.

KPI aggregation reads persisted rows only and never runs detection. Its optional
timezone-aware `as_of` defaults to `Settings.as_of`, is normalized to UTC, and
is echoed in the response. It returns `orders_processed`, `open_orders`,
`fulfilled_orders`, `cancelled_orders`, nullable `sla_performance_pct`,
`open_exceptions`, `critical_exceptions`, `revenue_at_risk`, `stockout_risks`,
`supplier_delays`, and `shipment_delays`, with order/exception rows bounded by
that instant. Revenue at risk is a finding-level sum across active exception
findings; the same order may therefore contribute to more than one finding and
the value is not a distinct-order financial total. Authentication/RBAC, operational mutations,
ingestion/detection endpoints, dashboard, and export remain outside M04.
