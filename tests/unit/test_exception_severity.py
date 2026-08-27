from decimal import Decimal

import pytest
from pydantic import ValidationError

from control_tower.config import Settings
from control_tower.enums import ExceptionSeverity
from control_tower.exceptions.severity import severity_for_metrics


def test_severity_uses_highest_rank_across_inclusive_metrics() -> None:
    settings = Settings(
        severity_critical_revenue_at_risk=Decimal("1000"),
        severity_high_revenue_at_risk=Decimal("500"),
        severity_medium_revenue_at_risk=Decimal("100"),
        severity_critical_orders_affected=50,
        severity_high_orders_affected=20,
        severity_medium_orders_affected=5,
        severity_critical_overdue_hours=48,
        severity_high_overdue_hours=24,
        severity_medium_overdue_hours=4,
    )

    assert (
        severity_for_metrics(
            settings,
            revenue_at_risk=Decimal("100"),
            orders_affected=20,
            overdue_hours=0,
        )
        == ExceptionSeverity.HIGH
    )
    assert (
        severity_for_metrics(
            settings,
            revenue_at_risk=Decimal("0"),
            orders_affected=0,
            overdue_hours=48,
        )
        == ExceptionSeverity.CRITICAL
    )


@pytest.mark.parametrize(
    ("metric", "medium", "high", "critical"),
    [
        ("revenue_at_risk", Decimal("100"), Decimal("500"), Decimal("1000")),
        ("orders_affected", 5, 20, 50),
        ("overdue_hours", 4, 24, 48),
    ],
)
def test_each_severity_metric_has_inclusive_boundaries(
    metric: str, medium: Decimal | int, high: Decimal | int, critical: Decimal | int
) -> None:
    threshold_values: dict[str, object] = {
        "severity_medium_revenue_at_risk": Decimal("100000"),
        "severity_high_revenue_at_risk": Decimal("100000"),
        "severity_critical_revenue_at_risk": Decimal("100000"),
        "severity_medium_orders_affected": 1000,
        "severity_high_orders_affected": 1000,
        "severity_critical_orders_affected": 1000,
        "severity_medium_overdue_hours": 1000,
        "severity_high_overdue_hours": 1000,
        "severity_critical_overdue_hours": 1000,
    }
    if metric == "revenue_at_risk":
        threshold_values.update(
            severity_medium_revenue_at_risk=medium,
            severity_high_revenue_at_risk=high,
            severity_critical_revenue_at_risk=critical,
        )
    elif metric == "orders_affected":
        threshold_values.update(
            severity_medium_orders_affected=medium,
            severity_high_orders_affected=high,
            severity_critical_orders_affected=critical,
        )
    else:
        threshold_values.update(
            severity_medium_overdue_hours=medium,
            severity_high_overdue_hours=high,
            severity_critical_overdue_hours=critical,
        )
    settings = Settings.model_validate(threshold_values)

    def score(value: Decimal | int) -> ExceptionSeverity:
        if metric == "revenue_at_risk":
            return severity_for_metrics(settings, revenue_at_risk=Decimal(value))
        if metric == "orders_affected":
            return severity_for_metrics(settings, orders_affected=int(value))
        return severity_for_metrics(settings, overdue_hours=value)

    assert score(medium) == ExceptionSeverity.MEDIUM
    assert score(high) == ExceptionSeverity.HIGH
    assert score(critical) == ExceptionSeverity.CRITICAL


@pytest.mark.parametrize(
    ("field", "values"),
    [
        (
            "revenue_at_risk",
            {"severity_medium_revenue_at_risk": 2, "severity_high_revenue_at_risk": 1},
        ),
        (
            "orders_affected",
            {"severity_medium_orders_affected": 2, "severity_high_orders_affected": 1},
        ),
        (
            "overdue_hours",
            {"severity_medium_overdue_hours": 2, "severity_high_overdue_hours": 1},
        ),
    ],
)
def test_settings_rejects_invalid_ordering_for_every_severity_dimension(
    field: str, values: dict[str, object]
) -> None:
    with pytest.raises(ValidationError, match=field):
        Settings.model_validate(values)


def test_severity_falls_back_to_low_when_no_metric_reaches_medium() -> None:
    settings = Settings()

    assert (
        severity_for_metrics(
            settings,
            revenue_at_risk=Decimal("0"),
            orders_affected=0,
            overdue_hours=0,
        )
        == ExceptionSeverity.LOW
    )


def test_settings_rejects_descending_threshold_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEVERITY_MEDIUM_REVENUE_AT_RISK", "1000")
    monkeypatch.setenv("SEVERITY_HIGH_REVENUE_AT_RISK", "500")
    monkeypatch.setenv("SEVERITY_CRITICAL_REVENUE_AT_RISK", "400")

    with pytest.raises(ValidationError, match="medium.*high.*critical"):
        Settings()
