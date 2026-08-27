"""Configurable, deterministic exception severity calculation."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from control_tower.config import Settings
from control_tower.enums import ExceptionSeverity

if TYPE_CHECKING:
    from control_tower.exceptions.contracts import ExceptionDetection


def _hours(value: Decimal | int | float | timedelta | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, timedelta):
        return Decimal(str(value.total_seconds())) / Decimal("3600")
    return Decimal(str(value))


def severity_for_metrics(
    settings: Settings,
    *,
    revenue_at_risk: Decimal | None = None,
    orders_affected: int | None = None,
    overdue_hours: Decimal | int | float | timedelta | None = None,
) -> ExceptionSeverity:
    """Choose the highest rank reached by any available configured metric.

    Thresholds are inclusive. A metric that is absent is ignored; if no metric
    reaches the MEDIUM threshold, the deterministic fallback is LOW.
    """

    revenue = None if revenue_at_risk is None else Decimal(str(revenue_at_risk))
    orders = None if orders_affected is None else int(orders_affected)
    overdue = _hours(overdue_hours)
    checks = (
        (
            revenue,
            settings.severity_critical_revenue_at_risk,
            settings.severity_high_revenue_at_risk,
            settings.severity_medium_revenue_at_risk,
        ),
        (
            orders,
            settings.severity_critical_orders_affected,
            settings.severity_high_orders_affected,
            settings.severity_medium_orders_affected,
        ),
        (
            overdue,
            settings.severity_critical_overdue_hours,
            settings.severity_high_overdue_hours,
            settings.severity_medium_overdue_hours,
        ),
    )
    for severity, index in (
        (ExceptionSeverity.CRITICAL, 0),
        (ExceptionSeverity.HIGH, 1),
        (ExceptionSeverity.MEDIUM, 2),
    ):
        if any(value is not None and value >= metric[index] for value, *metric in checks):
            return severity
    return ExceptionSeverity.LOW


def severity_for_detection(detection: ExceptionDetection, settings: Settings) -> ExceptionSeverity:
    """Calculate severity from the metrics carried by a detection DTO."""

    return severity_for_metrics(
        settings,
        revenue_at_risk=detection.revenue_at_risk,
        orders_affected=detection.orders_affected,
        overdue_hours=detection.overdue_hours,
    )


__all__ = ["severity_for_detection", "severity_for_metrics"]
