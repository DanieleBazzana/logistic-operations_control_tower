"""Scenario labels used to make deterministic source fixtures useful to detection."""

SCENARIO_IDS = (
    "SLA_BREACH_RISK",
    "INVENTORY_SHORTAGE",
    "STOCKOUT_RISK",
    "INVENTORY_MISMATCH",
    "SUPPLIER_DELAY",
    "SHIPMENT_DELAY",
)


def scenario_manifest(source_ids: dict[str, str]) -> list[dict[str, str]]:
    """Build deterministic scenario metadata without creating exception records."""

    return [
        {"scenario_id": scenario_id, "source_id": source_ids[scenario_id]}
        for scenario_id in SCENARIO_IDS
    ]


__all__ = ["SCENARIO_IDS", "scenario_manifest"]
