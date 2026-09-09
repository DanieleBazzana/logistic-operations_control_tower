"""Stable validation-run evidence and KPI/queue coherence structures."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from control_tower.external_validation.contracts import mapping_dict


def _rejected_order_count(rejections: Sequence[Any]) -> int:
    identities = {
        (rejection.source_id, rejection.row_number)
        if getattr(rejection, "source_id", None) is None
        else ("source_id", rejection.source_id)
        for rejection in rejections
    }
    return len(identities)


def _result_counts(result: Any) -> tuple[int, int, int, int]:
    if isinstance(result, Mapping):
        return (
            int(result.get("accepted_orders", result.get("accepted", 0))),
            int(result.get("rejected_orders", result.get("rejected", 0))),
            int(result.get("rejection_errors", result.get("rejected", 0))),
            int(result.get("duplicate_identical", 0)),
        )
    rejections = getattr(result, "rejections", [])
    return (
        int(getattr(result, "accepted", 0)),
        _rejected_order_count(rejections),
        len(rejections),
        int(getattr(result, "duplicate_identical", 0)),
    )


def _coherence(kpis: Mapping[str, Any], queue_counts: Mapping[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for field in (
        "open_exceptions",
        "critical_exceptions",
        "stockout_risks",
        "supplier_delays",
        "shipment_delays",
    ):
        expected = kpis.get(field)
        observed = queue_counts.get(field)
        if expected is None or observed is None:
            status = "UNAVAILABLE"
            reason = "external source does not provide a trustworthy exception/queue domain"
        else:
            status = "PASS" if expected == observed else "MISMATCH"
            reason = (
                "independent value equals supplied local queue count"
                if status == "PASS"
                else "values differ"
            )
        checks.append(
            {
                "field": field,
                "expected": expected,
                "observed": observed,
                "status": status,
                "reason": reason,
            }
        )
    comparable = [check for check in checks if check["status"] in {"PASS", "MISMATCH"}]
    status = (
        "NOT_COMPARABLE"
        if not comparable
        else ("PASS" if all(check["status"] == "PASS" for check in comparable) else "MISMATCH")
    )
    return {"status": status, "checks": checks}


def build_validation_evidence(
    *,
    dataset: str,
    role: str,
    sample_size: int | None,
    rows_read: Mapping[str, int],
    validated: Mapping[str, Any],
    unavailable: Sequence[str],
    kpis: Mapping[str, Any],
    queue_counts: Mapping[str, Any],
    source_line_rows: Mapping[str, int] | None = None,
    adapted_orders: Mapping[str, int] | None = None,
    provenance: Sequence[Mapping[str, Any]] | None = None,
    mappings: Sequence[Any] | None = None,
    manifest_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build JSON-serializable evidence without contacting the API or database."""

    artifact_counts: dict[str, dict[str, int]] = {}
    source_lines_by_artifact = dict(source_line_rows or rows_read)
    adapted_orders_by_artifact = dict(adapted_orders or {})
    source_line_total = sum(source_lines_by_artifact.values())
    adapted_order_total = sum(adapted_orders_by_artifact.values())
    accepted = rejected_orders = rejection_errors = duplicates = 0
    for artifact, result in validated.items():
        (
            artifact_accepted,
            artifact_rejected_orders,
            artifact_rejection_errors,
            artifact_duplicates,
        ) = _result_counts(result)
        artifact_adapted_orders = adapted_orders_by_artifact.get(
            artifact, artifact_accepted + artifact_rejected_orders + artifact_duplicates
        )
        artifact_source_line_rows = source_lines_by_artifact.get(artifact, 0)
        artifact_counts[artifact] = {
            "source_line_rows": artifact_source_line_rows,
            "adapted_orders": artifact_adapted_orders,
            "accepted_orders": artifact_accepted,
            "rejected_orders": artifact_rejected_orders,
            "rejection_errors": artifact_rejection_errors,
            "duplicate_identical": artifact_duplicates,
        }
        accepted += artifact_accepted
        rejected_orders += artifact_rejected_orders
        rejection_errors += artifact_rejection_errors
        duplicates += artifact_duplicates
    if not adapted_orders_by_artifact:
        adapted_order_total = sum(item["adapted_orders"] for item in artifact_counts.values())
    if not source_line_rows:
        source_line_total = sum(item["source_line_rows"] for item in artifact_counts.values())
    return {
        "dataset": dataset,
        "role": role,
        "sampling": {"max_rows": sample_size, "method": "stable_prefix"},
        "rows_read": dict(rows_read),
        "counts": {
            "source_line_rows": source_line_total,
            "adapted_orders": adapted_order_total,
            "accepted_orders": accepted,
            "rejected_orders": rejected_orders,
            "rejection_errors": rejection_errors,
            "duplicate_identical": duplicates,
        },
        "artifacts": artifact_counts,
        "unavailable_domains": sorted(set(unavailable)),
        "kpis": dict(kpis),
        "coherence": _coherence(kpis, queue_counts),
        "provenance": list(provenance or []),
        "manifest_provenance": dict(manifest_provenance or {}),
        "mappings": [
            mapping if isinstance(mapping, Mapping) else mapping_dict(mapping)
            for mapping in (mappings or [])
        ],
        "api_called": False,
        "raw_data_committed": False,
    }


__all__ = ["build_validation_evidence"]
