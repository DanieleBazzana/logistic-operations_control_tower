"""Deterministic operational KPI aggregation; this module never runs detection."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from control_tower.config import Settings
from control_tower.enums import ExceptionSeverity, ExceptionStatus, ExceptionType, OrderStatus
from control_tower.exceptions.contracts import normalize_as_of
from control_tower.models import ExceptionRecord, Order

ACTIVE_EXCEPTION_STATUSES = (
    ExceptionStatus.OPEN,
    ExceptionStatus.ACKNOWLEDGED,
    ExceptionStatus.IN_PROGRESS,
)


class KPIService:
    """Calculate a point-in-time summary from persisted rows only."""

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or Settings()

    def summary(
        self,
        as_of: datetime | None = None,
        *,
        dataset_version_id: int | None = None,
    ) -> dict[str, int | str | None]:
        instant = normalize_as_of(as_of or self.settings.as_of)
        order_before = Order.ordered_at <= instant
        order_scope = (
            (Order.dataset_version_id == dataset_version_id,)
            if dataset_version_id is not None
            else ()
        )
        orders_processed = (
            self.session.scalar(
                select(func.count()).select_from(Order).where(order_before, *order_scope)
            )
            or 0
        )
        open_orders = (
            self.session.scalar(
                select(func.count())
                .select_from(Order)
                .where(order_before, Order.status == OrderStatus.OPEN, *order_scope)
            )
            or 0
        )
        fulfilled_orders = (
            self.session.scalar(
                select(func.count())
                .select_from(Order)
                .where(
                    order_before,
                    Order.status == OrderStatus.FULFILLED,
                    Order.fulfilled_at <= instant,
                    *order_scope,
                )
            )
            or 0
        )
        cancelled_orders = (
            self.session.scalar(
                select(func.count())
                .select_from(Order)
                .where(order_before, Order.status == OrderStatus.CANCELLED, *order_scope)
            )
            or 0
        )
        on_time_orders = (
            self.session.scalar(
                select(func.count())
                .select_from(Order)
                .where(
                    order_before,
                    Order.status == OrderStatus.FULFILLED,
                    Order.fulfilled_at <= instant,
                    Order.fulfilled_at.is_not(None),
                    Order.fulfilled_at <= Order.promised_at,
                    *order_scope,
                )
            )
            or 0
        )
        sla_percent = (
            (Decimal(on_time_orders) * Decimal("100") / Decimal(fulfilled_orders)).quantize(
                Decimal("0.01")
            )
            if fulfilled_orders
            else None
        )
        active = ExceptionRecord.status.in_(ACTIVE_EXCEPTION_STATUSES)
        detected_before = ExceptionRecord.detected_at <= instant
        exception_scope = (
            (ExceptionRecord.dataset_version_id == dataset_version_id,)
            if dataset_version_id is not None
            else ()
        )
        open_exceptions = (
            self.session.scalar(
                select(func.count())
                .select_from(ExceptionRecord)
                .where(active, detected_before, *exception_scope)
            )
            or 0
        )
        critical_exceptions = (
            self.session.scalar(
                select(func.count())
                .select_from(ExceptionRecord)
                .where(
                    active,
                    detected_before,
                    ExceptionRecord.severity == ExceptionSeverity.CRITICAL,
                    *exception_scope,
                )
            )
            or 0
        )
        revenue = self.session.scalar(
            select(func.coalesce(func.sum(ExceptionRecord.revenue_at_risk), 0)).where(
                active, detected_before, *exception_scope
            )
        )
        values: dict[str, int | str | None] = {
            "as_of": instant.isoformat().replace("+00:00", "Z"),
            "orders_processed": int(orders_processed),
            "open_orders": int(open_orders),
            "fulfilled_orders": int(fulfilled_orders),
            "cancelled_orders": int(cancelled_orders),
            "sla_performance_pct": (f"{sla_percent:.2f}" if sla_percent is not None else None),
            "open_exceptions": int(open_exceptions),
            "critical_exceptions": int(critical_exceptions),
            "revenue_at_risk": f"{Decimal(revenue or 0):.2f}",
            "stockout_risks": self._count_type(
                ExceptionType.STOCKOUT_RISK, active, detected_before, exception_scope
            ),
            "supplier_delays": self._count_type(
                ExceptionType.SUPPLIER_DELAY, active, detected_before, exception_scope
            ),
            "shipment_delays": self._count_type(
                ExceptionType.SHIPMENT_DELAY, active, detected_before, exception_scope
            ),
        }
        return values

    def _count_type(
        self,
        exception_type: ExceptionType,
        active,
        detected_before,
        exception_scope=(),
    ) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(ExceptionRecord)
                .where(
                    active,
                    detected_before,
                    ExceptionRecord.exception_type == exception_type,
                    *exception_scope,
                )
            )
            or 0
        )


# A concise alias is useful for callers that prefer functional naming.
def summarize_kpis(
    session: Session,
    as_of: datetime | None = None,
    *,
    dataset_version_id: int | None = None,
) -> dict[str, int | str | None]:
    return KPIService(session).summary(as_of, dataset_version_id=dataset_version_id)
