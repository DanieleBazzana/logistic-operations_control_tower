"""Stable CSV and manifest writing for synthetic source artifacts."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any

ARTIFACT_COLUMNS: dict[str, tuple[str, ...]] = {
    "oms/products.csv": (
        "source_product_id",
        "sku",
        "name",
        "description",
        "unit_price",
        "active",
    ),
    "oms/orders.csv": (
        "source_order_id",
        "order_number",
        "status",
        "region",
        "source_warehouse_id",
        "ordered_at",
        "promised_at",
        "fulfilled_at",
        "total_amount",
        "currency",
    ),
    "oms/order_items.csv": (
        "source_order_item_id",
        "source_order_id",
        "source_product_id",
        "line_number",
        "ordered_quantity",
        "fulfilled_quantity",
        "unit_price",
    ),
    "wms/warehouses.csv": (
        "source_warehouse_id",
        "code",
        "name",
        "region",
        "timezone",
    ),
    "wms/inventory.csv": (
        "source_product_id",
        "source_warehouse_id",
        "on_hand",
        "reserved",
        "observed_at",
    ),
    "wms/inventory_movements.csv": (
        "source_movement_id",
        "source_product_id",
        "source_warehouse_id",
        "movement_type",
        "quantity",
        "occurred_at",
        "reference_type",
        "reference_id",
    ),
    "erp/suppliers.csv": (
        "source_supplier_id",
        "code",
        "name",
        "region",
        "active",
    ),
    "erp/purchase_orders.csv": (
        "source_purchase_order_id",
        "po_number",
        "source_supplier_id",
        "source_warehouse_id",
        "status",
        "ordered_at",
        "expected_delivery_at",
        "received_at",
    ),
    "erp/purchase_order_items.csv": (
        "source_purchase_order_item_id",
        "source_purchase_order_id",
        "source_product_id",
        "ordered_quantity",
        "received_quantity",
        "unit_cost",
    ),
    "carrier/shipments.csv": (
        "source_shipment_id",
        "source_order_id",
        "carrier",
        "tracking_id",
        "status",
        "shipped_at",
        "eta",
        "delivered_at",
    ),
}


def manifest_identity(manifest: dict[str, Any]) -> str:
    """Return a stable identity for a manifest independent of filesystem location."""

    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_artifact_rows(output_dir: Path, artifact_name: str, rows: list[dict[str, Any]]) -> int:
    """Write sorted rows as a UTF-8 CSV with a stable header and newline policy."""

    columns = ARTIFACT_COLUMNS[artifact_name]
    path = output_dir / artifact_name
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered_rows = sorted(
        rows, key=lambda row: tuple(str(row.get(column, "")) for column in columns)
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in ordered_rows:
            writer.writerow(
                {
                    column: "" if row.get(column) is None else str(row.get(column, ""))
                    for column in columns
                }
            )
    return len(rows)


def write_manifest(output_dir: Path, manifest: dict[str, Any]) -> Path:
    """Write the canonical manifest without a generation-time field."""

    path = output_dir / "manifest.json"
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


__all__ = ["ARTIFACT_COLUMNS", "manifest_identity", "write_artifact_rows", "write_manifest"]
