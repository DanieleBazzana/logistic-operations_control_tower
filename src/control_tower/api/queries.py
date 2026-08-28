"""Read-only SQLAlchemy queries used by the M04 routes."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from control_tower.models import (
    ExceptionRecord,
    Inventory,
    Order,
    OrderItem,
    PurchaseOrder,
    PurchaseOrderItem,
    Shipment,
    Warehouse,
)


def _page_query(
    session: Session,
    statement: Select[Any],
    model: type[Any],
    page: int,
    page_size: int,
    order_by: Sequence[Any],
) -> tuple[list[Any], int]:
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = session.scalars(
        statement.order_by(*order_by).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return rows, total


def list_orders(
    session: Session,
    *,
    page: int,
    page_size: int,
    statuses: Sequence[Any] | None = None,
    region: str | None = None,
    warehouse_source_id: str | None = None,
    ordered_from: Any = None,
    ordered_to: Any = None,
) -> tuple[list[Order], int]:
    statement = (
        select(Order)
        .join(Order.warehouse)
        .options(
            joinedload(Order.warehouse),
            selectinload(Order.items).joinedload(OrderItem.product),
            selectinload(Order.shipments),
        )
    )
    if statuses:
        statement = statement.where(Order.status.in_(statuses))
    if region is not None:
        statement = statement.where(Order.region == region)
    if warehouse_source_id is not None:
        statement = statement.where(Warehouse.source_warehouse_id == warehouse_source_id)
    if ordered_from is not None:
        statement = statement.where(Order.ordered_at >= ordered_from)
    if ordered_to is not None:
        statement = statement.where(Order.ordered_at <= ordered_to)
    return _page_query(session, statement, Order, page, page_size, (Order.source_order_id,))


def get_order(session: Session, source_order_id: str) -> Order | None:
    return session.scalar(
        select(Order)
        .join(Order.warehouse)
        .options(
            joinedload(Order.warehouse),
            selectinload(Order.items).joinedload(OrderItem.product),
            selectinload(Order.shipments),
        )
        .where(Order.source_order_id == source_order_id)
    )


def list_inventory(
    session: Session,
    *,
    page: int,
    page_size: int,
    product_source_id: str | None = None,
    warehouse_source_id: str | None = None,
    sku: str | None = None,
    available_max: Any = None,
    observed_from: Any = None,
    observed_to: Any = None,
) -> tuple[list[Inventory], int]:
    statement = (
        select(Inventory)
        .join(Inventory.product)
        .join(Inventory.warehouse)
        .options(joinedload(Inventory.product), joinedload(Inventory.warehouse))
    )
    if product_source_id is not None:
        statement = statement.where(Inventory.product.has(source_product_id=product_source_id))
    if sku is not None:
        statement = statement.where(Inventory.product.has(sku=sku))
    if warehouse_source_id is not None:
        statement = statement.where(
            Inventory.warehouse.has(source_warehouse_id=warehouse_source_id)
        )
    if available_max is not None:
        statement = statement.where(Inventory.available <= available_max)
    if observed_from is not None:
        statement = statement.where(Inventory.observed_at >= observed_from)
    if observed_to is not None:
        statement = statement.where(Inventory.observed_at <= observed_to)
    return _page_query(
        session,
        statement,
        Inventory,
        page,
        page_size,
        (Inventory.product_id, Inventory.warehouse_id),
    )


def list_purchase_orders(
    session: Session,
    *,
    page: int,
    page_size: int,
    statuses: Sequence[Any] | None = None,
    supplier_source_id: str | None = None,
    warehouse_source_id: str | None = None,
    expected_from: Any = None,
    expected_to: Any = None,
    ordered_from: Any = None,
    ordered_to: Any = None,
    remaining_min: Any = None,
    remaining_max: Any = None,
) -> tuple[list[PurchaseOrder], int]:
    statement = select(PurchaseOrder).options(
        joinedload(PurchaseOrder.supplier), joinedload(PurchaseOrder.warehouse)
    )
    if statuses:
        statement = statement.where(PurchaseOrder.status.in_(statuses))
    if supplier_source_id is not None:
        statement = statement.where(
            PurchaseOrder.supplier.has(source_supplier_id=supplier_source_id)
        )
    if warehouse_source_id is not None:
        statement = statement.where(
            PurchaseOrder.warehouse.has(source_warehouse_id=warehouse_source_id)
        )
    if expected_from is not None:
        statement = statement.where(PurchaseOrder.expected_delivery_at >= expected_from)
    if expected_to is not None:
        statement = statement.where(PurchaseOrder.expected_delivery_at <= expected_to)
    if ordered_from is not None:
        statement = statement.where(PurchaseOrder.ordered_at >= ordered_from)
    if ordered_to is not None:
        statement = statement.where(PurchaseOrder.ordered_at <= ordered_to)
    remaining = (
        select(
            func.coalesce(
                func.sum(
                    PurchaseOrderItem.ordered_quantity
                    - func.coalesce(PurchaseOrderItem.received_quantity, 0)
                ),
                0,
            )
        )
        .where(PurchaseOrderItem.purchase_order_id == PurchaseOrder.id)
        .scalar_subquery()
    )
    if remaining_min is not None:
        statement = statement.where(remaining >= remaining_min)
    if remaining_max is not None:
        statement = statement.where(remaining <= remaining_max)
    statement = statement.options(
        selectinload(PurchaseOrder.items).joinedload(PurchaseOrderItem.product)
    )
    return _page_query(
        session,
        statement,
        PurchaseOrder,
        page,
        page_size,
        (PurchaseOrder.source_purchase_order_id,),
    )


def list_shipments(
    session: Session,
    *,
    page: int,
    page_size: int,
    statuses: Sequence[Any] | None = None,
    carrier: str | None = None,
    order_source_id: str | None = None,
    warehouse_source_id: str | None = None,
    eta_from: Any = None,
    eta_to: Any = None,
) -> tuple[list[Shipment], int]:
    statement = select(Shipment).options(joinedload(Shipment.order).joinedload(Order.warehouse))
    if statuses:
        statement = statement.where(Shipment.status.in_(statuses))
    if carrier is not None:
        statement = statement.where(Shipment.carrier == carrier)
    if order_source_id is not None:
        statement = statement.where(Shipment.order.has(source_order_id=order_source_id))
    if warehouse_source_id is not None:
        statement = statement.where(
            Shipment.order.has(Order.warehouse.has(source_warehouse_id=warehouse_source_id))
        )
    if eta_from is not None:
        statement = statement.where(Shipment.eta >= eta_from)
    if eta_to is not None:
        statement = statement.where(Shipment.eta <= eta_to)
    return _page_query(
        session, statement, Shipment, page, page_size, (Shipment.source_shipment_id,)
    )


def list_exceptions(
    session: Session,
    *,
    page: int,
    page_size: int,
    exception_types: Sequence[Any] | None = None,
    severities: Sequence[Any] | None = None,
    statuses: Sequence[Any] | None = None,
    entity: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    product_source_id: str | None = None,
    warehouse_source_id: str | None = None,
    detected_from: Any = None,
    detected_to: Any = None,
) -> tuple[list[ExceptionRecord], int]:
    statement = select(ExceptionRecord).options(
        joinedload(ExceptionRecord.warehouse), joinedload(ExceptionRecord.product)
    )
    if exception_types:
        statement = statement.where(ExceptionRecord.exception_type.in_(exception_types))
    if severities:
        statement = statement.where(ExceptionRecord.severity.in_(severities))
    if statuses:
        statement = statement.where(ExceptionRecord.status.in_(statuses))
    if entity is not None:
        statement = statement.where(ExceptionRecord.entity_type == entity)
    if entity_type is not None:
        statement = statement.where(ExceptionRecord.entity_type == entity_type)
    if entity_id is not None:
        statement = statement.where(ExceptionRecord.entity_id == entity_id)
    if product_source_id is not None:
        statement = statement.where(
            ExceptionRecord.product.has(source_product_id=product_source_id)
        )
    if warehouse_source_id is not None:
        statement = statement.where(
            ExceptionRecord.warehouse.has(source_warehouse_id=warehouse_source_id)
        )
    if detected_from is not None:
        statement = statement.where(ExceptionRecord.detected_at >= detected_from)
    if detected_to is not None:
        statement = statement.where(ExceptionRecord.detected_at <= detected_to)
    return _page_query(session, statement, ExceptionRecord, page, page_size, (ExceptionRecord.id,))


def get_exception(session: Session, exception_id: int) -> ExceptionRecord | None:
    return session.scalar(
        select(ExceptionRecord)
        .options(
            joinedload(ExceptionRecord.warehouse),
            joinedload(ExceptionRecord.product),
            selectinload(ExceptionRecord.history),
        )
        .where(ExceptionRecord.id == exception_id)
    )
