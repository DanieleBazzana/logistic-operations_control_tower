"""M04 PostgreSQL HTTP contract gate against an explicitly disposable URL."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from control_tower.api.app import create_app
from control_tower.config import Settings, set_alembic_database_url
from control_tower.db import create_db_engine
from control_tower.enums import ExceptionSeverity, ExceptionStatus, ExceptionType, OrderStatus
from control_tower.exceptions.service import ExceptionService
from control_tower.ingestion.loader import ingest
from control_tower.models import ExceptionRecord, Order
from control_tower.synthetic.generator import generate

AS_OF = datetime(2025, 1, 15, 12, tzinfo=timezone.utc)


@pytest.mark.integration
@pytest.mark.usefixtures("reset_disposable_postgres_database")
@pytest.mark.anyio
async def test_m04_postgres_http_contract_and_kpis(tmp_path: Path) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not set; provide a disposable PostgreSQL database")
    if not database_url.startswith("postgresql"):
        pytest.fail("TEST_DATABASE_URL must be a PostgreSQL URL")

    bundle = tmp_path / "bundle"
    generate(bundle, seed=19, as_of=AS_OF, product_count=8, order_count=20)
    settings = Settings(
        database_url=database_url,
        as_of=AS_OF,
        safety_stock=Decimal("20"),
        inventory_mismatch_tolerance=Decimal("1"),
    )
    engine = create_db_engine(settings)
    alembic_config = Config("alembic.ini")
    set_alembic_database_url(alembic_config, database_url)
    try:
        command.upgrade(alembic_config, "head")
        ingestion = ingest(bundle, engine=engine)
        assert ingestion.committed
        with Session(engine) as session:
            detection = ExceptionService(session, settings).detect(AS_OF)
            session.commit()
            assert {finding.exception_type for finding in detection.detections} == set(
                ExceptionType
            )
            assert detection.created == len(detection.detections)
            active_id = session.scalar(
                select(ExceptionRecord.id)
                .where(
                    ExceptionRecord.status.in_(
                        (
                            ExceptionStatus.OPEN,
                            ExceptionStatus.ACKNOWLEDGED,
                            ExceptionStatus.IN_PROGRESS,
                        )
                    )
                )
                .order_by(ExceptionRecord.id)
            )
            assert active_id is not None

            expected_orders = (
                session.scalar(
                    select(func.count()).select_from(Order).where(Order.ordered_at <= AS_OF)
                )
                or 0
            )
            expected_open = (
                session.scalar(
                    select(func.count())
                    .select_from(Order)
                    .where(Order.ordered_at <= AS_OF, Order.status == OrderStatus.OPEN)
                )
                or 0
            )
            expected_fulfilled = (
                session.scalar(
                    select(func.count())
                    .select_from(Order)
                    .where(
                        Order.ordered_at <= AS_OF,
                        Order.status == OrderStatus.FULFILLED,
                        Order.fulfilled_at <= AS_OF,
                    )
                )
                or 0
            )
            expected_cancelled = (
                session.scalar(
                    select(func.count())
                    .select_from(Order)
                    .where(Order.ordered_at <= AS_OF, Order.status == OrderStatus.CANCELLED)
                )
                or 0
            )
            expected_on_time = (
                session.scalar(
                    select(func.count())
                    .select_from(Order)
                    .where(
                        Order.ordered_at <= AS_OF,
                        Order.status == OrderStatus.FULFILLED,
                        Order.fulfilled_at <= AS_OF,
                        Order.fulfilled_at <= Order.promised_at,
                    )
                )
                or 0
            )
            expected_open_exceptions = (
                session.scalar(
                    select(func.count())
                    .select_from(ExceptionRecord)
                    .where(
                        ExceptionRecord.detected_at <= AS_OF,
                        ExceptionRecord.status.in_(
                            (
                                ExceptionStatus.OPEN,
                                ExceptionStatus.ACKNOWLEDGED,
                                ExceptionStatus.IN_PROGRESS,
                            )
                        ),
                    )
                )
                or 0
            )
            expected_critical = (
                session.scalar(
                    select(func.count())
                    .select_from(ExceptionRecord)
                    .where(
                        ExceptionRecord.detected_at <= AS_OF,
                        ExceptionRecord.status.in_(
                            (
                                ExceptionStatus.OPEN,
                                ExceptionStatus.ACKNOWLEDGED,
                                ExceptionStatus.IN_PROGRESS,
                            )
                        ),
                        ExceptionRecord.severity == ExceptionSeverity.CRITICAL,
                    )
                )
                or 0
            )
            expected_stockout = (
                session.scalar(
                    select(func.count())
                    .select_from(ExceptionRecord)
                    .where(
                        ExceptionRecord.detected_at <= AS_OF,
                        ExceptionRecord.status.in_(
                            (
                                ExceptionStatus.OPEN,
                                ExceptionStatus.ACKNOWLEDGED,
                                ExceptionStatus.IN_PROGRESS,
                            )
                        ),
                        ExceptionRecord.exception_type == ExceptionType.STOCKOUT_RISK,
                    )
                )
                or 0
            )
            expected_supplier_delays = (
                session.scalar(
                    select(func.count())
                    .select_from(ExceptionRecord)
                    .where(
                        ExceptionRecord.detected_at <= AS_OF,
                        ExceptionRecord.status.in_(
                            (
                                ExceptionStatus.OPEN,
                                ExceptionStatus.ACKNOWLEDGED,
                                ExceptionStatus.IN_PROGRESS,
                            )
                        ),
                        ExceptionRecord.exception_type == ExceptionType.SUPPLIER_DELAY,
                    )
                )
                or 0
            )
            expected_shipment_delays = (
                session.scalar(
                    select(func.count())
                    .select_from(ExceptionRecord)
                    .where(
                        ExceptionRecord.detected_at <= AS_OF,
                        ExceptionRecord.status.in_(
                            (
                                ExceptionStatus.OPEN,
                                ExceptionStatus.ACKNOWLEDGED,
                                ExceptionStatus.IN_PROGRESS,
                            )
                        ),
                        ExceptionRecord.exception_type == ExceptionType.SHIPMENT_DELAY,
                    )
                )
                or 0
            )
            expected_revenue = (
                session.scalar(
                    select(func.coalesce(func.sum(ExceptionRecord.revenue_at_risk), 0)).where(
                        ExceptionRecord.detected_at <= AS_OF,
                        ExceptionRecord.status.in_(
                            (
                                ExceptionStatus.OPEN,
                                ExceptionStatus.ACKNOWLEDGED,
                                ExceptionStatus.IN_PROGRESS,
                            )
                        ),
                    )
                )
                or 0
            )

        client = AsyncClient(
            transport=ASGITransport(app=create_app(engine=engine, settings=settings)),
            base_url="http://test",
        )
        async with client:
            health = await client.get("/api/v1/health")
            orders = await client.get("/api/v1/orders", params={"page": 1, "page_size": 2})
            order_detail = await client.get("/api/v1/orders/ O000001".replace(" ", ""))
            inventory = await client.get("/api/v1/inventory", params={"sku": "SKU-0001"})
            purchase_orders = await client.get(
                "/api/v1/purchase-orders", params={"remaining_min": "1"}
            )
            shipments = await client.get("/api/v1/shipments", params={"warehouse_id": "W001"})
            exceptions = await client.get("/api/v1/exceptions", params={"page_size": 1})
            exception_detail = await client.get(f"/api/v1/exceptions/{active_id}")
            kpi = await client.get("/api/v1/kpis/summary", params={"as_of": AS_OF.isoformat()})
            kpi_repeat = await client.get(
                "/api/v1/kpis/summary", params={"as_of": AS_OF.isoformat()}
            )

            acknowledged = await client.patch(
                f"/api/v1/exceptions/{active_id}/status",
                json={"status": "ACKNOWLEDGED", "actor": " integration-test "},
            )
            in_progress = await client.patch(
                f"/api/v1/exceptions/{active_id}/status",
                json={"status": "IN_PROGRESS", "actor": "integration-test"},
            )
            resolved = await client.patch(
                f"/api/v1/exceptions/{active_id}/status",
                json={
                    "status": "RESOLVED",
                    "actor": "integration-test",
                    "reason": " integration fixture resolved ",
                },
            )
            invalid = await client.patch(
                f"/api/v1/exceptions/{active_id}/status",
                json={"status": "OPEN", "actor": "integration-test"},
            )
            after_invalid = await client.get(f"/api/v1/exceptions/{active_id}")

        assert health.status_code == 200 and health.json() == {"status": "ok"}
        assert orders.status_code == 200 and orders.json()["total"] >= 2
        assert order_detail.status_code == 200
        assert inventory.status_code == 200 and inventory.json()["total"] >= 1
        assert purchase_orders.status_code == 200 and purchase_orders.json()["total"] >= 1
        assert shipments.status_code == 200 and shipments.json()["total"] >= 1
        assert exceptions.status_code == 200 and exceptions.json()["total"] >= len(
            detection.detections
        )
        assert exception_detail.status_code == 200
        assert kpi.status_code == kpi_repeat.status_code == 200
        assert kpi.json() == kpi_repeat.json()
        body = kpi.json()
        assert body["as_of"] == "2025-01-15T12:00:00Z"
        assert body["orders_processed"] == expected_orders
        assert body["open_orders"] == expected_open
        assert body["fulfilled_orders"] == expected_fulfilled
        assert body["cancelled_orders"] == expected_cancelled
        expected_sla = (
            (Decimal(expected_on_time) * 100 / Decimal(expected_fulfilled))
            if expected_fulfilled
            else None
        )
        assert body["sla_performance_pct"] == (
            f"{expected_sla:.2f}" if expected_sla is not None else None
        )
        assert body["open_exceptions"] == expected_open_exceptions
        assert body["critical_exceptions"] == expected_critical
        assert body["revenue_at_risk"] == f"{Decimal(expected_revenue):.2f}"
        assert body["stockout_risks"] == expected_stockout
        assert body["supplier_delays"] == expected_supplier_delays
        assert body["shipment_delays"] == expected_shipment_delays
        assert acknowledged.status_code == in_progress.status_code == resolved.status_code == 200
        assert acknowledged.json()["history"] is not None
        assert invalid.status_code == 409
        assert after_invalid.json()["status"] == "RESOLVED"
    finally:
        engine.dispose()
