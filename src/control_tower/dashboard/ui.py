"""Data-driven Streamlit views for the Operations Control Tower."""

from __future__ import annotations

import csv
import io
import os
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd
import streamlit as st

from control_tower.dashboard.client import DashboardAPIError, DashboardClient

KPI_DEFINITIONS = (
    ("orders_processed", "Orders processed", "count"),
    ("sla_performance_pct", "SLA performance", "percent"),
    ("open_exceptions", "Open exceptions", "count"),
    ("critical_exceptions", "Critical exceptions", "count"),
    ("revenue_at_risk", "Revenue at risk", "money"),
    ("stockout_risks", "Stockout risks", "count"),
    ("supplier_delays", "Supplier delays", "count"),
    ("shipment_delays", "Shipment delays", "count"),
)

EXCEPTION_TYPES = (
    "SLA_BREACH_RISK",
    "INVENTORY_SHORTAGE",
    "STOCKOUT_RISK",
    "INVENTORY_MISMATCH",
    "SUPPLIER_DELAY",
    "SHIPMENT_DELAY",
)
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
STATUSES = ("OPEN", "ACKNOWLEDGED", "IN_PROGRESS", "RESOLVED", "DISMISSED")
LEGAL_TRANSITIONS = {
    "OPEN": ("ACKNOWLEDGED", "DISMISSED"),
    "ACKNOWLEDGED": ("IN_PROGRESS", "DISMISSED"),
    "IN_PROGRESS": ("RESOLVED", "DISMISSED"),
    "RESOLVED": (),
    "DISMISSED": (),
}


def public_demo_read_only() -> bool:
    """Read the explicit UI safety boundary without loading database settings."""

    return os.getenv("PUBLIC_DEMO_READ_ONLY", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


QUEUE_COLUMNS = (
    "id",
    "exception_type",
    "severity",
    "status",
    "entity_type",
    "entity_id",
    "business_impact",
    "revenue_at_risk",
    "orders_affected",
    "detected_at",
    "recommended_action",
)

QUEUE_COLUMN_LABELS = {
    "id": "Exception ID",
    "exception_type": "Exception type",
    "severity": "Severity",
    "status": "Status",
    "entity_type": "Entity type",
    "entity_id": "Entity ID",
    "business_impact": "Business impact",
    "revenue_at_risk": "Revenue at risk",
    "orders_affected": "Orders affected",
    "detected_at": "Detected at",
    "recommended_action": "Recommended action",
}

HISTORY_COLUMN_LABELS = {
    "id": "History ID",
    "from_status": "Previous status",
    "to_status": "New status",
    "changed_at": "Changed at",
    "actor": "Changed by",
    "transition_reason": "Reason",
}

PURCHASE_ORDER_COLUMN_LABELS = {
    "source_purchase_order_id": "Purchase order ID",
    "po_number": "PO number",
    "source_supplier_id": "Supplier ID",
    "supplier_name": "Supplier",
    "source_warehouse_id": "Warehouse ID",
    "status": "Status",
    "ordered_at": "Ordered at",
    "expected_delivery_at": "Expected delivery",
    "received_at": "Received at",
    "remaining_quantity": "Remaining quantity",
}


def build_exception_filters(
    *,
    exception_types: Sequence[str] = (),
    severities: Sequence[str] = (),
    statuses: Sequence[str] = (),
    warehouse_id: str = "",
    entity_type: str = "",
    entity_id: str = "",
) -> dict[str, Any]:
    """Translate dashboard controls to API filter names; supplier is context-only."""

    filters: dict[str, Any] = {}
    if exception_types:
        filters["exception_type"] = list(exception_types)
    if severities:
        filters["severity"] = list(severities)
    if statuses:
        filters["status"] = list(statuses)
    if warehouse_id.strip():
        filters["warehouse_id"] = warehouse_id.strip()
    if entity_type.strip():
        filters["entity_type"] = entity_type.strip()
    if entity_id.strip():
        filters["entity_id"] = entity_id.strip()
    return filters


def build_purchase_order_filters(
    supplier_id: str = "", warehouse_id: str = ""
) -> dict[str, str]:
    filters: dict[str, str] = {}
    if supplier_id.strip():
        filters["supplier_id"] = supplier_id.strip()
    if warehouse_id.strip():
        filters["warehouse_id"] = warehouse_id.strip()
    return filters


def exceptions_to_csv(rows: Sequence[Mapping[str, Any]]) -> str:
    """Return a stable, flat CSV containing every row passed by the caller."""

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(QUEUE_COLUMNS), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in QUEUE_COLUMNS})
    return output.getvalue()


