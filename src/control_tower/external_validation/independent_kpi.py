"""Independent, source-level KPI calculations for cross-checking only.

This module deliberately does not import the API, KPI service, detection rules, or ORM.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


def _value(row: dict[str, Any], *names: str) -> Any:
    normalized: dict[str, Any] = {}
    for key, value in row.items():
        normalized.setdefault(_header_name(key), value)
    for name in names:
        value = normalized.get(_header_name(name))
        if value not in (None, ""):
            return value
    return None


def _header_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value)).strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def _instant(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in (None, "%m/%d/%Y %H:%M", "%m/%d/%Y"):
        try:
            parsed = datetime.fromisoformat(text) if fmt is None else datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _order_level_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the adapter's first source row for each identifiable order."""

    seen: set[str] = set()
    order_rows: list[dict[str, Any]] = []
    for row in rows:
        order_id = _value(row, "order_id", "Order Id")
        if order_id in (None, ""):
            continue
        key = str(order_id).strip()
        if key not in seen:
            seen.add(key)
            order_rows.append(row)
    return order_rows


def _money(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).strip())
    except Exception:  # external CSV values are untrusted; KPI remains unavailable on bad data
        return None


def calculate_independent_kpis(
    dataset: str, tables: dict[str, list[dict[str, Any]]], *, as_of: datetime
) -> dict[str, Any]:
    """Calculate order-side metrics without calling the application KPI path."""

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must include a timezone")
    instant = as_of.astimezone(timezone.utc)
    rows = _order_level_rows(tables.get("orders", []))
    if dataset not in {"olist", "dataco"}:
        raise ValueError(f"unsupported dataset: {dataset}")
    processed = [
        row
        for row in rows
        if (
            (
                order_date := _instant(
                    _value(row, "order_purchase_timestamp", "Order Date (DateOrders)")
                )
            )
            is not None
            and order_date <= instant
        )
    ]
    statuses = []
    for row in processed:
        raw = str(_value(row, "order_status", "Order Status") or "").strip().lower()
        delivery = str(_value(row, "Delivery Status") or "").lower()
        if dataset == "olist":
            status = (
                "FULFILLED"
                if raw == "delivered"
                else "CANCELLED"
                if raw in {"canceled", "cancelled"}
                else "OPEN"
            )
        else:
            status = (
                "CANCELLED"
                if "cancel" in raw
                else (
                    "FULFILLED"
                    if raw in {"complete", "completed"} or "delivered" in delivery
                    else "OPEN"
                )
            )
        statuses.append((row, status))
    fulfilled = [
        row
        for row, status in statuses
        if status == "FULFILLED"
        and (fulfilled_at := _instant(_value(row, "order_delivered_customer_date"))) is not None
        and fulfilled_at <= instant
    ]
    sla_observations: list[bool] = []
    for row in fulfilled:
        delivered = _instant(_value(row, "order_delivered_customer_date"))
        promised = _instant(_value(row, "order_estimated_delivery_date"))
        if delivered is not None and delivered <= instant and promised is not None:
            sla_observations.append(delivered <= promised)
    sla = (
        (Decimal(sum(sla_observations) * 100) / Decimal(len(sla_observations))).quantize(
            Decimal("0.01")
        )
        if sla_observations
        else None
    )
    return {
        "as_of": instant.isoformat().replace("+00:00", "Z"),
        "orders_processed": len(processed),
        "open_orders": sum(status == "OPEN" for _, status in statuses),
        "fulfilled_orders": len(fulfilled),
        "cancelled_orders": sum(status == "CANCELLED" for _, status in statuses),
        "sla_performance_pct": sla,
        "open_exceptions": None,
        "critical_exceptions": None,
        "revenue_at_risk": None,
        "stockout_risks": None,
        "supplier_delays": None,
        "shipment_delays": None,
        "unavailable": [
            "exceptions",
            "severity",
            "revenue_at_risk",
            "inventory",
            "supplier",
            "carrier",
        ],
        "formula_source": "external_validation.independent_kpi",
    }


__all__ = ["calculate_independent_kpis"]
