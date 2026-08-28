from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from control_tower.api.app import create_app
from control_tower.db import Base
from control_tower.enums import ExceptionSeverity, ExceptionStatus, ExceptionType
from control_tower.models import (
    ExceptionHistory,
    ExceptionRecord,
    Inventory,
    Order,
    OrderItem,
    Product,
    PurchaseOrder,
    PurchaseOrderItem,
    Shipment,
    Supplier,
    Warehouse,
)

AS_OF = datetime(2025, 1, 15, 12, tzinfo=timezone.utc)


@event.listens_for(ExceptionHistory, "before_insert")
def _sqlite_history_identity(mapper, connection, target) -> None:
    if target.id is None and connection.dialect.name == "sqlite":
        target.id = connection.scalar(select(func.max(ExceptionHistory.id))) or 0
        target.id += 1


@pytest.fixture
def api_client():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        warehouse = Warehouse(id=1, source_warehouse_id="W1", code="W1", name="Main", region="EU")
        product = Product(
            id=1, source_product_id="P1", sku="SKU1", name="Widget", unit_price=Decimal("12.50")
        )
        supplier = Supplier(id=1, source_supplier_id="SUP1", code="SUP1", name="Acme", region="EU")
        session.add_all([warehouse, product, supplier])
        session.flush()
        session.add_all(
            [
                Order(
                    id=1,
                    source_order_id="O2",
                    order_number="1002",
                    status="FULFILLED",
                    region="EU",
                    warehouse_id=1,
                    ordered_at=AS_OF - timedelta(days=2),
                    promised_at=AS_OF - timedelta(days=1),
                    fulfilled_at=AS_OF - timedelta(hours=2),
                    total_amount=Decimal("20.00"),
                ),
                Order(
                    id=2,
                    source_order_id="O1",
                    order_number="1001",
                    status="OPEN",
                    region="US",
                    warehouse_id=1,
                    ordered_at=AS_OF - timedelta(days=1),
                    promised_at=AS_OF + timedelta(days=1),
                    total_amount=Decimal("30.00"),
                ),
                Inventory(
                    id=1,
                    product_id=1,
                    warehouse_id=1,
                    on_hand=Decimal("8.125"),
                    reserved=Decimal("2.125"),
                    observed_at=AS_OF,
                ),
                PurchaseOrder(
                    id=1,
                    source_purchase_order_id="PO1",
                    po_number="PO-1",
                    supplier_id=1,
                    warehouse_id=1,
                    status="OPEN",
                    ordered_at=AS_OF - timedelta(days=5),
                    expected_delivery_at=AS_OF + timedelta(days=2),
                ),
                OrderItem(
                    id=1,
                    source_order_item_id="O1-01",
                    order_id=2,
                    product_id=1,
                    line_number=1,
                    ordered_quantity=Decimal("5.125"),
                    fulfilled_quantity=Decimal("2.125"),
                    unit_price=Decimal("12.50"),
                ),
                PurchaseOrderItem(
                    id=1,
                    source_purchase_order_item_id="PO1-01",
                    purchase_order_id=1,
                    product_id=1,
                    ordered_quantity=Decimal("10.125"),
                    received_quantity=Decimal("3.125"),
                    unit_cost=Decimal("8.00"),
                ),
                Shipment(
                    id=1,
                    source_shipment_id="S1",
                    order_id=1,
                    carrier="Carrier",
                    tracking_id="TRACK-1",
                    status="IN_TRANSIT",
                    eta=AS_OF + timedelta(days=1),
                ),
                ExceptionRecord(
                    id=1,
                    deduplication_key="key-1",
                    exception_type=ExceptionType.SLA_BREACH_RISK,
                    issue_key="O2",
                    entity_type="order",
                    entity_id="O2",
                    severity=ExceptionSeverity.HIGH,
                    status=ExceptionStatus.OPEN,
                    detected_at=AS_OF,
                    business_impact="Late order",
                    revenue_at_risk=Decimal("20.00"),
                    orders_affected=1,
                    root_cause="Delay",
                    recommended_action="Expedite",
                    confidence=Decimal("0.9000"),
                    warehouse_id=1,
                    product_id=1,
                ),
            ]
        )
        session.commit()
    return AsyncClient(
        transport=ASGITransport(app=create_app(engine=engine)),
        base_url="http://test",
    )


