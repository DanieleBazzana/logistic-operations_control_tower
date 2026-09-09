# M07.5 local external-dataset validation

This namespace validates local, user-authorized copies of two external datasets without
changing the production API, database schema, exception rules, severity thresholds, KPI
service, dashboard, bootstrap, or M07.4 files.

## Acquisition manifests

- `manifests/olist.json` is the primary comparison: Kaggle canonical record,
  CC BY-NC-SA 4.0.
- `manifests/dataco.json` is secondary only: Mendeley Data DOI record version 5,
  CC BY 4.0. Whether the dataset is observed or synthetic remains unclear and is
  preserved as a caveat. The verified local file uses `latin-1` and has the manifest's
  recorded SHA-256 checksum; the raw file remains outside the repository. The runner
  hashes the declared file before CSV parsing and fails on a missing file or checksum
  mismatch, so evidence is never emitted for unverified bytes.
- `manifests/olist.json` remains primary. The canonical version 2 archive was acquired
  and verified locally outside the repository; its archive checksum and each of the
  nine CSV file checksums, byte sizes, and logical row counts are committed as
  provenance. Raw files remain outside the repository. Olist verification records
  the archive and every CSV file's filename, byte size, SHA-256, and logical CSV row
  count (records after the header, not physical newline count), including quoted
  newlines. DataCo retains its scalar filename/encoding/SHA-256 provenance fields.

The manifests contain URLs, licenses, expected filenames, and a no-raw-data policy.
They do not contain credentials, tokens, or downloaded data. The validator never
performs network acquisition.

## Local run

Place user-authorized CSVs outside the repository, then run for a bounded deterministic
prefix sample:

```bash
.venv/bin/python scripts/validate_external_dataset.py olist \
  --input-dir /path/to/olist \
  --sample-size 1000 \
  --as-of 2018-12-31T23:59:59Z \
  --output /tmp/olist-m075-evidence.json
```

Use `dataco` with the DataCo manifest filename for the secondary comparison. The JSON
output records `source_line_rows`, `adapted_orders`, `accepted_orders`,
`rejected_orders`, `rejection_errors`, and `duplicate_identical` separately at both
the aggregate and artifact levels. `rejected_orders` counts unique rejected orders;
`rejection_errors` counts validation errors, so multiple missing fields on one order
are not misreported as multiple rejected orders. It also records committed manifest
provenance (`source_version`, filename, encoding, SHA-256, and verification status),
`SOURCE -> TRANSFORMATION -> OUTPUT` provenance, unavailable domains, independent
order-side KPIs, and KPI/queue coherence. For DataCo, manifest provenance retains
scalar filename, encoding, SHA-256, and verification status; for Olist it includes
version, license, encoding, archive metadata, and per-file metadata. It also records
`api_called: false` and `raw_data_committed: false`.

The DataCo adapter groups source line rows by `Order Id` at order level and retains
only the first source row for the independent order-side KPI calculation. The
independent point-in-time KPI path requires a valid order date at or before `as-of`
and only counts fulfilled orders with a valid fulfillment timestamp at or before
`as-of`; missing or invalid timestamps are not fabricated. DataCo's
`source_warehouse_id`, `promised_at`, and `currency` remain unavailable and therefore
continue to be rejected by the existing ingestion contract; no mapping is invented.
When both promised and fulfilled timestamps are unavailable, SLA is reported as
unavailable (`null`), never as zero.

## Mapping and truth boundary

Adapters emit only existing ingestion-contract artifacts. Each mapped field is labeled
`DIRECT`, `DERIVED`, `APPROXIMATE`, or `UNAVAILABLE`. A missing required contract field
is left null and rejected by the existing side-effect-free validator; it is not filled
from a guessed supplier, inventory, warehouse, carrier, quantity, delay, severity,
critical/high finding, or revenue-at-risk value.

- Olist can support order-side comparison and payment-derived order totals, but has no
  trustworthy Control Tower warehouse/currency contract fields in this mapping.
- DataCo can support a secondary order-side comparison, but shipping schedule is not
  reinterpreted as promised delivery, and shipping date is not reinterpreted as a
  delivered-at timestamp.
- Supplier, inventory, carrier, exception queue, severity, and revenue-at-risk domains
  remain unavailable. No detection run is invoked.

The independent KPI module does not import or call FastAPI, the API client, the KPI
service, ORM models, or exception formulas. Queue coherence is `NOT_COMPARABLE` when
external data cannot supply the corresponding domain; this is evidence, not a pass.
