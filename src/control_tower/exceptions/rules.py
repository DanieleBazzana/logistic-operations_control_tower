"""Six deterministic SQLAlchemy exception rules for M03."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from control_tower.config import Settings
from control_tower.enums import (
    ExceptionType,
    InventoryMovementType,
    OrderStatus,
    PurchaseOrderStatus,
    ShipmentStatus,
)
from control_tower.exceptions.contracts import ExceptionDetection, normalize_as_of
from control_tower.models import (
    Inventory,
    InventoryMovement,
    Order,
    OrderItem,
    Product,
    PurchaseOrder,
    PurchaseOrderItem,
    Shipment,
    Warehouse,
)


def _facts(**values: object) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((key, str(value)) for key, value in values.items()))


def _decimal(value: object) -> Decimal:
    return Decimal(str(value or 0))


def _aware(value: datetime) -> datetime:
    """Normalize driver-returned timestamps (SQLite may drop tzinfo in unit tests)."""

    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _overdue_hours(as_of: datetime, due: datetime) -> Decimal:
    seconds = max(Decimal("0"), Decimal(str((as_of - due).total_seconds())))
    return (seconds / Decimal("3600")).quantize(Decimal("0.0001"))


def detect_sla_breach_risk(
    session: Session, as_of: datetime, settings: Settings | None = None
) -> tuple[ExceptionDetection, ...]:
    """Find open, incompletely fulfilled orders due or within the risk window."""

    instant = normalize_as_of(as_of)
    configured = settings or Settings()
    risk_end = instant + timedelta(hours=configured.sla_risk_window_hours)
    remaining = OrderItem.ordered_quantity - OrderItem.fulfilled_quantity
    rows = session.execute(
        select(
            Order,
            func.sum(remaining).label("remaining"),
            func.sum(remaining * OrderItem.unit_price).label("revenue"),
        )
        .join(OrderItem, OrderItem.order_id == Order.id)
        .where(
            Order.status == OrderStatus.OPEN,
            Order.ordered_at <= instant,
            Order.promised_at <= risk_end,
            remaining > 0,
        )
        .group_by(Order.id)
        .order_by(Order.source_order_id)
    ).all()
    return tuple(
        ExceptionDetection(
            exception_type=ExceptionType.SLA_BREACH_RISK,
            issue_key=f"ORDER:{order.source_order_id}",
            entity_type="ORDER",
            entity_id=order.source_order_id,
            expected_resolution=_aware(order.promised_at),
            business_impact=(
                f"{remaining_qty} units remain unfulfilled on order {order.source_order_id}"
            ),
            revenue_at_risk=_decimal(revenue),
            orders_affected=1,
            root_cause=(
                f"Order promised at {_aware(order.promised_at).isoformat()} is overdue"
                if _aware(order.promised_at) <= instant
                else (
                    f"Order promised at {_aware(order.promised_at).isoformat()} "
                    "is inside the SLA risk window"
                )
            ),
            recommended_action="Prioritize fulfillment and confirm a revised customer commitment",
            overdue_hours=_overdue_hours(instant, _aware(order.promised_at)),
            warehouse_id=order.warehouse_id,
            source_facts=_facts(
                order=order.source_order_id,
                status=order.status,
                ordered_at=_aware(order.ordered_at).isoformat(),
                promised_at=_aware(order.promised_at).isoformat(),
                remaining=remaining_qty,
                revenue=_decimal(revenue),
            ),
        )
        for order, remaining_qty, revenue in rows
    )


def _open_demand(session: Session, instant: datetime):
    remaining = OrderItem.ordered_quantity - OrderItem.fulfilled_quantity
    return (
        select(
            OrderItem.product_id.label("product_id"),
            Order.warehouse_id.label("warehouse_id"),
            func.sum(remaining).label("demand"),
            func.sum(remaining * OrderItem.unit_price).label("revenue"),
            func.count(func.distinct(Order.id)).label("orders"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.status == OrderStatus.OPEN,
            Order.ordered_at <= instant,
            remaining > 0,
        )
        .group_by(OrderItem.product_id, Order.warehouse_id)
        .subquery("open_demand")
    )


def _inventory_demand_detections(
    session: Session, as_of: datetime, settings: Settings, exception_type: ExceptionType
) -> tuple[ExceptionDetection, ...]:
    instant = normalize_as_of(as_of)
    demand = _open_demand(session, instant)
    rows = session.execute(
        select(
            Product,
            Warehouse,
            Inventory,
            demand.c.demand,
            demand.c.revenue,
            demand.c.orders,
        )
        .join(demand, demand.c.product_id == Product.id)
        .join(
            Inventory,
            (Inventory.product_id == Product.id)
            & (Inventory.warehouse_id == demand.c.warehouse_id),
        )
        .join(Warehouse, Warehouse.id == demand.c.warehouse_id)
        .where(Inventory.observed_at <= instant)
        .order_by(Product.source_product_id, Warehouse.source_warehouse_id)
    ).all()
    detections: list[ExceptionDetection] = []
    for product, warehouse, inventory, demand_qty, revenue, orders in rows:
        demand_value = _decimal(demand_qty)
        available = _decimal(inventory.available)
        configured_safety = _decimal(settings.safety_stock)
        shortage = demand_value > available
        stockout = available - demand_value < configured_safety
        triggered = shortage if exception_type == ExceptionType.INVENTORY_SHORTAGE else stockout
        if not triggered:
            continue
        issue_key = f"PRODUCT_WAREHOUSE:{product.source_product_id}:{warehouse.source_warehouse_id}"
        if exception_type == ExceptionType.INVENTORY_SHORTAGE:
            root_cause = f"Open demand {demand_value} exceeds available inventory {available}"
            action = "Replenish inventory or rebalance demand to another warehouse"
            impact = f"{demand_value - available} units cannot be covered at the assigned warehouse"
        else:
            root_cause = (
                f"Projected available inventory after open demand is {available - demand_value}, "
                f"below safety stock {configured_safety}"
            )
            action = "Expedite replenishment and protect the configured safety-stock buffer"
            impact = f"Projected inventory buffer is {available - demand_value} units"
        facts = {
            "product": product.source_product_id,
            "warehouse": warehouse.source_warehouse_id,
            "demand": demand_value,
            "available": available,
            "orders": int(orders),
        }
        if exception_type == ExceptionType.STOCKOUT_RISK:
            facts["safety_stock"] = configured_safety
        detections.append(
            ExceptionDetection(
                exception_type=exception_type,
                issue_key=issue_key,
                entity_type="PRODUCT_WAREHOUSE",
                entity_id=f"{product.source_product_id}:{warehouse.source_warehouse_id}",
                expected_resolution=None,
                business_impact=impact,
                revenue_at_risk=_decimal(revenue),
                orders_affected=int(orders),
                root_cause=root_cause,
                recommended_action=action,
                product_id=product.id,
                warehouse_id=warehouse.id,
                source_facts=_facts(**facts),
            )
        )
    return tuple(detections)


def detect_inventory_shortage(
    session: Session, as_of: datetime, settings: Settings | None = None
) -> tuple[ExceptionDetection, ...]:
    return _inventory_demand_detections(
        session, as_of, settings or Settings(), ExceptionType.INVENTORY_SHORTAGE
    )


def detect_stockout_risk(
    session: Session, as_of: datetime, settings: Settings | None = None
) -> tuple[ExceptionDetection, ...]:
    return _inventory_demand_detections(
        session, as_of, settings or Settings(), ExceptionType.STOCKOUT_RISK
    )


def detect_inventory_mismatch(
    session: Session, as_of: datetime, settings: Settings | None = None
) -> tuple[ExceptionDetection, ...]:
    instant = normalize_as_of(as_of)
    configured = settings or Settings()
    snapshots = session.execute(
        select(Inventory, Product, Warehouse)
        .join(Product, Product.id == Inventory.product_id)
        .join(Warehouse, Warehouse.id == Inventory.warehouse_id)
        .where(Inventory.observed_at <= instant)
        .order_by(
            Product.source_product_id,
            Warehouse.source_warehouse_id,
            Inventory.observed_at.desc(),
            Inventory.id.desc(),
        )
    ).all()
    latest: dict[tuple[int, int], tuple[Inventory, Product, Warehouse]] = {}
    for inventory, product, warehouse in snapshots:
        latest.setdefault(
            (inventory.product_id, inventory.warehouse_id), (inventory, product, warehouse)
        )
    positive = [
        InventoryMovementType.RECEIPT,
        InventoryMovementType.ADJUSTMENT_IN,
        InventoryMovementType.TRANSFER_IN,
    ]
    negative = [
        InventoryMovementType.SHIPMENT,
        InventoryMovementType.ADJUSTMENT_OUT,
        InventoryMovementType.TRANSFER_OUT,
    ]
    signed_quantity = case(
        (InventoryMovement.movement_type.in_(positive), InventoryMovement.quantity),
        (InventoryMovement.movement_type.in_(negative), -InventoryMovement.quantity),
        else_=0,
    )
    detections: list[ExceptionDetection] = []
    for inventory, product, warehouse in latest.values():
        reconstructed = _decimal(
            session.scalar(
                select(func.coalesce(func.sum(signed_quantity), 0)).where(
                    InventoryMovement.product_id == inventory.product_id,
                    InventoryMovement.warehouse_id == inventory.warehouse_id,
                    InventoryMovement.occurred_at <= inventory.observed_at,
                )
            )
        )
        difference = abs(_decimal(inventory.on_hand) - reconstructed)
        if difference <= _decimal(configured.inventory_mismatch_tolerance):
            continue
        detections.append(
            ExceptionDetection(
                exception_type=ExceptionType.INVENTORY_MISMATCH,
                issue_key=f"PRODUCT_WAREHOUSE:{product.source_product_id}:{warehouse.source_warehouse_id}",
                entity_type="PRODUCT_WAREHOUSE",
                entity_id=f"{product.source_product_id}:{warehouse.source_warehouse_id}",
                expected_resolution=None,
                business_impact="Persisted inventory does not reconcile to movement history",
                root_cause=(
                    f"Snapshot on-hand {_decimal(inventory.on_hand)} differs from "
                    f"reconstructed {reconstructed} by {difference}"
                ),
                recommended_action="Reconcile the inventory snapshot and audit movement history",
                product_id=product.id,
                warehouse_id=warehouse.id,
                source_facts=_facts(
                    product=product.source_product_id,
                    warehouse=warehouse.source_warehouse_id,
                    observed_at=inventory.observed_at.isoformat(),
                    on_hand=_decimal(inventory.on_hand),
                    reconstructed=reconstructed,
                    difference=difference,
                    tolerance=_decimal(configured.inventory_mismatch_tolerance),
                ),
            )
        )
    return tuple(detections)


def detect_supplier_delay(
    session: Session, as_of: datetime, settings: Settings | None = None
) -> tuple[ExceptionDetection, ...]:
    instant = normalize_as_of(as_of)
    rows = session.execute(
        select(
            PurchaseOrder,
            func.sum(
                PurchaseOrderItem.ordered_quantity - PurchaseOrderItem.received_quantity
            ).label("remaining"),
        )
        .join(PurchaseOrderItem, PurchaseOrderItem.purchase_order_id == PurchaseOrder.id)
        .where(
            PurchaseOrder.status.in_(
                (PurchaseOrderStatus.OPEN, PurchaseOrderStatus.PARTIALLY_RECEIVED)
            ),
            PurchaseOrder.ordered_at <= instant,
            PurchaseOrder.expected_delivery_at < instant,
            PurchaseOrderItem.received_quantity < PurchaseOrderItem.ordered_quantity,
        )
        .group_by(PurchaseOrder.id)
        .order_by(PurchaseOrder.source_purchase_order_id)
    ).all()
    return tuple(
        ExceptionDetection(
            exception_type=ExceptionType.SUPPLIER_DELAY,
            issue_key=f"PO:{po.source_purchase_order_id}",
            entity_type="PURCHASE_ORDER",
            entity_id=po.source_purchase_order_id,
            expected_resolution=_aware(po.expected_delivery_at),
            business_impact=(
                f"{remaining} units remain due on overdue purchase order "
                f"{po.source_purchase_order_id}"
            ),
            root_cause=(
                f"Purchase order expected on {_aware(po.expected_delivery_at).isoformat()} "
                "remains partially or wholly unreceived"
            ),
            recommended_action=(
                "Contact the supplier and obtain a recovery date or alternate supply"
            ),
            overdue_hours=_overdue_hours(instant, _aware(po.expected_delivery_at)),
            warehouse_id=po.warehouse_id,
            source_facts=_facts(
                purchase_order=po.source_purchase_order_id,
                status=po.status,
                expected_delivery_at=_aware(po.expected_delivery_at).isoformat(),
                remaining=_decimal(remaining),
            ),
        )
        for po, remaining in rows
        if _decimal(remaining) > 0
    )


def detect_shipment_delay(
    session: Session, as_of: datetime, settings: Settings | None = None
) -> tuple[ExceptionDetection, ...]:
    instant = normalize_as_of(as_of)
    remaining = OrderItem.ordered_quantity - OrderItem.fulfilled_quantity
    rows = session.execute(
        select(
            Shipment,
            Order,
            func.coalesce(func.sum(remaining * OrderItem.unit_price), 0).label("revenue"),
        )
        .join(Order, Order.id == Shipment.order_id)
        .outerjoin(OrderItem, OrderItem.order_id == Order.id)
        .where(Shipment.status != ShipmentStatus.DELIVERED, Shipment.eta < instant)
        .group_by(Shipment.id, Order.id)
        .order_by(Shipment.source_shipment_id)
    ).all()
    return tuple(
        ExceptionDetection(
            exception_type=ExceptionType.SHIPMENT_DELAY,
            issue_key=f"SHIPMENT:{shipment.source_shipment_id}",
            entity_type="SHIPMENT",
            entity_id=shipment.source_shipment_id,
            expected_resolution=_aware(shipment.eta),
            business_impact=(
                f"Shipment {shipment.source_shipment_id} is overdue for order "
                f"{order.source_order_id}"
            ),
            revenue_at_risk=_decimal(revenue),
            orders_affected=1,
            root_cause=(
                f"Shipment ETA {_aware(shipment.eta).isoformat()} passed while status "
                f"is {shipment.status}"
            ),
            recommended_action="Trace the shipment with the carrier and communicate a revised ETA",
            warehouse_id=order.warehouse_id,
            source_facts=_facts(
                shipment=shipment.source_shipment_id,
                status=shipment.status,
                eta=_aware(shipment.eta).isoformat(),
                order=order.source_order_id,
                revenue=_decimal(revenue),
            ),
        )
        for shipment, order, revenue in rows
    )


Rule = Callable[[Session, datetime, Settings | None], tuple[ExceptionDetection, ...]]
RULES: tuple[Rule, ...] = (
    detect_sla_breach_risk,
    detect_inventory_shortage,
    detect_stockout_risk,
    detect_inventory_mismatch,
    detect_supplier_delay,
    detect_shipment_delay,
)


def detect_all(
    session: Session, as_of: datetime, settings: Settings | None = None
) -> tuple[ExceptionDetection, ...]:
    """Run all six rules in stable rule/entity order."""

    instant = normalize_as_of(as_of)
    configured = settings or Settings()
    findings: list[ExceptionDetection] = []
    for rule in RULES:
        findings.extend(rule(session, instant, configured))
    return tuple(findings)


__all__ = [
    "RULES",
    "detect_all",
    "detect_inventory_mismatch",
    "detect_inventory_shortage",
    "detect_shipment_delay",
    "detect_sla_breach_risk",
    "detect_stockout_risk",
    "detect_supplier_delay",
]
