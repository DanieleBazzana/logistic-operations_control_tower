"""Data-driven Streamlit views for the Operations Control Tower."""

from __future__ import annotations

import csv
import io
import os
from collections.abc import Mapping, Sequence
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


def build_exception_filters(
    *,
    exception_types: Sequence[str] = (),
    severities: Sequence[str] = (),
    statuses: Sequence[str] = (),
    warehouse_id: str = "",
    entity_type: str = "",
    entity_id: str = "",
) -> dict[str, Any]:
    """Translate controls to M04 names; supplier is intentionally excluded."""

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
        return f"${value}"
    if kind == "percent":
        return f"{value}%"
    return str(value)


def _sidebar_filters() -> tuple[dict[str, Any], str, int]:
    st.sidebar.header("Filters")
    exception_types = st.sidebar.multiselect("Exception type", EXCEPTION_TYPES)
    severities = st.sidebar.multiselect("Severity", SEVERITIES)
    statuses = st.sidebar.multiselect(
        "Status", STATUSES, default=["OPEN", "ACKNOWLEDGED", "IN_PROGRESS"]
    )
    warehouse_id = st.sidebar.text_input("Warehouse ID")
    entity_type = st.sidebar.text_input("Entity type")
    entity_id = st.sidebar.text_input("Entity ID")
    supplier_id = st.sidebar.text_input("Supplier ID (purchase-order context)")
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
    with st.spinner("Loading exception detail..."):
        try:
            detail = client.get_exception(exception_id)
        except DashboardAPIError as error:
            st.error(str(error))
            return

    st.subheader(f"Exception detail · #{detail.get('id', exception_id)}")
    st.write(f"**Business impact:** {detail.get('business_impact', '—')}")
    st.write(f"**Operational status:** {detail.get('status', '—')}")
    left, right = st.columns(2)
    with left:
        st.write(f"**Revenue at risk:** {detail.get('revenue_at_risk', '—')}")
        st.write(f"**Orders affected:** {detail.get('orders_affected', '—')}")
        st.write(f"**Root cause:** {detail.get('root_cause', '—')}")
    with right:
        st.write(f"**Recommended action:** {detail.get('recommended_action', '—')}")
        st.write(f"**Confidence:** {detail.get('confidence', '—')}")
        st.write(f"**Detected:** {detail.get('detected_at', '—')}")

    history = detail.get("history") or []
    if history:
        st.caption("Lifecycle history")
        st.dataframe(pd.DataFrame(history), hide_index=True, use_container_width=True)

    current_status = detail.get("status", "OPEN")
    transitions = LEGAL_TRANSITIONS.get(current_status, ())
    if not transitions:
        st.info("This exception is in a terminal state; no further transition is available.")
        return
    with st.form(f"lifecycle-{exception_id}"):
        target_status = st.selectbox("New operational status", transitions)
        actor = st.text_input("Actor")
        reason = st.text_area("Reason", help="Required when resolving or dismissing.")
        submitted = st.form_submit_button("Save status")
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
    with st.spinner("Loading supplier purchase orders..."):
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
        st.info("No purchase orders match this supplier context.")
        return
    st.dataframe(pd.DataFrame(orders), hide_index=True, use_container_width=True)


def render_dashboard(client) -> None:
    """Render the complete dashboard; ``client`` is injectable for AppTest/fakes."""

    st.set_page_config(page_title="Operations Control Tower", layout="wide")
    st.title("Operations Control Tower")
    st.caption("M05 dashboard · read data through the versioned FastAPI boundary")
    filters, supplier_id, page_size = _sidebar_filters()
    warehouse_id = str(filters.pop("_warehouse_id", ""))

    with st.spinner("Loading KPI summary..."):
        try:
            summary = _cache_get("summary", {}, client.summary)
        except DashboardAPIError as error:
            st.error(str(error))
            st.info("Start the M04 API and confirm API_BASE_URL before retrying.")
            return
    render_kpis(summary)

    st.header("Exception Queue")
    page = st.number_input("Queue page", min_value=1, value=1, step=1)
    with st.spinner("Loading exception queue..."):
        try:
            body = _cache_get(
                "exceptions",
                {**filters, "page": page, "page_size": page_size},
                lambda: client.list_exceptions(page=page, page_size=page_size, filters=filters),
            )
        except DashboardAPIError as error:
            st.error(str(error))
            return
    rows = body.get("items", [])
    total = int(body.get("total", len(rows)))
    st.caption(f"Showing page {int(page)} · {total} matching exceptions")
    if not rows:
        st.info("No exceptions match the selected filters.")
    else:
        frame = pd.DataFrame(
            [{column: row.get(column, "") for column in QUEUE_COLUMNS} for row in rows]
        )
        st.dataframe(frame, hide_index=True, use_container_width=True)
        try:
            all_rows = _cache_get(
                "exception_export", filters, lambda: client.get_all_exceptions(filters)
            )
            st.download_button(
                "Download filtered queue CSV",
                data=exceptions_to_csv(all_rows),
                file_name="operations-exception-queue.csv",
                mime="text/csv",
            )
        except DashboardAPIError as error:
            st.error(str(error))

        ids = [int(row["id"]) for row in rows if row.get("id") is not None]
        if ids:
            selected_id = st.selectbox(
                "Open exception detail", ids, format_func=lambda value: f"Exception #{value}"
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
    "render_dashboard",
]
