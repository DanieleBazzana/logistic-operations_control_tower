# M07.5 local external-dataset validation

This namespace validates local, user-authorized copies of two external datasets without
changing the production API, database schema, exception rules, severity thresholds, KPI
service, dashboard, bootstrap, or M07.4 files.

## Acquisition manifests

- `manifests/olist.json` is the primary comparison: Kaggle canonical record,
  CC BY-NC-SA 4.0.
- `manifests/dataco.json` is secondary only: Mendeley Data DOI record version 5,
  CC BY 4.0. Whether the dataset is observed or synthetic remains unclear and is
  preserved as a caveat.

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
output records row counts, accepted/rejected/identical-duplicate counts, mappings,
`SOURCE -> TRANSFORMATION -> OUTPUT` provenance, unavailable domains, independent
order-side KPIs, and KPI/queue coherence. It also records `api_called: false` and
`raw_data_committed: false`.

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
