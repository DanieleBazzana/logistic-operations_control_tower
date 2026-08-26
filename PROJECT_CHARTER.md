# PROJECT CHARTER — Supply Chain Operations Control Tower

## 1. Objective

Build a portfolio-quality Supply Chain Operations Control Tower simulating a realistic multi-system operations environment.

The system must consolidate operational data from OMS, WMS, ERP/Procurement and Carrier sources into PostgreSQL, detect actionable supply-chain exceptions through deterministic business rules, expose data through FastAPI and provide an interactive Streamlit dashboard.

Target relevance: Operations, Supply Chain, TechOps and Operations Analyst roles.

The final product must be functional, testable, reproducible and understandable by recruiters or technical reviewers. This is not a mockup.

## 2. Architecture

Target flow:

OMS / WMS / ERP / Carrier
-> ingestion and validation
-> PostgreSQL
-> Exception Engine
-> service layer
-> FastAPI
-> Streamlit dashboard

Prioritize realistic business logic, explainability, SQL, modularity, reproducibility, tests and clear documentation.

Avoid unnecessary complexity and overengineering.

## 3. Stack

Required:

- Python 3.11+
- PostgreSQL
- Docker Compose for PostgreSQL
- SQLAlchemy
- Alembic
- FastAPI
- Pydantic / pydantic-settings
- Pandas where useful
- Streamlit
- Pytest
- Ruff
- openpyxl only if useful for Excel export

The Python application may run natively in a virtual environment. Do not containerize the whole application unless clearly useful.

Dependencies must be declared in the repository, preferably through pyproject.toml.

Hermes must bootstrap all project infrastructure required by the application, including repository structure, dependency configuration, Docker Compose, PostgreSQL, database setup, migrations, testing and application entry points.

No secrets may be committed. Provide .env.example.

## 4. Simulated Source Systems

### OMS
Generate orders and order items with timestamps, promised SLA, fulfillment status, region, SKU/product, warehouse assignment, quantity and unit price.

### WMS
Generate inventory snapshots and movements with SKU, warehouse, on-hand, reserved, available quantity and timestamps.

### ERP / Procurement
Generate suppliers, purchase orders and PO items with ordered quantity, received quantity, PO date, expected delivery date and status.

### Carrier
Generate shipments with order reference, carrier, tracking ID, status, shipped timestamp, ETA and delivered timestamp where applicable.

Sources may use deterministic CSV and/or JSON files. No real external APIs are required.

## 5. Synthetic Dataset

Create deterministic seed generation.

Target approximately:

- 200 products
- 2–3 warehouses
- 10 suppliers
- at least 1,000 orders
- realistic inventory and movements
- purchase orders
- shipments

The dataset must intentionally include anomalies that trigger every required exception type while remaining coherent enough for meaningful relational joins and analysis.

## 6. Ingestion

Implement reusable ingestion pipelines that:

- read simulated sources
- validate required fields and types
- normalize data
- validate relationships where practical
- reject/report invalid records clearly
- handle duplicates safely
- support idempotent re-execution where practical
- load PostgreSQL
- return a useful ingestion summary

Do not silently accept corrupted operational data.

## 7. Database

At minimum implement:

- products
- warehouses
- inventory
- inventory_movements
- orders
- order_items
- suppliers
- purchase_orders
- purchase_order_items
- shipments
- exceptions
- exception_history

Use proper primary keys, foreign keys, indexes, unique constraints, timestamps, numeric types and status fields.

Use Alembic migrations.

PostgreSQL is the primary database. Do not replace it with SQLite for the MVP.

SQL must be a visible skill in the project. Important analysis should involve realistic joins, filters, aggregations, grouping and calculations using SQLAlchemy and/or explicit SQL.

## 8. Exception Engine

Implement exactly these six MVP exception types.

### SLA_BREACH_RISK
An open/unfulfilled order is overdue against its SLA or within a configurable risk window. Default risk window may be about four hours.

### INVENTORY_SHORTAGE
Current open demand for a SKU at its assigned warehouse exceeds available inventory.

### STOCKOUT_RISK
Projected inventory after relevant open demand falls below a configurable safety-stock threshold. This represents future risk and must remain distinct from INVENTORY_SHORTAGE.

### INVENTORY_MISMATCH
Inventory snapshot differs from inventory reconstructed from movement history beyond a configurable tolerance.

### SUPPLIER_DELAY
A purchase order is past expected delivery while required quantity remains incompletely received.

### SHIPMENT_DELAY
A shipment is past ETA and is not DELIVERED.

Repeated Exception Engine execution must not create duplicate active exceptions for the same underlying issue. Use a clear deduplication/identity strategy.

A resolved issue may be raised again only when a genuinely new operational event justifies it.

## 9. Exception Data and Severity

Each exception should expose where applicable:

- exception_id
- exception_type
- entity_type
- entity_id
- severity
- status
- detected_at
- expected_resolution
- business_impact
- revenue_at_risk
- orders_affected
- root_cause
- recommended_action
- confidence

Severity levels:

CRITICAL, HIGH, MEDIUM, LOW.

Severity must be deterministic, configurable and based on meaningful operational factors such as revenue at risk, orders affected, overdue duration, urgency or shortage quantity.

