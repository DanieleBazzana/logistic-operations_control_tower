"""Deterministic, isolated Olist and DataCo source-to-contract adapters."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from itertools import islice
from typing import Any, Iterable

from control_tower.external_validation.contracts import (
    AdapterResult,
    FieldMapping,
    Provenance,
)


def normalize_external_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize headers and trim values without changing row order or meaning."""

    normalized: dict[str, Any] = {}
    for key, value in row.items():
        name = unicodedata.normalize("NFKC", str(key)).strip().lower()
        name = re.sub(r"[^a-z0-9]+", "_", name).strip("_")
        if name in normalized:
            raise ValueError(f"duplicate normalized external column: {name}")
        normalized[name] = value.strip() if isinstance(value, str) else value
    return normalized


def bounded_rows(rows: Iterable[dict[str, Any]], limit: int | None) -> list[dict[str, Any]]:
    """Take the first *limit* rows; a bound is explicit and reproducible."""

    if limit is not None and limit < 0:
        raise ValueError("sample_size must be non-negative")
    bounded = rows if limit is None else islice(rows, limit)
    return [dict(row) for row in bounded]


def _timestamp(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace("Z", "+00:00")
    candidates = (text, text.replace(" ", "T"), f"{text}T00:00:00")
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                # External files do not carry offsets. This assumption is retained as
                # APPROXIMATE in the mapping table; it is never used as a source fact.
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            continue
    for fmt in ("%m/%d/%Y", "%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            return parsed.isoformat()
        except ValueError:
            continue
    return None


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _mapping(
    dataset: str,
    output_field: str,
    source_fields: tuple[str, ...],
    classification: str,
    transformation: str,
    caveat: str = "",
) -> FieldMapping:
    return FieldMapping(
        dataset=dataset,
        artifact="oms/orders.csv",
        output_field=output_field,
        source_fields=source_fields,
        classification=classification,  # type: ignore[arg-type]
        transformation=transformation,
        caveat=caveat,
    )


def _provenance(dataset: str, mapping: FieldMapping) -> Provenance:
    source = f"{dataset}:{'+'.join(mapping.source_fields) or 'unavailable'}"
    return Provenance(
        source=source,
        transformation=mapping.transformation,
        output=f"{mapping.artifact}.{mapping.output_field}",
    )


class OlistAdapter:
    """Adapt only fields that can be traced to Olist observations."""

    dataset = "olist"
    role = "primary"

    def adapt(
        self, tables: dict[str, Iterable[dict[str, Any]]], *, sample_size: int | None = None
    ) -> AdapterResult:
        source_tables = {name: bounded_rows(rows, sample_size) for name, rows in tables.items()}
        source_line_rows = {name: len(rows) for name, rows in source_tables.items()}
        orders = [normalize_external_row(row) for row in source_tables.get("orders", [])]
        payments = [normalize_external_row(row) for row in source_tables.get("payments", [])]
        customers = [normalize_external_row(row) for row in source_tables.get("customers", [])]
        payment_totals: defaultdict[str, Decimal] = defaultdict(Decimal)
        for row in payments:
            amount = _decimal(row.get("payment_value"))
            if amount is not None and row.get("order_id"):
                payment_totals[str(row["order_id"])] += amount
        customer_region = {
            str(row.get("customer_id")): row.get("customer_state") for row in customers
        }
        mappings = self._mappings()
        adapted: list[dict[str, Any]] = []
        for raw in orders:
            order_id = raw.get("order_id")
            status = str(raw.get("order_status") or "").lower()
            adapted.append(
                {
                    "source_order_id": order_id,
                    "order_number": order_id,
                    "status": "FULFILLED"
                    if status == "delivered"
                    else ("CANCELLED" if status in {"canceled", "cancelled"} else "OPEN"),
                    "region": customer_region.get(str(raw.get("customer_id"))),
                    "source_warehouse_id": None,
                    "ordered_at": _timestamp(raw.get("order_purchase_timestamp")),
                    "promised_at": _timestamp(raw.get("order_estimated_delivery_date")),
                    "fulfilled_at": _timestamp(raw.get("order_delivered_customer_date")),
                    "total_amount": (
                        f"{payment_totals[str(order_id)]:.2f}"
                        if str(order_id) in payment_totals
                        else None
                    ),
                    "currency": None,
                }
            )
        unavailable = tuple(
            mapping for mapping in mappings if mapping.classification == "UNAVAILABLE"
        )
        return AdapterResult(
            dataset=self.dataset,
            role=self.role,
            rows_by_artifact={"oms/orders.csv": adapted},
            provenance=tuple(_provenance("Olist", mapping) for mapping in mappings),
            mappings=mappings,
            unavailable=unavailable,
            rows_read=source_line_rows,
            source_line_rows=source_line_rows,
            adapted_orders={"oms/orders.csv": len(adapted)},
        )

    @staticmethod
    def _mappings() -> tuple[FieldMapping, ...]:
        return (
            _mapping("olist", "source_order_id", ("order_id",), "DIRECT", "copy order_id"),
            _mapping(
                "olist",
                "order_number",
                ("order_id",),
                "DERIVED",
                "reuse order_id as stable display number",
            ),
            _mapping(
                "olist",
                "status",
                ("order_status",),
                "DERIVED",
                "map delivered/canceled/other to contract enum",
            ),
            _mapping(
                "olist",
                "region",
                ("customer_state",),
                "APPROXIMATE",
                "use customer state as regional proxy",
                "not an operational region",
            ),
            _mapping(
                "olist",
                "source_warehouse_id",
                (),
                "UNAVAILABLE",
                "no warehouse identifier in Olist order tables",
                "reject rather than fabricate",
            ),
            _mapping(
                "olist",
                "ordered_at",
                ("order_purchase_timestamp",),
                "APPROXIMATE",
                "parse timestamp and assume UTC when offset absent",
            ),
            _mapping(
                "olist",
                "promised_at",
                ("order_estimated_delivery_date",),
                "APPROXIMATE",
                "parse estimated date and assume UTC when offset absent",
            ),
            _mapping(
                "olist",
                "fulfilled_at",
                ("order_delivered_customer_date",),
                "APPROXIMATE",
                "parse timestamp and assume UTC when offset absent",
            ),
            _mapping(
                "olist",
                "total_amount",
                ("payment_value",),
                "DERIVED",
                "sum payment_value by order_id",
            ),
            _mapping(
                "olist",
                "currency",
                (),
                "UNAVAILABLE",
                "Olist source has no currency column",
                "do not infer BRL",
            ),
        )


class DataCoAdapter:
    """Adapt DataCo only as a secondary comparison with explicit caveats."""

    dataset = "dataco"
    role = "secondary"

    def adapt(
        self, tables: dict[str, Iterable[dict[str, Any]]], *, sample_size: int | None = None
    ) -> AdapterResult:
        source_tables = {name: bounded_rows(rows, sample_size) for name, rows in tables.items()}
        source_line_rows = {name: len(rows) for name, rows in source_tables.items()}
        rows = [normalize_external_row(row) for row in source_tables.get("orders", [])]
        grouped: dict[str, dict[str, Any]] = {}
        totals: defaultdict[str, Decimal] = defaultdict(Decimal)
        for raw in rows:
            order_id = raw.get("order_id")
            if order_id in (None, ""):
                continue
            key = str(order_id)
            amount = _decimal(raw.get("order_item_total") or raw.get("sales"))
            if amount is not None:
                totals[key] += amount
            grouped.setdefault(key, raw)
        mappings = self._mappings()
        adapted: list[dict[str, Any]] = []
        for order_id, raw in grouped.items():
            raw_status = str(raw.get("order_status") or "").lower()
            delivery_status = str(raw.get("delivery_status") or "").lower()
            status = (
                "CANCELLED"
                if "cancel" in raw_status
                else (
                    "FULFILLED"
                    if raw_status in {"complete", "completed"} or "delivered" in delivery_status
                    else "OPEN"
                )
            )
            adapted.append(
                {
                    "source_order_id": order_id,
                    "order_number": order_id,
                    "status": status,
                    "region": raw.get("order_region"),
                    "source_warehouse_id": None,
                    "ordered_at": _timestamp(raw.get("order_date_dateorders")),
                    "promised_at": None,
                    "fulfilled_at": None,
                    "total_amount": f"{totals[order_id]:.2f}" if order_id in totals else None,
                    "currency": None,
                }
            )
        unavailable = tuple(
            mapping for mapping in mappings if mapping.classification == "UNAVAILABLE"
        )
        return AdapterResult(
            dataset=self.dataset,
            role=self.role,
            rows_by_artifact={"oms/orders.csv": adapted},
            provenance=tuple(_provenance("DataCo", mapping) for mapping in mappings),
            mappings=mappings,
            unavailable=unavailable,
            rows_read=source_line_rows,
            source_line_rows=source_line_rows,
            adapted_orders={"oms/orders.csv": len(adapted)},
        )

    @staticmethod
    def _mappings() -> tuple[FieldMapping, ...]:
        return (
            _mapping("dataco", "source_order_id", ("order_id",), "DIRECT", "copy Order Id"),
            _mapping(
                "dataco",
                "order_number",
                ("order_id",),
                "DERIVED",
                "reuse Order Id as stable display number",
            ),
            _mapping(
                "dataco",
                "status",
                ("order_status", "delivery_status"),
                "DERIVED",
                "map DataCo status vocabulary to contract enum",
            ),
            _mapping("dataco", "region", ("order_region",), "DIRECT", "copy Order Region"),
            _mapping(
                "dataco",
                "source_warehouse_id",
                (),
                "UNAVAILABLE",
                "DataCo has no operational warehouse identifier",
                "reject rather than fabricate",
            ),
            _mapping(
                "dataco",
                "ordered_at",
                ("order_date_dateorders",),
                "APPROXIMATE",
                "parse source date and assume UTC when offset absent",
            ),
            _mapping(
                "dataco",
                "promised_at",
                (),
                "UNAVAILABLE",
                "shipping schedule is not a promised delivery timestamp",
                "do not reinterpret Days for shipment scheduled",
            ),
            _mapping(
                "dataco",
                "fulfilled_at",
                (),
                "UNAVAILABLE",
                "no trustworthy delivered-at timestamp in the approved mapping",
                "do not use shipping date as delivery",
            ),
            _mapping(
                "dataco",
                "total_amount",
                ("order_item_total", "sales"),
                "DERIVED",
                "sum order line amounts by Order Id",
            ),
            _mapping(
                "dataco",
                "currency",
                (),
                "UNAVAILABLE",
                "DataCo source currency is not part of the approved mapping",
                "do not infer USD",
            ),
        )


__all__ = ["DataCoAdapter", "OlistAdapter", "bounded_rows", "normalize_external_row"]
