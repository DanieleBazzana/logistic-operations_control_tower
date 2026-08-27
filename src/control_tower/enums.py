"""Shared domain enumerations persisted by the PostgreSQL model."""

from enum import StrEnum


class ExceptionType(StrEnum):
    """The six exception types defined by the MVP Charter."""

    SLA_BREACH_RISK = "SLA_BREACH_RISK"
    INVENTORY_SHORTAGE = "INVENTORY_SHORTAGE"
    STOCKOUT_RISK = "STOCKOUT_RISK"
    INVENTORY_MISMATCH = "INVENTORY_MISMATCH"
    SUPPLIER_DELAY = "SUPPLIER_DELAY"
    SHIPMENT_DELAY = "SHIPMENT_DELAY"


class ExceptionSeverity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ExceptionStatus(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    DISMISSED = "DISMISSED"


class OrderStatus(StrEnum):
    OPEN = "OPEN"
    FULFILLED = "FULFILLED"
    CANCELLED = "CANCELLED"


class PurchaseOrderStatus(StrEnum):
    OPEN = "OPEN"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
    RECEIVED = "RECEIVED"
    CANCELLED = "CANCELLED"


class ShipmentStatus(StrEnum):
    CREATED = "CREATED"
    IN_TRANSIT = "IN_TRANSIT"
    DELIVERED = "DELIVERED"
    EXCEPTION = "EXCEPTION"


class InventoryMovementType(StrEnum):
    RECEIPT = "RECEIPT"
    SHIPMENT = "SHIPMENT"
    ADJUSTMENT_IN = "ADJUSTMENT_IN"
    ADJUSTMENT_OUT = "ADJUSTMENT_OUT"
    RESERVATION = "RESERVATION"
    RELEASE = "RELEASE"
    TRANSFER_IN = "TRANSFER_IN"
    TRANSFER_OUT = "TRANSFER_OUT"