def format_enum(value: Any) -> str:
    """Turn API enum values into readable labels without changing wire values."""

    if value is None or value == "":
        return "—"
    raw_value = str(getattr(value, "value", value)).replace("_", " ").lower()
    if raw_value.startswith("sla "):
        return "SLA " + raw_value[4:]
    return raw_value.capitalize()


def format_currency(value: Any) -> str:
    """Format money for display while leaving API and CSV values untouched."""

    if value is None or value == "":
        return "—"
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    return f"${amount:,.2f}"


def format_timestamp(value: Any) -> str:
    """Format an API timestamp consistently in UTC for operations users."""

    if value is None or value == "":
        return "—"
    try:
        timestamp = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            return str(value)
        return timestamp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return str(value)


def format_confidence(value: Any) -> str:
    """Render the API's decimal confidence ratio as a percentage."""

    if value is None or value == "":
        return "—"
    try:
        confidence = Decimal(str(value)) * 100
    except (InvalidOperation, ValueError):
        return str(value)
    percent = confidence.quantize(Decimal("0.1"))
    return f"{percent:.1f}".rstrip("0").rstrip(".") + "%"


def queue_snapshot_as_of(body: Mapping[str, Any]) -> datetime | None:
    """Find the deterministic detection instant carried by queue rows."""

    # The queue response has no response-level ``as_of``; ``detected_at`` is the
    # existing row field written from the detection run instant, shared by the
    # findings in a deterministic dataset.
    candidates: list[datetime] = []
    for row in body.get("items", []):
        value = row.get("detected_at")
        if value:
            try:
                timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                continue
            if timestamp.tzinfo is not None and timestamp.utcoffset() is not None:
                candidates.append(timestamp.astimezone(timezone.utc))
    return max(candidates) if candidates else None


def _format_queue_rows(rows: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    presented = []
    for row in rows:
        presented.append(
            {
                QUEUE_COLUMN_LABELS[column]: (
                    format_enum(row.get(column))
                    if column in {"exception_type", "severity", "status"}
                    else format_currency(row.get(column))
                    if column == "revenue_at_risk"
                    else format_timestamp(row.get(column))
                    if column == "detected_at"
                    else row.get(column, "—")
                )
                for column in QUEUE_COLUMNS
            }
        )
    return pd.DataFrame(presented, columns=list(QUEUE_COLUMN_LABELS.values()))


def _format_history_rows(rows: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                HISTORY_COLUMN_LABELS[column]: (
                    format_enum(row.get(column))
                    if column in {"from_status", "to_status"}
                    else format_timestamp(row.get(column))
                    if column == "changed_at"
                    else row.get(column, "—")
                )
                for column in HISTORY_COLUMN_LABELS
            }
            for row in rows
        ],
        columns=list(HISTORY_COLUMN_LABELS.values()),
    )


