# Release review and portfolio capture checklist

This document is the public, software-only release record for the Logistic/Supply
Chain Operations Control Tower. It maps product requirements to the implementation,
tests, and executable verification paths. Source data and all example credentials are
synthetic/local-only.

## Product and release requirements

Status vocabulary:

- **Implemented and locally verified** — the repository contains the behavior and a
  repeatable unit/static check has exercised it.
- **Implemented; disposable-environment gate** — the behavior and tests exist, but
  authoritative evidence requires the isolated PostgreSQL release command.
- **Implemented; live-process gate** — the behavior has rendering/AppTest coverage and
  the release command also probes the real HTTP process.
- **Limit / not provided** — an intentional boundary of this local portfolio MVP.

| Requirement | Concrete implementation evidence | Test / command evidence | Status and explicit limits |
| --- | --- | --- | --- |
| Four synthetic operational sources: OMS, WMS, ERP/procurement, and carrier | `src/control_tower/synthetic/generator.py` writes the `oms/`, `wms/`, `erp/`, and `carrier/` artifact trees; `src/control_tower/synthetic/artifacts.py` defines the manifest contract. | `tests/unit/test_synthetic_generator.py::test_generation_manifest_has_target_volumes_and_six_source_scenarios`; `python -m control_tower.synthetic generate --output-dir <dir> --seed 20250301 --as-of 2025-01-15T12:00:00Z` | **Implemented and locally verified.** These are deterministic fixtures, not production connectors or live system integrations. |
| Deterministic generation | `generator.generate()` uses an explicit seed and timezone-aware `as_of`; artifacts are sorted and the manifest has a content identity. | `tests/unit/test_synthetic_generator.py`; `scripts/verify_release.sh` generates two bundles and compares every manifest/CSV byte-for-byte. | **Implemented and locally verified.** Reproducibility is guaranteed for the same generator version, seed, and instant; it is not a historical data replay. |
| Validated, normalized, atomic ingestion | `src/control_tower/ingestion/readers.py`, `normalization.py`, `validation.py`, and `loader.py` validate schema, types, bounds, relationships, duplicate conflicts, timestamps, and snapshot freshness before one PostgreSQL transaction. | `tests/unit/test_ingestion_validation.py`, `test_ingestion_readers.py`, `test_ingestion_loader.py`; `python -m control_tower.ingestion ingest --input-dir <bundle>` | **Implemented and locally verified.** PostgreSQL is the supported runtime backend; ingestion is a CLI boundary, not an HTTP mutation endpoint. |
| Idempotent source ingestion | `src/control_tower/ingestion/loader.py` preflights existing source identifiers and distinguishes identical rows from conflicts; inventory uses natural-key freshness. | `tests/unit/test_ingestion_idempotency.py`; `tests/integration/test_ingestion_postgres.py::test_generated_bundle_ingests_idempotently_in_postgres`; the release gate ingests the same bundle twice. | **Implemented; disposable-environment gate.** Requires a fresh isolated PostgreSQL target for live persistence evidence. |
| PostgreSQL persistence and Alembic ownership | `src/control_tower/models.py` defines the relational model; `migrations/versions/` owns schema changes; `alembic.ini` points at the migration tree and uses the platform path separator. | `tests/unit/test_db.py`, `tests/unit/test_models.py`, `tests/integration/test_postgres.py`; `.venv/bin/python -m alembic upgrade head`; release gate migrates its fresh Compose database. | **Implemented; disposable-environment gate.** The release gate requires Docker/Compose and a local disposable URL. |
| Six exception types | `src/control_tower/enums.py::ExceptionType`, `src/control_tower/exceptions/rules.py::detect_all`, and `dashboard/ui.py::EXCEPTION_TYPES` define `SLA_BREACH_RISK`, `INVENTORY_SHORTAGE`, `STOCKOUT_RISK`, `INVENTORY_MISMATCH`, `SUPPLIER_DELAY`, and `SHIPMENT_DELAY`. | `tests/unit/test_exception_rules.py`; `tests/unit/test_synthetic_generator.py`; `tests/integration/test_exception_postgres.py`; `python -m control_tower.exceptions --as-of 2025-01-15T12:00:00Z` | **Implemented; disposable-environment gate.** Rules are deterministic and synchronous; no forecasting or ML exception class is included. |
| Severity and explainability | `src/control_tower/exceptions/severity.py` derives severity; `contracts.py` and `rules.py` carry business impact, root cause, recommended action, confidence, and risk metrics. | `tests/unit/test_exception_severity.py`, `test_exception_contracts.py`, and `test_exception_rules.py`. | **Implemented and locally verified.** Thresholds are configured rule values, not learned prioritization. |
| Business impact, revenue, orders, root cause, action, and confidence | `src/control_tower/exceptions/contracts.py::ExceptionDetection`; `src/control_tower/models.py::ExceptionRecord`; `src/control_tower/api/schemas.py::ExceptionOut`; `dashboard/ui.py::render_exception_detail`. | `tests/unit/test_exception_contracts.py`, `tests/unit/test_api.py`, `tests/unit/test_dashboard_ui.py`; API detail smoke in `scripts/verify_release.sh`. | **Implemented and locally verified.** Revenue-at-risk is a finding-level sum, so one order appearing in multiple findings can contribute more than once. |
| Active-finding deduplication and rerun refresh | `src/control_tower/exceptions/service.py::ExceptionService.detect` resolves active findings by domain identity, refreshes derived facts, and suppresses historical fingerprints only when appropriate. | `tests/unit/test_exception_service_lifecycle.py::test_active_rerun_updates_derived_fields_without_history_or_status_change`; `tests/integration/test_exception_postgres.py`; release gate detects twice and checks `created`/`updated`. | **Implemented; disposable-environment gate.** Deduplication is scoped to the defined issue identity and active lifecycle. |
| Lifecycle and immutable history | `src/control_tower/exceptions/lifecycle.py` defines legal transitions; `service.py` writes `exception_history`; migration `20250827_02_exception_history_immutable.py` protects the database boundary. | `tests/unit/test_exception_service_lifecycle.py` covers initial history, legal paths, terminal states, ORM immutability, and migration declarations; release gate runs `OPEN -> ACKNOWLEDGED -> IN_PROGRESS -> RESOLVED` and checks history. | **Implemented; disposable-environment gate.** Terminal states cannot transition; authentication/RBAC around the operator identity is not provided. |
| Eight dashboard KPIs | `src/control_tower/kpis/service.py` and `src/control_tower/api/schemas.py` expose `orders_processed`, `sla_performance_pct`, `open_exceptions`, `critical_exceptions`, `revenue_at_risk`, `stockout_risks`, `supplier_delays`, and `shipment_delays`; `dashboard/ui.py::KPI_DEFINITIONS` renders all eight. | `tests/unit/test_api.py::test_kpi_summary_is_deterministic_and_revenue_is_finding_level`; dashboard KPI rendering tests; release gate checks all eight response keys. | **Implemented and locally verified.** KPI results are persisted-row, point-in-time aggregates; detection is not run by the KPI read path. |
| Versioned API and operational collections | `src/control_tower/api/routes.py`, `queries.py`, and `schemas.py` provide `/api/v1/health`, orders, inventory, purchase orders, shipments, exceptions, status update, and KPI summary routes with pagination, filters, stable ordering, and safe error mapping. | `tests/unit/test_api.py` covers collection contracts, filters, bounds, validation, lifecycle conflicts, and KPI `as_of`; `tests/integration/test_api_postgres.py`; release gate probes health, KPI, queue, detail, filters, lifecycle/history, and export. | **Implemented; disposable-environment gate.** The API has no authentication/RBAC and no ingestion/detection control endpoint. |
| Streamlit dashboard through the API boundary | `src/control_tower/dashboard/client.py` is the HTTP client; `src/control_tower/dashboard/ui.py` renders without database imports; `src/control_tower/dashboard/app.py` is the executable entry point. | `tests/unit/test_dashboard_client.py`; `tests/unit/test_dashboard_ui.py` uses `streamlit.testing.v1.AppTest`; `scripts/verify_release.sh` starts Streamlit and probes `/_stcore/health` plus `/`. | **Implemented; live-process gate.** It is a local MVP; deployment identity, scaling, observability, and production hardening remain outside this release. |
| Exception queue, filters, detail, lifecycle update, and export | `dashboard/ui.py::render_dashboard`, `build_exception_filters`, `render_exception_detail`, and `exceptions_to_csv` provide type/severity/status/warehouse/entity filters, supplier purchase-order context, queue fields, detail fields/history, controlled status form, and all-page CSV export. | `tests/unit/test_dashboard_ui.py` covers KPI/queue rendering, supplier-filter separation, CSV rows, lifecycle validation, and cache invalidation; client pagination/export tests are in `tests/unit/test_dashboard_client.py`. | **Implemented and locally verified.** CSV export is CSV-only; Excel export, direct database access, and ingestion/detection controls are intentionally unsupported. |
| End-to-end release evidence | `scripts/verify_release.sh` creates a unique Compose project/volume, validates the URL fail-closed, traps cleanup, migrates, generates, ingests, detects, starts API and Streamlit, probes public contracts, then runs integration tests. | `ALLOW_DESTRUCTIVE_TEST_DB=1 ./scripts/verify_release.sh` with exported synthetic/local values; command logs are temporary and not printed. | **Implemented; disposable-environment gate.** Requires Docker, Compose, `curl`, the repository `.venv`, a free API/dashboard port, and a synthetic password. |

