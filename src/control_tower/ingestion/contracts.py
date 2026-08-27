"""Typed contracts shared by the strict ingestion pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Rejection:
    artifact: str
    row_number: int
    source_id: str | None
    error_code: str
    field: str | None
    message: str


@dataclass
class ValidationResult:
    artifact: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    duplicate_identical: int = 0

    @property
    def accepted(self) -> int:
        return len(self.rows)


@dataclass
class SourceSummary:
    source: str
    rows_read: int = 0
    accepted: int = 0
    rejected: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    conflicted: int = 0
    final_count: int = 0
    rejection_details: list[Rejection] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "rows_read": self.rows_read,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped": self.skipped,
            "conflicted": self.conflicted,
            "final_count": self.final_count,
            "rejection_details": [rejection.__dict__ for rejection in self.rejection_details],
        }


@dataclass
class IngestionResult:
    manifest_identity: str
    seed: int
    as_of: datetime
    summaries: list[SourceSummary]
    committed: bool
    manifest: dict[str, Any]

    @property
    def rejected(self) -> int:
        return sum(summary.rejected for summary in self.summaries)

    @property
    def inserted(self) -> int:
        return sum(summary.inserted for summary in self.summaries)

    @property
    def updated(self) -> int:
        return sum(summary.updated for summary in self.summaries)

    @property
    def skipped(self) -> int:
        return sum(summary.skipped for summary in self.summaries)


@dataclass(frozen=True)
class ArtifactBundle:
    root: Any
    manifest: dict[str, Any]
    rows: dict[str, list[dict[str, str]]]
    headers: dict[str, tuple[str, ...] | None] = field(default_factory=dict)


@dataclass(frozen=True)
class FieldSpec:
    required: bool = True
    kind: str = "string"
    enum: tuple[str, ...] = ()
    relation: str | None = None


SCHEMAS: dict[str, dict[str, FieldSpec]] = {
    "oms/products.csv": {
        "source_product_id": FieldSpec(),
        "sku": FieldSpec(),
        "name": FieldSpec(),
        "description": FieldSpec(required=False),
        "unit_price": FieldSpec(kind="money"),
        "active": FieldSpec(kind="bool"),
    },
    "oms/orders.csv": {
        "source_order_id": FieldSpec(),
        "order_number": FieldSpec(),
        "status": FieldSpec(enum=("OPEN", "FULFILLED", "CANCELLED")),
        "region": FieldSpec(),
        "source_warehouse_id": FieldSpec(relation="warehouses"),
        "ordered_at": FieldSpec(kind="timestamp"),
        "promised_at": FieldSpec(kind="timestamp"),
        "fulfilled_at": FieldSpec(required=False, kind="timestamp"),
        "total_amount": FieldSpec(kind="money"),
        "currency": FieldSpec(),
    },
    "oms/order_items.csv": {
        "source_order_item_id": FieldSpec(),
        "source_order_id": FieldSpec(relation="orders"),
        "source_product_id": FieldSpec(relation="products"),
        "line_number": FieldSpec(kind="int"),
        "ordered_quantity": FieldSpec(kind="positive_quantity"),
        "fulfilled_quantity": FieldSpec(kind="nonnegative_quantity"),
        "unit_price": FieldSpec(kind="money"),
    },
    "wms/warehouses.csv": {
        "source_warehouse_id": FieldSpec(),
        "code": FieldSpec(),
        "name": FieldSpec(),
        "region": FieldSpec(),
        "timezone": FieldSpec(),
    },
    "wms/inventory.csv": {
        "source_product_id": FieldSpec(relation="products"),
        "source_warehouse_id": FieldSpec(relation="warehouses"),
        "on_hand": FieldSpec(kind="nonnegative_quantity"),
        "reserved": FieldSpec(kind="nonnegative_quantity"),
        "observed_at": FieldSpec(kind="timestamp"),
    },
    "wms/inventory_movements.csv": {
        "source_movement_id": FieldSpec(),
        "source_product_id": FieldSpec(relation="products"),
        "source_warehouse_id": FieldSpec(relation="warehouses"),
        "movement_type": FieldSpec(
            enum=(
                "RECEIPT",
                "SHIPMENT",
                "ADJUSTMENT_IN",
                "ADJUSTMENT_OUT",
                "RESERVATION",
                "RELEASE",
                "TRANSFER_IN",
                "TRANSFER_OUT",
            )
        ),
        "quantity": FieldSpec(kind="positive_quantity"),
        "occurred_at": FieldSpec(kind="timestamp"),
        "reference_type": FieldSpec(required=False),
        "reference_id": FieldSpec(required=False),
    },
    "erp/suppliers.csv": {
        "source_supplier_id": FieldSpec(),
        "code": FieldSpec(),
        "name": FieldSpec(),
        "region": FieldSpec(),
        "active": FieldSpec(kind="bool"),
    },
    "erp/purchase_orders.csv": {
        "source_purchase_order_id": FieldSpec(),
        "po_number": FieldSpec(),
        "source_supplier_id": FieldSpec(relation="suppliers"),
        "source_warehouse_id": FieldSpec(relation="warehouses"),
        "status": FieldSpec(enum=("OPEN", "PARTIALLY_RECEIVED", "RECEIVED", "CANCELLED")),
        "ordered_at": FieldSpec(kind="timestamp"),
        "expected_delivery_at": FieldSpec(kind="timestamp"),
        "received_at": FieldSpec(required=False, kind="timestamp"),
    },
    "erp/purchase_order_items.csv": {
        "source_purchase_order_item_id": FieldSpec(),
        "source_purchase_order_id": FieldSpec(relation="purchase_orders"),
        "source_product_id": FieldSpec(relation="products"),
        "ordered_quantity": FieldSpec(kind="positive_quantity"),
        "received_quantity": FieldSpec(kind="nonnegative_quantity"),
        "unit_cost": FieldSpec(kind="money"),
    },
    "carrier/shipments.csv": {
        "source_shipment_id": FieldSpec(),
        "source_order_id": FieldSpec(relation="orders"),
        "carrier": FieldSpec(),
        "tracking_id": FieldSpec(),
        "status": FieldSpec(enum=("CREATED", "IN_TRANSIT", "DELIVERED", "EXCEPTION")),
        "shipped_at": FieldSpec(required=False, kind="timestamp"),
        "eta": FieldSpec(kind="timestamp"),
        "delivered_at": FieldSpec(required=False, kind="timestamp"),
    },
}

SOURCE_FIELDS = {
    "oms/products.csv": "source_product_id",
    "oms/orders.csv": "source_order_id",
    "oms/order_items.csv": "source_order_item_id",
    "wms/warehouses.csv": "source_warehouse_id",
    "wms/inventory_movements.csv": "source_movement_id",
    "erp/suppliers.csv": "source_supplier_id",
    "erp/purchase_orders.csv": "source_purchase_order_id",
    "erp/purchase_order_items.csv": "source_purchase_order_item_id",
    "carrier/shipments.csv": "source_shipment_id",
}