def _format_purchase_order_rows(rows: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                PURCHASE_ORDER_COLUMN_LABELS[column]: (
                    format_enum(row.get(column))
                    if column == "status"
                    else format_timestamp(row.get(column))
                    if column.endswith("_at")
                    else row.get(column, "—")
                )
                for column in PURCHASE_ORDER_COLUMN_LABELS
            }
            for row in rows
        ],
        columns=list(PURCHASE_ORDER_COLUMN_LABELS.values()),
    )


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple(sorted((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(_freeze(item) for item in value)
    return value


def _cache_get(name: str, params: Mapping[str, Any], loader: Any) -> Any:
    version = st.session_state.setdefault("_dashboard_data_version", 0)
    cache = st.session_state.setdefault("_dashboard_cache", {})
    key = (name, version, _freeze(params))
    if key not in cache:
        cache[key] = loader()
    return cache[key]


def invalidate_dashboard_cache() -> None:
    """Invalidate all read data after a lifecycle mutation."""

    st.session_state["_dashboard_data_version"] = st.session_state.get(
        "_dashboard_data_version", 0
    ) + 1
    st.session_state.pop("_dashboard_cache", None)
    st.cache_data.clear()


def _format_kpi(value: Any, kind: str) -> str:
    if value is None:
        return "—"
    if kind == "money":
        return format_currency(value)
    if kind == "percent":
        return f"{value}%"
    return str(value)


def _sidebar_filters() -> tuple[dict[str, Any], str, int]:
    st.sidebar.header("Queue filters")
    exception_types = st.sidebar.multiselect(
        "Exception type", EXCEPTION_TYPES, format_func=format_enum
    )
    severities = st.sidebar.multiselect("Severity", SEVERITIES, format_func=format_enum)
    statuses = st.sidebar.multiselect(
        "Lifecycle status",
        STATUSES,
        default=["OPEN", "ACKNOWLEDGED", "IN_PROGRESS"],
        format_func=format_enum,
    )
    warehouse_id = st.sidebar.text_input("Warehouse ID", help="Filter exceptions to one warehouse.")
    entity_type = st.sidebar.text_input("Entity type")
    entity_id = st.sidebar.text_input("Entity ID")
    supplier_id = st.sidebar.text_input("Supplier ID", help="Optional purchase-order context.")
    page_size = st.sidebar.selectbox("Rows per page", (25, 50, 100), index=0)
    filters = build_exception_filters(
        exception_types=exception_types,
        severities=severities,
        statuses=statuses,
        warehouse_id=warehouse_id,
        entity_type=entity_type,
        entity_id=entity_id,
    )
    filters["_warehouse_id"] = warehouse_id
    return filters, supplier_id.strip(), page_size


def render_kpis(summary: Mapping[str, Any]) -> None:
    columns = st.columns(4)
    for index, (key, label, kind) in enumerate(KPI_DEFINITIONS):
        with columns[index % 4]:
            st.metric(label, _format_kpi(summary.get(key), kind))


def render_exception_detail(client: Any, exception_id: int) -> None:
    with st.spinner("Loading exception details…"):
        try:
            detail = client.get_exception(exception_id)
        except DashboardAPIError as error:
            st.error(str(error))
            return

    st.subheader(f"Exception details · #{detail.get('id', exception_id)}")
    st.write(f"**Business impact:** {detail.get('business_impact', '—')}")
    st.write(f"**Operational status:** {format_enum(detail.get('status'))}")
    left, right = st.columns(2)
    with left:
        st.write(f"**Revenue at risk:** {format_currency(detail.get('revenue_at_risk'))}")
        st.write(f"**Orders affected:** {detail.get('orders_affected', '—')}")
        st.write(f"**Root cause:** {detail.get('root_cause', '—')}")
    with right:
        st.write(f"**Recommended action:** {detail.get('recommended_action', '—')}")
        st.write(f"**Confidence:** {format_confidence(detail.get('confidence'))}")
        st.write(f"**Detected:** {format_timestamp(detail.get('detected_at'))}")

    history = detail.get("history") or []
    if history:
        st.subheader("Lifecycle history")
        st.dataframe(_format_history_rows(history), hide_index=True, use_container_width=True)

    if public_demo_read_only():
        st.info("The public demo is read-only; lifecycle updates are disabled.")
        return

    current_status = detail.get("status", "OPEN")
    transitions = LEGAL_TRANSITIONS.get(current_status, ())
    if not transitions:
        st.info("This exception is in a terminal state; no further transition is available.")
        return
    with st.form(f"lifecycle-{exception_id}"):
        target_status = st.selectbox("New lifecycle status", transitions, format_func=format_enum)
        actor = st.text_input("Operator name")
        reason = st.text_area("Transition reason", help="Required when resolving or dismissing.")
        submitted = st.form_submit_button("Update exception")
    if submitted:
        if not actor.strip():
            st.error("Actor is required.")
            return
        if target_status in ("RESOLVED", "DISMISSED") and not reason.strip():
            st.error("A reason is required for terminal statuses.")
            return
        try:
            client.update_exception_status(
                exception_id, target_status, actor=actor, reason=reason or None
            )
        except DashboardAPIError as error:
            st.error(str(error))
            return
        invalidate_dashboard_cache()
        st.success("Exception status updated.")
        st.rerun()


def _supplier_context(client: Any, supplier_id: str, warehouse_id: str) -> None:
    if not supplier_id:
        return
    st.subheader("Supplier context")
    filters = build_purchase_order_filters(supplier_id, warehouse_id)
    with st.spinner("Loading supplier purchase orders…"):
        try:
            body = _cache_get(
                "purchase_orders",
                filters,
                lambda: client.list_purchase_orders(filters=filters),
            )
        except DashboardAPIError as error:
            st.error(str(error))
            return
    orders = body.get("items", [])
    if not orders:
        st.info("No purchase orders match this supplier.")
        return
    st.dataframe(_format_purchase_order_rows(orders), hide_index=True, use_container_width=True)


def render_dashboard(client) -> None:
    """Render the complete dashboard; ``client`` is injectable for AppTest and fakes."""

    st.set_page_config(page_title="Operations Control Tower", layout="wide")
    st.title("Operations Control Tower")
    st.subheader("Prioritize operational exceptions, understand impact, and coordinate resolution.")
    if public_demo_read_only():
        st.caption("Public Demo · Read Only")
    filters, supplier_id, page_size = _sidebar_filters()
    warehouse_id = str(filters.pop("_warehouse_id", ""))

    st.header("Exception queue")
    page = st.number_input("Queue page number", min_value=1, value=1, step=1)
    with st.spinner("Loading exception queue…"):
        try:
            body = _cache_get(
                "exceptions",
                {**filters, "page": page, "page_size": page_size},
                lambda: client.list_exceptions(page=page, page_size=page_size, filters=filters),
            )
        except DashboardAPIError as error:
            st.error(f"Unable to load the exception queue. {error}")
            return
    rows = body.get("items", [])
    snapshot_as_of = queue_snapshot_as_of(body)

    with st.spinner("Loading operational summary…"):
        try:
            summary_params = {"as_of": snapshot_as_of} if snapshot_as_of else {}
            summary = _cache_get(
                "summary", summary_params, lambda: client.summary(**summary_params)
            )
        except DashboardAPIError as error:
            st.error(f"Unable to load the operational summary. {error}")
            return
    render_kpis(summary)
    st.caption(f"Snapshot: {format_timestamp(summary.get('as_of'))}")

    total = int(body.get("total", len(rows)))
    st.caption(f"Page {int(page)} · {total} exceptions match the current filters")
    if not rows:
        st.info("No exceptions match the current filters.")
    else:
        frame = _format_queue_rows(rows)
        st.dataframe(frame, hide_index=True, use_container_width=True)
        try:
            all_rows = _cache_get(
                "exception_export", filters, lambda: client.get_all_exceptions(filters)
            )
            st.download_button(
                "Download queue CSV",
                data=exceptions_to_csv(all_rows),
                file_name="operations-exception-queue.csv",
                mime="text/csv",
            )
        except DashboardAPIError as error:
            st.error(f"Unable to prepare the queue export. {error}")

        ids = [int(row["id"]) for row in rows if row.get("id") is not None]
        if ids:
            selected_id = st.selectbox(
                "Select an exception", ids, format_func=lambda value: f"Exception #{value}"
            )
            render_exception_detail(client, selected_id)

    _supplier_context(client, supplier_id, warehouse_id)


def main(client: Any | None = None) -> None:
    """Streamlit entry point, with an injectable client for deterministic tests."""

    api_base_url = os.getenv("API_BASE_URL", "http://127.0.0.1:8000/api/v1")
    owned_client = client is None
    api_client = client or DashboardClient(api_base_url)
    try:
        render_dashboard(api_client)
    finally:
        if owned_client:
            api_client.close()


__all__ = [
    "KPI_DEFINITIONS",
    "build_exception_filters",
    "build_purchase_order_filters",
    "exceptions_to_csv",
    "invalidate_dashboard_cache",
    "main",
    "public_demo_read_only",
    "render_dashboard",
]
