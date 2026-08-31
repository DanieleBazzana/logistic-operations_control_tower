"""Versioned read/write-safe HTTP routes for operational data."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from control_tower.api import queries
from control_tower.api.dependencies import get_engine, get_session, get_settings
from control_tower.api.schemas import (
    ExceptionHistoryOut,
    ExceptionOut,
    ExceptionStatusPatch,
    InventoryOut,
    KpiSummaryOut,
    OrderItemOut,
    OrderOut,
    Page,
    PurchaseOrderOut,
    ShipmentOut,
    ShipmentSummaryOut,
)
from control_tower.config import Settings
from control_tower.db import check_database_health
from control_tower.enums import (
    ExceptionSeverity,
    ExceptionStatus,
    ExceptionType,
    OrderStatus,
    PurchaseOrderStatus,
    ShipmentStatus,
)
from control_tower.exceptions.lifecycle import InvalidTransition, transition_exception
from control_tower.kpis.service import KPIService

router = APIRouter()
SessionDependency = Annotated[Session, Depends(get_session)]
SettingsDependency = Annotated[Settings, Depends(get_settings)]


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    instant = (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )
    return instant.isoformat().replace("+00:00", "Z")


def _money(value: Decimal | int | float | None) -> str:
    return f"{Decimal(value if value is not None else 0):.2f}"


def _quantity(value: Decimal | int | float | None) -> str:
    return f"{Decimal(value if value is not None else 0):.3f}"


def _normalize_filter(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(status_code=422, detail="timestamp filters must include a timezone")
    return value.astimezone(timezone.utc)


def _order_out(order) -> OrderOut:
    return OrderOut(
        source_order_id=order.source_order_id,
        order_number=order.order_number,
        status=order.status,
        region=order.region,
        source_warehouse_id=order.warehouse.source_warehouse_id,
        ordered_at=_timestamp(order.ordered_at),
        promised_at=_timestamp(order.promised_at),
        fulfilled_at=_timestamp(order.fulfilled_at),
        total_amount=_money(order.total_amount),
        currency=order.currency,
        items=[
            OrderItemOut(
                source_product_id=item.product.source_product_id,
                sku=item.product.sku,
                ordered_quantity=_quantity(item.ordered_quantity),
                fulfilled_quantity=_quantity(item.fulfilled_quantity),
                unit_price=_money(item.unit_price),
            )
            for item in order.items
        ],
        shipments=[
            ShipmentSummaryOut(
                source_shipment_id=shipment.source_shipment_id,
                carrier=shipment.carrier,
                tracking_id=shipment.tracking_id,
                status=shipment.status,
                shipped_at=_timestamp(shipment.shipped_at),
                eta=_timestamp(shipment.eta),
                delivered_at=_timestamp(shipment.delivered_at),
            )
            for shipment in order.shipments
        ],
    )


def _inventory_out(row) -> InventoryOut:
    return InventoryOut(
        source_product_id=row.product.source_product_id,
        source_warehouse_id=row.warehouse.source_warehouse_id,
        sku=row.product.sku,
        product_name=row.product.name,
        on_hand=_quantity(row.on_hand),
        reserved=_quantity(row.reserved),
        available=_quantity(row.available),
        observed_at=_timestamp(row.observed_at),
    )


def _purchase_order_out(row) -> PurchaseOrderOut:
    return PurchaseOrderOut(
        source_purchase_order_id=row.source_purchase_order_id,
        po_number=row.po_number,
        source_supplier_id=row.supplier.source_supplier_id,
        supplier_name=row.supplier.name,
        source_warehouse_id=row.warehouse.source_warehouse_id,
        status=row.status,
        ordered_at=_timestamp(row.ordered_at),
        expected_delivery_at=_timestamp(row.expected_delivery_at),
        received_at=_timestamp(row.received_at),
        remaining_quantity=_quantity(row.remaining_quantity),
    )


def _shipment_out(row) -> ShipmentOut:
    return ShipmentOut(
        source_shipment_id=row.source_shipment_id,
        source_order_id=row.order.source_order_id,
        source_warehouse_id=row.order.warehouse.source_warehouse_id,
        carrier=row.carrier,
        tracking_id=row.tracking_id,
        status=row.status,
        shipped_at=_timestamp(row.shipped_at),
        eta=_timestamp(row.eta),
        delivered_at=_timestamp(row.delivered_at),
    )


def _exception_out(row, include_history: bool = False) -> ExceptionOut:
    return ExceptionOut(
        id=row.id,
        exception_type=row.exception_type,
        issue_key=row.issue_key,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        severity=row.severity,
        status=row.status,
        detected_at=_timestamp(row.detected_at),
        expected_resolution=_timestamp(row.expected_resolution),
        business_impact=row.business_impact,
        revenue_at_risk=_money(row.revenue_at_risk),
        orders_affected=row.orders_affected,
        root_cause=row.root_cause,
        recommended_action=row.recommended_action,
        confidence=f"{Decimal(row.confidence):.4f}",
        source_warehouse_id=row.warehouse.source_warehouse_id if row.warehouse else None,
        source_product_id=row.product.source_product_id if row.product else None,
        resolved_at=_timestamp(row.resolved_at),
        history=(
            [
                ExceptionHistoryOut(
                    id=entry.id,
                    from_status=entry.from_status,
                    to_status=entry.to_status,
                    changed_at=_timestamp(entry.changed_at),
                    actor=entry.actor,
                    transition_reason=entry.transition_reason,
                )
                for entry in row.history
            ]
            if include_history
            else None
        ),
    )


@router.get("/health")
def health(engine=Depends(get_engine)) -> dict[str, str]:
    try:
        check_database_health(engine)
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok"}


@router.get("/livez")
def liveness() -> dict[str, str]:
    """Report process liveness without touching the database."""

    return {"status": "ok"}


@router.get("/readyz")
def readiness(engine=Depends(get_engine)) -> dict[str, str]:
    """Report readiness only when the configured database accepts a query."""

    try:
        check_database_health(engine)
    except Exception:
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok"}


@router.get("/orders", response_model=Page[OrderOut])
def orders(
    session: SessionDependency,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    status: list[OrderStatus] | None = Query(None),
    region: str | None = None,
    warehouse_id: str | None = None,
    ordered_from: datetime | None = None,
    ordered_to: datetime | None = None,
) -> Page[OrderOut]:
    rows, total = queries.list_orders(
        session,
        page=page,
        page_size=page_size,
        statuses=status,
        region=region,
        warehouse_source_id=warehouse_id,
        ordered_from=_normalize_filter(ordered_from),
        ordered_to=_normalize_filter(ordered_to),
    )
    return Page(
        items=[_order_out(row) for row in rows], page=page, page_size=page_size, total=total
    )


@router.get("/orders/{source_order_id}", response_model=OrderOut)
def order_detail(source_order_id: str, session: SessionDependency) -> OrderOut:
    row = queries.get_order(session, source_order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="order not found")
    return _order_out(row)


@router.get("/inventory", response_model=Page[InventoryOut])
def inventory(
    session: SessionDependency,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    product_id: str | None = None,
    warehouse_id: str | None = None,
    sku: str | None = None,
    available_max: Decimal | None = None,
    observed_from: datetime | None = None,
    observed_to: datetime | None = None,
) -> Page[InventoryOut]:
    rows, total = queries.list_inventory(
        session,
        page=page,
        page_size=page_size,
        product_source_id=product_id,
        warehouse_source_id=warehouse_id,
        sku=sku,
        available_max=available_max,
        observed_from=_normalize_filter(observed_from),
        observed_to=_normalize_filter(observed_to),
    )
    return Page(
        items=[_inventory_out(row) for row in rows], page=page, page_size=page_size, total=total
    )


@router.get("/purchase-orders", response_model=Page[PurchaseOrderOut])
def purchase_orders(
    session: SessionDependency,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    status: list[PurchaseOrderStatus] | None = Query(None),
    supplier_id: str | None = None,
    warehouse_id: str | None = None,
    expected_from: datetime | None = None,
    expected_to: datetime | None = None,
    ordered_from: datetime | None = None,
    ordered_to: datetime | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    remaining_min: Decimal | None = None,
    remaining_max: Decimal | None = None,
    remaining_quantity_min: Decimal | None = None,
    remaining_quantity_max: Decimal | None = None,
) -> Page[PurchaseOrderOut]:
    rows, total = queries.list_purchase_orders(
        session,
        page=page,
        page_size=page_size,
        statuses=status,
        supplier_source_id=supplier_id,
        warehouse_source_id=warehouse_id,
        expected_from=_normalize_filter(expected_from),
        expected_to=_normalize_filter(expected_to),
        ordered_from=_normalize_filter(ordered_from or date_from),
        ordered_to=_normalize_filter(ordered_to or date_to),
        remaining_min=remaining_min if remaining_min is not None else remaining_quantity_min,
        remaining_max=remaining_max if remaining_max is not None else remaining_quantity_max,
    )
    return Page(
        items=[_purchase_order_out(row) for row in rows],
        page=page,
        page_size=page_size,
        total=total,
    )


@router.get("/shipments", response_model=Page[ShipmentOut])
def shipments(
    session: SessionDependency,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    status: list[ShipmentStatus] | None = Query(None),
    carrier: str | None = None,
    order_id: str | None = None,
    warehouse_id: str | None = None,
    eta_from: datetime | None = None,
    eta_to: datetime | None = None,
) -> Page[ShipmentOut]:
    rows, total = queries.list_shipments(
        session,
        page=page,
        page_size=page_size,
        statuses=status,
        carrier=carrier,
        order_source_id=order_id,
        warehouse_source_id=warehouse_id,
        eta_from=_normalize_filter(eta_from),
        eta_to=_normalize_filter(eta_to),
    )
    return Page(
        items=[_shipment_out(row) for row in rows], page=page, page_size=page_size, total=total
    )


@router.get("/exceptions", response_model=Page[ExceptionOut])
def exceptions(
    session: SessionDependency,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    exception_type: list[ExceptionType] | None = Query(None),
    type: list[ExceptionType] | None = Query(None),
    severity: list[ExceptionSeverity] | None = Query(None),
    status: list[ExceptionStatus] | None = Query(None),
    entity: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    source_product_id: str | None = None,
    warehouse_id: str | None = None,
    detected_from: datetime | None = None,
    detected_to: datetime | None = None,
) -> Page[ExceptionOut]:
    rows, total = queries.list_exceptions(
        session,
        page=page,
        page_size=page_size,
        exception_types=(exception_type or []) + (type or []),
        severities=severity,
        statuses=status,
        entity=entity,
        entity_type=entity_type,
        entity_id=entity_id,
        product_source_id=source_product_id,
        warehouse_source_id=warehouse_id,
        detected_from=_normalize_filter(detected_from),
        detected_to=_normalize_filter(detected_to),
    )
    return Page(
        items=[_exception_out(row) for row in rows], page=page, page_size=page_size, total=total
    )


@router.get("/exceptions/{exception_id}", response_model=ExceptionOut)
def exception_detail(exception_id: int, session: SessionDependency) -> ExceptionOut:
    row = queries.get_exception(session, exception_id)
    if row is None:
        raise HTTPException(status_code=404, detail="exception not found")
    return _exception_out(row, include_history=True)


@router.patch("/exceptions/{exception_id}/status", response_model=ExceptionOut)
def exception_status(
    exception_id: int,
    payload: ExceptionStatusPatch,
    session: SessionDependency,
    settings: SettingsDependency,
) -> ExceptionOut:
    if settings.public_demo_read_only:
        raise HTTPException(status_code=403, detail="public demo is read-only")
    try:
        row = transition_exception(
            session,
            exception_id,
            payload.status,
            actor=payload.actor,
            reason=payload.reason,
        )
        session.commit()
    except LookupError:
        session.rollback()
        raise HTTPException(status_code=404, detail="exception not found")
    except InvalidTransition:
        session.rollback()
        raise HTTPException(status_code=409, detail="invalid exception lifecycle transition")
    except ValueError:
        session.rollback()
        raise HTTPException(status_code=422, detail="invalid lifecycle request")
    return _exception_out(queries.get_exception(session, row.id) or row, include_history=True)


@router.get("/kpis/summary", response_model=KpiSummaryOut)
def kpi_summary(
    session: SessionDependency,
    settings: SettingsDependency,
    as_of: datetime | None = None,
) -> KpiSummaryOut:
    effective_as_of = _normalize_filter(as_of) if as_of is not None else settings.as_of
    return KpiSummaryOut(**KPIService(session).summary(effective_as_of))