## Portfolio screenshot and demo checklist

Do not add or claim screenshot files as part of the source tree. Capture only from a
running local instance after the data and API checks below succeed.

### Prerequisites and safe data

- [ ] Use synthetic values only. Do not use production exports, real customer data,
  real credentials, or a shared database.
- [ ] Complete the local install in the README and create a disposable
  `TEST_DATABASE_URL` ending in `_test` (or named `control_tower_m04`) on a loopback
  PostgreSQL instance.
- [ ] Export synthetic `POSTGRES_PASSWORD`, `TEST_DATABASE_URL`, and
  `ALLOW_DESTRUCTIVE_TEST_DB=1` explicitly in the current shell. Do not source an
  unknown configuration file.
- [ ] Run the canonical gate first; it proves the migration, fixture, ingestion,
  detection, API, lifecycle, and live dashboard process boundaries:

```bash
export POSTGRES_USER=control_tower
export POSTGRES_PASSWORD=synthetic-release-password
export TEST_DATABASE_URL=postgresql+psycopg://control_tower:synthetic-release-password@127.0.0.1:5432/control_tower_test
export ALLOW_DESTRUCTIVE_TEST_DB=1
./scripts/verify_release.sh
```

The `.env.example` values are placeholders. Use synthetic local values in the current
shell and never paste command output containing a URL or password into a portfolio
artifact.

