# Portfolio copy

Reusable copy for presenting the Supply Chain Operations Control Tower as an
operations and supply-chain engineering project. The wording describes the current
repository and public demo accurately: a production-grade portfolio demo, not an
enterprise platform claim.

## CV bullets

- Built a production-grade portfolio demo of a Supply Chain Operations Control Tower
  that consolidates synthetic OMS, WMS, ERP/procurement, and carrier data into a
  prioritized, explainable exception queue with severity, business impact, root
  cause, recommended action, and lifecycle history.
- Delivered a versioned FastAPI read/API boundary and Streamlit operations dashboard,
  with deterministic generation, PostgreSQL/Alembic persistence, idempotent ingestion,
  rule-based exception detection, and a public read-only Cloud Run deployment.

## LinkedIn paragraph

I built a Supply Chain Operations Control Tower as a production-grade portfolio demo
for turning fragmented operational signals into decisions. The system generates
deterministic OMS, WMS, ERP/procurement, and carrier data, persists it in PostgreSQL,
detects six explainable exception types, and presents prioritized findings through a
versioned FastAPI API and Streamlit dashboard. The public Cloud Run demo is
read-only by design, while local development preserves the controlled lifecycle
workflow. The project focuses on practical operations engineering: reliable data
contracts, idempotent processing, explainability, and safe deployment boundaries.

## GitHub one-sentence description

Read-only Supply Chain Operations Control Tower demo with deterministic exception intelligence, FastAPI, Streamlit, PostgreSQL, and Cloud Run deployment.

## 60-second interview explanation

“I built this to model the part of an operations team’s day that is usually hidden
inside spreadsheets and disconnected systems: deciding which supply-chain issue
deserves attention first and why. I generate realistic but synthetic OMS, WMS,
procurement, and carrier inputs, validate and ingest them idempotently into
PostgreSQL, then run six deterministic exception rules. Every finding includes
severity, financial and order impact, root cause, recommended action, and immutable
lifecycle history. FastAPI is the contract boundary and Streamlit is the operator
view; the public Cloud Run deployment is deliberately read-only, with lifecycle
changes available only in local development. The result is a small but complete
operations product that is reproducible, testable, and honest about its limits.”
