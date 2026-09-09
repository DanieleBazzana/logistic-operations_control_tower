"""Stable validation-run evidence and KPI/queue coherence structures."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from control_tower.external_validation.contracts import mapping_dict


def _result_counts(result: Any) -> tuple[int, int, int]:
    if isinstance(result, Mapping):
        return (
            int(result.get("accepted", 0)),
            int(result.get("rejected", 0)),
            int(result.get("duplicate_identical", 0)),
        )
    return (
        int(getattr(result, "accepted", 0)),
        len(getattr(result, "rejections", [])),
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
    provenance: Sequence[Mapping[str, Any]] | None = None,
    mappings: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Build JSON-serializable evidence without contacting the API or database."""

    artifact_counts: dict[str, dict[str, int]] = {}
    accepted = rejected = duplicates = 0
    for artifact, result in validated.items():
        artifact_accepted, artifact_rejected, artifact_duplicates = _result_counts(result)
        artifact_counts[artifact] = {
            "accepted": artifact_accepted,
            "rejected": artifact_rejected,
            "duplicate_identical": artifact_duplicates,
        }
        accepted += artifact_accepted
        rejected += artifact_rejected
        duplicates += artifact_duplicates
    return {
        "dataset": dataset,
        "role": role,
        "sampling": {"max_rows": sample_size, "method": "stable_prefix"},
        "rows_read": dict(rows_read),
        "counts": {
            "accepted": accepted,
            "rejected": rejected,
            "duplicate_identical": duplicates,
        },
        "artifacts": artifact_counts,
        "unavailable_domains": sorted(set(unavailable)),
        "kpis": dict(kpis),
        "coherence": _coherence(kpis, queue_counts),
        "provenance": list(provenance or []),
        "mappings": [
            mapping if isinstance(mapping, Mapping) else mapping_dict(mapping)
            for mapping in (mappings or [])
        ],
        "api_called": False,
        "raw_data_committed": False,
    }


__all__ = ["build_validation_evidence"]