### Start a capture session

If a persistent view is needed after the gate has cleaned up, start a separate
**non-destructive** local stack and repeat the documented flow against a disposable
runtime database:

```bash
docker compose up -d postgres
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m control_tower.synthetic generate \
  --output-dir /tmp/control-tower-demo-bundle \
  --seed 20250301 --as-of 2025-01-15T12:00:00Z
.venv/bin/python -m control_tower.ingestion ingest \
  --input-dir /tmp/control-tower-demo-bundle
.venv/bin/python -m control_tower.exceptions \
  --as-of 2025-01-15T12:00:00Z
```

In another terminal, start the API and dashboard with loopback bindings and a free
port:

```bash
.venv/bin/python -m uvicorn control_tower.api.app:app \
  --host 127.0.0.1 --port 8000
API_BASE_URL=http://127.0.0.1:8000/api/v1 \
  .venv/bin/python -m streamlit run src/control_tower/dashboard/app.py \
  --server.address 127.0.0.1 --server.port 8501
```

### Expected views and fields

- [ ] **Overview:** title `Operations Control Tower`; eight visible KPI cards:
  orders processed, SLA performance, open exceptions, critical exceptions, revenue
  at risk, stockout risks, supplier delays, and shipment delays.
- [ ] **Exception Queue:** at least one row with exception type, severity, status,
  entity type/ID, business impact, revenue at risk, orders affected, detected time,
  and recommended action.
