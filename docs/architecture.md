# M01-M06 architecture

## System purpose

The Supply Chain Operations Control Tower consolidates deterministic synthetic
OMS, WMS, ERP/procurement, and carrier data, identifies six explainable operational
exceptions, exposes a stable API, and presents an operator dashboard. PostgreSQL is
the runtime system of record; generated source artifacts and migrations are explicit
inputs to the flow.

## End-to-end flow

```text
seed + timezone-aware as_of
        |
        v
Synthetic generator --> stable CSV bundle + manifest
        |
        v
Validation/normalization --> atomic PostgreSQL ingestion
        |
        v
M03 rules at explicit as_of --> exception records + immutable history
        |
        +--> FastAPI /api/v1 --> API clients and Streamlit dashboard
        |
        +--> KPI read service (query-only, point-in-time bounded)

PATCH exception status --> M03 lifecycle service --> exception_history
```

The release path is intentionally operational rather than hidden inside application
startup: migrate to the current Alembic `head`, generate, ingest, detect, then use
the API and dashboard. There is no HTTP ingestion or detection endpoint.

## Runtime topology

```text
Docker Compose project
└── PostgreSQL 16 (loopback port, disposable release volume)

Native Python processes
├── Alembic migration CLI
├── synthetic generator CLI
├── ingestion CLI
├── exception detection CLI
├── Uvicorn/FastAPI process
└── Streamlit process (HTTP-only client of FastAPI)
```

`DATABASE_URL` identifies the runtime database. `TEST_DATABASE_URL` is a separate,
explicitly disposable integration/release target. The release script creates an
isolated Compose project and volume, validates the parsed test URL, waits for
PostgreSQL health, and removes only that project on exit.

## Architectural boundaries

### M01 relational foundation

SQLAlchemy models define products, warehouses, suppliers, orders and items,
inventory and movements, purchase orders and items, shipments, exceptions, and
append-only exception history. Alembic owns schema evolution. Application startup
creates an engine but does not create tables.

### M02 synthetic and ingestion boundary

`control_tower.synthetic.generator` uses one local `random.Random(seed)` and an
explicit UTC `as_of`; `artifacts.py` writes stable sorted UTF-8 CSVs and a
content-derived manifest identity. `anomalies.py` labels six ordinary source-data
scenarios; it does not write exception rows.

`ingestion.readers` reads the bundle, `normalization.py` canonicalizes values, and
`validation.py` checks schema, bounds, joins, duplicate IDs, time ordering, manifest
counts, and inventory snapshot freshness without database writes. `loader.py` then
performs conflict preflight and one dependency-ordered PostgreSQL transaction.
Identical source IDs are skipped; conflicting source IDs, stale snapshots, and
equal-time inventory conflicts are rejected.

### M03 exception intelligence boundary

The six deterministic rules evaluate persisted operational state at an explicit,
timezone-aware instant:

- `SLA_BREACH_RISK`
- `INVENTORY_SHORTAGE`
- `STOCKOUT_RISK`
- `INVENTORY_MISMATCH`
- `SUPPLIER_DELAY`
- `SHIPMENT_DELAY`

Immutable detection contracts carry issue identity, source fingerprint, business
impact, risk metrics, root cause, recommended action, confidence, and relationships.
`ExceptionService` resolves active findings by domain identity, refreshes derived
facts on rerun, and uses fingerprint suppression for historical resolved/dismissed
findings. New findings start `OPEN` and receive one initial history row.

The lifecycle service is the sole status-transition authority:

```text
OPEN -> ACKNOWLEDGED -> IN_PROGRESS -> RESOLVED
  |          |              |
  +----------+--------------+--> DISMISSED
```

Terminal states cannot transition. Actor is required for every transition and a
nonblank reason is required for `RESOLVED` and `DISMISSED`. History is append-only
through ORM and PostgreSQL protections, including bulk statement boundaries and
truncate protection.

The detection CLI (`python -m control_tower.exceptions`) requires `--as-of`, accepts
an optional `--database-url`, normalizes the instant to UTC, commits one detection
run, and prints only JSON counters.

### M04 API and KPI boundary

FastAPI dependencies create one SQLAlchemy session per request. Query functions own
stable ordering, exact filters, inclusive time bounds, pagination, eager loading,
and point-in-time predicates. Routes map those results to `/api/v1` contracts.
Operational resources use source identifiers; exception resources use numeric IDs.
The only write route is `PATCH /exceptions/{id}/status`, which delegates to M03 and
maps validation to 422, missing records to 404, illegal transitions to 409, and
PostgreSQL failures to safe 503 responses.

`GET /kpis/summary` is query-only. It accepts an optional timezone-aware `as_of`,
defaults from settings, echoes normalized UTC, and returns bounded order and active
exception aggregates. The dashboard KPI fields are orders processed, SLA
performance, open exceptions, critical exceptions, revenue at risk, stockout risks,
supplier delays, and shipment delays. Revenue at risk is a finding-level sum, not a
distinct-order financial total.

### M05 dashboard boundary

`DashboardClient` is a bounded-timeout HTTPX client with safe error mapping and
pagination-aware export. `dashboard.ui` is a Streamlit presentation adapter that
receives an injectable client for AppTest coverage. It renders the eight KPIs,
filtered/paginated Exception Queue, all-page CSV export, supplier purchase-order
context, exception detail/history, and explicit loading/error/empty states.

The dashboard does not import SQLAlchemy models or open a database connection. Its
lifecycle form sends actor, target status, and reason to the API; successful writes
increment a session data version, clear cached reads, and rerender.

### M06 release boundary

`TEST_DATABASE_URL` and `ALLOW_DESTRUCTIVE_TEST_DB=1` are required for destructive
integration reset. The target must be PostgreSQL, query-free, loopback-only, and
named `control_tower_m04` or ending in `_test`; the Compose password must match the
URL password. `scripts/verify_release.sh` exercises migration, deterministic
artifact comparison, ingest/re-ingest, detection/deduplication, API health/KPI/
queue/detail/lifecycle smoke, and the PostgreSQL integration suite. Temporary logs
are not retained and credentials are never printed.

## Out of scope

The current architecture deliberately excludes:

- authentication, authorization/RBAC, tenant isolation, and production secrets;
- production OMS/WMS/ERP/carrier connectors, scheduling, queues, and webhooks;
- ingestion or detection HTTP mutation endpoints;
- forecasting, ML/LLM enrichment, notification routing, and autonomous remediation;
- production observability, HA/scaling, cloud deployment, and disaster recovery;
- dashboard direct database access, Excel export, and multi-user collaboration.

These are future product or platform boundaries, not implicit behavior of the M01-M06
release.