@pytest.mark.anyio
async def test_orders_are_paginated_stably_and_filter_exact_status(api_client):
    async with api_client as client:
        response = await client.get("/api/v1/orders", params={"status": "OPEN"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["source_order_id"] == "O1"
    assert body["items"][0]["total_amount"] == "30.00"


@pytest.mark.anyio
async def test_collections_support_or_status_filters_inclusive_bounds_and_validation(api_client):
    async with api_client as client:
        response = await client.get(
            "/api/v1/orders",
            params=[
                ("status", "OPEN"),
                ("status", "FULFILLED"),
                ("ordered_from", (AS_OF - timedelta(days=1)).isoformat()),
                ("ordered_to", (AS_OF - timedelta(days=1)).isoformat()),
                ("page_size", "1"),
            ],
        )
        too_large = await client.get("/api/v1/orders", params={"page_size": 101})
        too_small = await client.get("/api/v1/orders", params={"page": 0})

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["source_order_id"] == "O1"
    assert too_large.status_code == too_small.status_code == 422


@pytest.mark.anyio
async def test_read_collections_and_source_identifier_details_are_available(api_client):
    async with api_client as client:
        inventory = await client.get("/api/v1/inventory")
        purchase_orders = await client.get("/api/v1/purchase-orders")
        shipments = await client.get("/api/v1/shipments")
        order = await client.get("/api/v1/orders/O1")
        missing = await client.get("/api/v1/orders/missing")

    assert all(
        result.status_code == 200 for result in (inventory, purchase_orders, shipments, order)
    )
    assert inventory.json()["items"][0]["available"] == "6.000"
    assert purchase_orders.json()["items"][0]["source_purchase_order_id"] == "PO1"
    assert shipments.json()["items"][0]["source_shipment_id"] == "S1"
    assert order.json()["source_order_id"] == "O1"
    assert order.json()["items"] == [
        {
            "source_product_id": "P1",
            "sku": "SKU1",
            "ordered_quantity": "5.125",
            "fulfilled_quantity": "2.125",
            "unit_price": "12.50",
        }
    ]
    assert order.json()["shipments"] == []
    assert purchase_orders.json()["items"][0]["remaining_quantity"] == "7.000"
    assert missing.status_code == 404


@pytest.mark.anyio
async def test_exception_status_patch_uses_lifecycle_and_returns_safe_conflict(api_client):
    async with api_client as client:
        acknowledged = await client.patch(
            "/api/v1/exceptions/1/status",
            json={"status": "ACKNOWLEDGED", "actor": "operator"},
        )
        conflict = await client.patch(
            "/api/v1/exceptions/1/status",
            json={"status": "OPEN", "actor": "operator"},
        )
        detail = await client.get("/api/v1/exceptions/1")
        missing = await client.patch(
            "/api/v1/exceptions/999/status",
            json={"status": "ACKNOWLEDGED", "actor": "operator"},
        )

    assert acknowledged.status_code == 200
    assert acknowledged.json()["status"] == "ACKNOWLEDGED"
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": "invalid exception lifecycle transition"}
    assert detail.status_code == 200
    assert detail.json()["history"][0]["to_status"] == "ACKNOWLEDGED"
    assert missing.status_code == 404


@pytest.mark.anyio
async def test_kpi_summary_is_deterministic_and_revenue_is_finding_level(api_client):
    async with api_client as client:
        first = await client.get("/api/v1/kpis/summary")
        second = await client.get("/api/v1/kpis/summary")

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["revenue_at_risk"] == "20.00"
    assert first.json()["orders_processed"] == 2
    assert first.json()["open_orders"] == 1
    assert first.json()["fulfilled_orders"] == 1
    assert first.json()["cancelled_orders"] == 0
    assert first.json()["sla_performance_pct"] == "0.00"
    assert first.json()["as_of"] == "2025-01-15T12:00:00Z"


@pytest.mark.anyio
async def test_collection_filters_cover_inventory_po_shipments_and_exceptions(api_client):
    async with api_client as client:
        inventory = await client.get(
            "/api/v1/inventory",
            params={
                "sku": "SKU1",
                "available_max": "6",
                "observed_from": AS_OF.isoformat(),
                "observed_to": AS_OF.isoformat(),
            },
        )
        purchase_orders = await client.get(
            "/api/v1/purchase-orders",
            params={
                "supplier_id": "SUP1",
                "warehouse_id": "W1",
                "ordered_from": (AS_OF - timedelta(days=5)).isoformat(),
                "ordered_to": (AS_OF - timedelta(days=5)).isoformat(),
                "remaining_min": "7",
                "remaining_max": "7",
            },
        )
        shipments = await client.get("/api/v1/shipments", params={"warehouse_id": "W1"})
        exceptions = await client.get(
            "/api/v1/exceptions",
            params={
                "entity_type": "order",
                "entity_id": "O2",
                "source_product_id": "P1",
                "type": "SLA_BREACH_RISK",
                "severity": "HIGH",
                "status": "OPEN",
                "warehouse_id": "W1",
                "detected_from": AS_OF.isoformat(),
                "detected_to": AS_OF.isoformat(),
            },
        )
        paged = await client.get("/api/v1/orders", params={"page": 2, "page_size": 1})

    assert inventory.status_code == purchase_orders.status_code == 200
    assert inventory.json()["total"] == 1
    assert purchase_orders.json()["total"] == 1
    assert shipments.status_code == 200 and shipments.json()["total"] == 1
    assert exceptions.status_code == 200 and exceptions.json()["total"] == 1
    assert paged.status_code == 200
    assert paged.json()["items"][0]["source_order_id"] == "O2"


@pytest.mark.anyio
async def test_kpi_as_of_is_timezone_aware_bounded_and_normalized(api_client):
    async with api_client as client:
        before = await client.get(
            "/api/v1/kpis/summary", params={"as_of": "2025-01-15T12:00:00+02:00"}
        )
        naive = await client.get("/api/v1/kpis/summary", params={"as_of": "2025-01-15T12:00:00"})

    assert before.status_code == 200
    assert before.json()["as_of"] == "2025-01-15T10:00:00Z"
    assert before.json()["orders_processed"] == 2
    assert naive.status_code == 422


@pytest.mark.anyio
async def test_status_patch_trims_values_and_rejects_validation_without_sql_leak(api_client):
    async with api_client as client:
        response = await client.patch(
            "/api/v1/exceptions/1/status",
            json={"status": "ACKNOWLEDGED", "actor": "  operator  ", "reason": "  note  "},
        )
        blank = await client.patch(
            "/api/v1/exceptions/1/status", json={"status": "IN_PROGRESS", "actor": "   "}
        )
        too_long = await client.patch(
            "/api/v1/exceptions/1/status", json={"status": "IN_PROGRESS", "actor": "x" * 101}
        )
        detail = await client.get("/api/v1/exceptions/1")

    assert response.status_code == 200
    assert blank.status_code == too_long.status_code == 422
    assert detail.json()["history"][0]["actor"] == "operator"
    assert detail.json()["history"][0]["transition_reason"] == "note"


@pytest.mark.anyio
async def test_health_reports_database_failure_without_leaking_details():
    class BrokenEngine:
        def connect(self):
            raise SQLAlchemyError("password=secret host=internal")

    app = create_app(engine=BrokenEngine())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}
