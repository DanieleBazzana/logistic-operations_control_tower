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
