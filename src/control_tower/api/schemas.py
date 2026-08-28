"""Stable Pydantic contracts for the M04 Operations API."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_tower.enums import (
    ExceptionSeverity,
    ExceptionStatus,
    ExceptionType,
    OrderStatus,
    PurchaseOrderStatus,
    ShipmentStatus,
)

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    page: int
    page_size: int
    total: int


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source_order_id: str
    order_number: str
    status: OrderStatus
    region: str
    source_warehouse_id: str
    ordered_at: str
    promised_at: str
    fulfilled_at: str | None
    total_amount: str
    currency: str
    items: list["OrderItemOut"] = Field(default_factory=list)
    shipments: list["ShipmentSummaryOut"] = Field(default_factory=list)


class OrderItemOut(BaseModel):
    source_product_id: str
    sku: str
    ordered_quantity: str
    fulfilled_quantity: str
    unit_price: str


class InventoryOut(BaseModel):
    source_product_id: str
    source_warehouse_id: str
    sku: str
    product_name: str
    on_hand: str
    reserved: str
    available: str
    observed_at: str


class PurchaseOrderOut(BaseModel):
    source_purchase_order_id: str
    po_number: str
    source_supplier_id: str
    supplier_name: str
    source_warehouse_id: str
    status: PurchaseOrderStatus
    ordered_at: str
    expected_delivery_at: str
    received_at: str | None
    remaining_quantity: str


class ShipmentOut(BaseModel):
    source_shipment_id: str
    source_order_id: str
    carrier: str
    tracking_id: str
    source_warehouse_id: str
    status: ShipmentStatus
    shipped_at: str | None
    eta: str
    delivered_at: str | None


class ShipmentSummaryOut(BaseModel):
    source_shipment_id: str
    carrier: str
    tracking_id: str
    status: ShipmentStatus
    shipped_at: str | None
    eta: str
    delivered_at: str | None


class ExceptionHistoryOut(BaseModel):
    id: int
    from_status: ExceptionStatus | None
    to_status: ExceptionStatus
    changed_at: str
    actor: str
    transition_reason: str | None


class ExceptionOut(BaseModel):
    id: int
    exception_type: ExceptionType
    issue_key: str
    entity_type: str
    entity_id: str
    severity: ExceptionSeverity
    status: ExceptionStatus
    detected_at: str
    expected_resolution: str | None
    business_impact: str
    revenue_at_risk: str
    orders_affected: int
    root_cause: str
    recommended_action: str
    confidence: str
    source_warehouse_id: str | None
    source_product_id: str | None
    resolved_at: str | None
    history: list[ExceptionHistoryOut] | None = None


class ExceptionStatusPatch(BaseModel):
    status: ExceptionStatus
    actor: str = Field(min_length=1, max_length=100)
    reason: str | None = None

    @model_validator(mode="before")
    @classmethod
    def trim_actor_and_reason(cls, values: object) -> object:
        if not isinstance(values, dict):
            return values
        values = dict(values)
        if isinstance(values.get("actor"), str):
            values["actor"] = values["actor"].strip()
        if isinstance(values.get("reason"), str):
            values["reason"] = values["reason"].strip()
        return values

    @model_validator(mode="after")
    def validate_actor_and_terminal_reason(self) -> "ExceptionStatusPatch":
        if not self.actor:
            raise ValueError("actor must be nonblank")
        if self.status in (ExceptionStatus.RESOLVED, ExceptionStatus.DISMISSED):
            if not self.reason:
                raise ValueError("reason is required for terminal exception status")
        return self


class KpiSummaryOut(BaseModel):
    as_of: str
    orders_processed: int
    open_orders: int
    fulfilled_orders: int
    cancelled_orders: int
    sla_performance_pct: str | None
    open_exceptions: int
    critical_exceptions: int
    revenue_at_risk: str
    stockout_risks: int
    supplier_delays: int
    shipment_delays: int
