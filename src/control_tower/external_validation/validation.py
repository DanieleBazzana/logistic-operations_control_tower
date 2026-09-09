"""Bridge external adapter output to the existing, side-effect-free row validator."""

from __future__ import annotations

from control_tower.external_validation.contracts import AdapterResult
from control_tower.ingestion.contracts import ValidationResult
from control_tower.ingestion.validation import validate_rows


def validate_adapted(adapter_result: AdapterResult) -> dict[str, ValidationResult]:
    """Validate each adapted artifact without persistence, API calls, or detection."""

    known_ids: dict[str, set[str]] = {}
    results: dict[str, ValidationResult] = {}
    for artifact, rows in adapter_result.rows_by_artifact.items():
        results[artifact] = validate_rows(artifact, rows, known_ids=known_ids)
        source_field = {
            "oms/orders.csv": "source_order_id",
            "oms/products.csv": "source_product_id",
        }.get(artifact)
        if source_field:
            known_ids[artifact.split("/")[-1].split(".")[0]] = {
                str(row[source_field]) for row in results[artifact].rows
            }
    return results


__all__ = ["validate_adapted"]
