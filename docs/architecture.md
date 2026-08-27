# M02 architecture

M02 is a deterministic fixture-and-load boundary around the M01 relational
model.

1. **Synthetic boundary** — `control_tower.synthetic.generator` uses a local
   `random.Random(seed)` and explicit UTC `as_of` to create coherent OMS, WMS,
   ERP, and carrier rows. `artifacts.py` writes stable sorted UTF-8 CSVs and a
   content-derived manifest identity. `anomalies.py` only labels six ordinary
   source-data fixtures for future M03 detection; it never creates exceptions.
2. **Validation boundary** — `control_tower.ingestion.readers` reads and
   identity-checks a bundle. `normalization.py` canonicalizes strings, enums,
   UTC timestamps, booleans, and Decimal values. `validation.py` validates
   schema, bounds, time ordering, joins, duplicate source IDs, and inventory
   snapshot natural keys without touching the database.
3. **Persistence boundary** — `loader.py` requires a PostgreSQL SQLAlchemy
   engine, performs all validation and existing-row conflict checks before
   writes, then uses one outer transaction in dependency order. Existing
   source IDs are idempotent; inventory uses `(product, warehouse)` plus
   `observed_at` freshness. Any database error rolls back the transaction.
4. **Reporting boundary** — typed contracts and `summary.py` expose per-source
   counts and structured rejection details, with manifest identity, seed, and
   `as_of` for reproducibility.

No exception detection, severity, lifecycle, dashboards, or new schema tables
are part of M02. The executable workflow is:

```text
python -m control_tower.synthetic generate --output-dir data/generated ...
python -m control_tower.ingestion ingest --input-dir data/generated
```