- [ ] **Queue controls:** exception type, severity, lifecycle status, warehouse ID,
  entity type/ID, page size/page number, and filtered CSV download are visible.
- [ ] **Exception detail:** business impact, operational status, revenue at risk,
  affected orders, root cause, recommended action, confidence, detected timestamp,
  and lifecycle history are visible.
- [ ] **Lifecycle demonstration:** submit a legal status change with a synthetic
  actor and reason where required; verify the success message and refreshed status.
- [ ] **Supplier context (optional):** enter a synthetic supplier ID and show the
  purchase-order context table; do not describe supplier ID as a generic exception
  filter.
- [ ] **Explicit states (optional):** capture a safe empty/error state only by using
  a non-sensitive filter or stopping the API; never expose stack traces or response
  bodies.

### Capture and cleanup safety

- [ ] Capture only the dashboard viewport; exclude terminal windows, environment
  files, URLs containing credentials, browser storage, and unrelated personal data.
- [ ] Redact local host ports or identifiers if the portfolio format requires it;
  never redact by editing the application or adding fake data to the repository.
- [ ] Stop the Streamlit and API processes after capture, then remove only the
  disposable Compose project/volume that you started:

```bash
docker compose down --volumes
rm -rf /tmp/control-tower-demo-bundle
```

- [ ] Confirm `git status --short` contains no generated bundle, cache, coverage,
  temporary, or screenshot artifact. Keep screenshots outside the repository unless
  they are deliberately reviewed portfolio assets.

## Final-review evidence and limitations

This is a final review of the entire repository, not only the latest implementation
patch. The review surface includes source, migrations, tests, scripts, configuration,
README, architecture, ignore rules, and this release document. Before publication,
run the following checks and retain their real output in the review record:

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/python -m compileall -q src tests
bash -n scripts/verify_release.sh
./scripts/verify_release.sh
```

For a fresh-clone/non-editable-install verification, install from a fresh checkout into a
fresh virtual environment with `pip install ".[dev]"` (not `-e`) and run the focused
unit suite plus Ruff from that environment. This catches packaging and source-path
assumptions that an editable developer install can hide. The live release command
must still be run from the reviewed checkout with a disposable PostgreSQL target.

Security and Git hygiene checks are part of the release boundary: inspect
`git status --short`, `git diff --check`, tracked-file names, ignore rules, and added
lines for credentials/private keys, unsafe database defaults, generated runtime state,
and accidental `.env`/cache/coverage artifacts. Do not stage, commit, or push as part
of this review.

Known skipped or unsupported gates must remain visible:

- Without `TEST_DATABASE_URL`, PostgreSQL integration tests skip by design; this is
  not PostgreSQL evidence.
- The disposable PostgreSQL and live API/Streamlit process checks require Docker,
  Compose, `curl`, a working `.venv`, and free loopback ports.
- There is no production connector, authentication/RBAC, multi-tenant isolation,
  scheduler/queue, notification integration, forecasting, Excel export, cloud
  deployment, or production observability in this local MVP.
- A passing AppTest proves the rendering boundary with a fake client; only the
  release command's live HTTP probes prove the real Streamlit entry point starts and
  serves its health/root endpoints.