Root cause and recommended action must also be deterministic and explainable. No LLM is required.

## 10. Lifecycle

Support:

OPEN -> ACKNOWLEDGED -> IN_PROGRESS -> RESOLVED

Also support DISMISSED.

Validate allowed transitions.

Every status change must create immutable exception_history information with timestamps and relevant transition details.

## 11. API

Provide a versioned FastAPI API with at least:

- health
- orders
- order detail
- inventory
- purchase orders
- shipments
- exceptions
- exception detail
- exception status update
- KPI / operational summary

Support useful filtering and pagination where appropriate.

Exception filters should include type, severity, status, entity and warehouse where relevant.

Automatic API documentation must work.

## 12. Dashboard

Build a functional Streamlit operations dashboard.

Primary KPIs:

- orders processed
- SLA performance
- open exceptions
- critical exceptions
- revenue at risk
- stockout risks
- supplier delays
- shipment delays

Provide an Exception Queue showing useful columns such as type, severity, status, affected entity, business impact, revenue at risk, detected time and recommended action.

Provide filters and an exception detail workflow.

If reasonably straightforward, support lifecycle status updates from the dashboard.

Support useful CSV export. Excel export is optional if simple.

## 13. Configuration

Important thresholds must be configurable, including where relevant:

- DATABASE_URL
- SLA risk window
- safety-stock / stockout threshold
- inventory mismatch tolerance
- severity thresholds

Provide safe local defaults and .env.example.

Never commit credentials.

## 14. Testing

Testing is mandatory.

Include unit tests for all six exception rules with triggering and non-triggering cases.

Also test important behavior including:

- ingestion validation
- idempotent ingestion where implemented
- exception deduplication
- severity
- lifecycle transitions/history
- API behavior
- database/service integration
- representative end-to-end workflow

Tests should verify business behavior, not only implementation details.

## 15. Milestones

### M01 — Foundation and Data Model
Bootstrap repository, dependencies, Docker Compose PostgreSQL, configuration, SQLAlchemy models, migrations, test/lint setup and basic health verification.

### M02 — Synthetic Data and Ingestion
Implement deterministic OMS, WMS, ERP and Carrier source generation plus validation and ingestion.

### M03 — Exception Intelligence
Implement all six exception rules, severity, business impact, root cause, recommended action, deduplication and lifecycle/history.

### M04 — Operations API
Implement versioned FastAPI endpoints, filtering, lifecycle operations and KPI services.

### M05 — Operations Dashboard
Implement Streamlit dashboard, KPIs, Exception Queue, filters, detail workflow and useful export.

### M06 — Portfolio Release
Complete quality testing, documentation, architecture explanation, setup instructions and final project verification.

Each milestone must pass its quality gate before completion.

## 16. Documentation

README.md must be portfolio-quality and explain:

- business problem
- solution
- architecture
- simulated source systems
- six exception types
- stack
- database
- local setup
- Docker/PostgreSQL startup
- migrations
- synthetic data generation
- ingestion
- API startup
- dashboard startup
- testing
- representative workflow
- future improvements

Also provide a concise architecture document or equivalent section.

A recruiter must understand the project's business and technical value without reading all source code.

## 17. Definition of Done

PROJECT_COMPLETE requires:

- reproducible project setup
- declared dependencies
- PostgreSQL running through Docker Compose
- working migrations
- deterministic synthetic data
- all four source systems represented
- working ingestion
- valid relational model
- all six exception types implemented
- exception deduplication
- deterministic severity
- root cause and recommended action
- lifecycle and exception history
- KPI calculations
- working FastAPI application
- working Streamlit dashboard
- working Exception Queue
- useful export
- core business logic covered by tests
- milestone quality gates passed
- test suite and Ruff passing, or only explicitly justified non-blocking exceptions
- complete documentation
- no committed secrets
- no unresolved high-severity review findings
- core workflows actually executed and verified

Do not declare PROJECT_COMPLETE merely because expected files exist.

## 18. Out of Scope

Do not add unless needed for a concrete blocker:

- ML or LLM features
- predictive/demand forecasting
- real SAP/Amazon/carrier integrations
- paid APIs or SaaS
- enterprise authentication/RBAC
- Kubernetes
- microservices
- Kafka
- mobile apps
- complex frontend frameworks
- advanced cloud infrastructure

These may be documented as future extensions.

## 19. Autonomous Execution Policy

PROJECT_CHARTER.md is the authoritative product specification.

Hermes may autonomously make R0/R1/R2 decisions when they are consistent with this Charter, reversible, local to the project, non-destructive, free/open-source and require no external account or credential.

DEFAULT_ACTION=CONTINUE for R0/R1/R2.

Do not ask the user for routine implementation approval.

Escalate only genuine R3 situations such as:

- destructive or irreversible operations
- meaningful data-loss risk
- credentials/secrets required from the user
- paid services or purchases
- external account creation
- force push or destructive Git operations
- material change to this Charter
- important security decisions requiring human judgment
- blocker remaining after reasonable autonomous remediation

Continue task by task and milestone by milestone until every Definition of Done condition is satisfied and PROJECT_COMPLETE is reached.
