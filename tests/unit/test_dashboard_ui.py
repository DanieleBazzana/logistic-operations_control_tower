
from streamlit.testing.v1 import AppTest

from control_tower.dashboard.ui import (
    KPI_DEFINITIONS,
    build_exception_filters,
    build_purchase_order_filters,
    exceptions_to_csv,
)

EXCEPTION = {
    "id": 1,
    "exception_type": "SLA_BREACH_RISK",
    "severity": "HIGH",
    "status": "OPEN",
    "entity_type": "order",
    "entity_id": "O1",
    "business_impact": "Late order",
    "revenue_at_risk": "125.00",
    "orders_affected": 1,
    "detected_at": "2025-01-15T12:00:00Z",
    "recommended_action": "Expedite",
    "root_cause": "Carrier delay",
    "confidence": "0.9000",
    "source_warehouse_id": "W1",
    "history": [],
}


class FakeClient:
    def __init__(self):
        self.updated = []
        self.status = "OPEN"

    def summary(self, **kwargs):
        return {
            "as_of": "2025-01-15T12:00:00Z",
            "orders_processed": 10,
            "open_orders": 3,
            "fulfilled_orders": 6,
            "cancelled_orders": 1,
            "sla_performance_pct": "95.00",
            "open_exceptions": 1,
            "critical_exceptions": 0,
            "revenue_at_risk": "125.00",
            "stockout_risks": 0,
            "supplier_delays": 0,
            "shipment_delays": 0,
        }

    def list_exceptions(self, **kwargs):
        return {"items": [EXCEPTION], "page": 1, "page_size": 25, "total": 1}

    def get_all_exceptions(self, filters=None):
        return [EXCEPTION]

    def get_exception(self, exception_id):
        return {**EXCEPTION, "status": self.status}

    def list_purchase_orders(self, **kwargs):
        return {"items": [], "page": 1, "page_size": 25, "total": 0}

    def update_exception_status(self, exception_id, status, *, actor, reason=None):
        self.updated.append((exception_id, status, actor, reason))
        self.status = status
        return {**EXCEPTION, "status": status}


def test_dashboard_defines_all_eight_charter_kpis():
    assert [key for key, _label, _format in KPI_DEFINITIONS] == [
        "orders_processed",
        "sla_performance_pct",
        "open_exceptions",
        "critical_exceptions",
        "revenue_at_risk",
        "stockout_risks",
        "supplier_delays",
        "shipment_delays",
    ]


def test_supplier_filter_only_applies_to_purchase_order_context():
    exception_filters = build_exception_filters(
        exception_types=["SUPPLIER_DELAY"],
        severities=[],
        statuses=["OPEN"],
        warehouse_id="W1",
        entity_type="purchase_order",
        entity_id="PO1",
    )
    purchase_order_filters = build_purchase_order_filters("SUP1", "W1")

    assert exception_filters == {
        "exception_type": ["SUPPLIER_DELAY"],
        "status": ["OPEN"],
        "warehouse_id": "W1",
        "entity_type": "purchase_order",
        "entity_id": "PO1",
    }
    assert purchase_order_filters == {"supplier_id": "SUP1", "warehouse_id": "W1"}
    assert "supplier_id" not in exception_filters


def test_exception_csv_serializes_all_filtered_rows():
    csv_text = exceptions_to_csv([EXCEPTION, {**EXCEPTION, "id": 2, "status": "ACKNOWLEDGED"}])

    assert "id,exception_type" in csv_text
    assert "1,SLA_BREACH_RISK" in csv_text
    assert "2,SLA_BREACH_RISK" in csv_text


def _run_dashboard(client):
    from control_tower.dashboard.ui import render_dashboard

    render_dashboard(client)


def test_dashboard_renders_kpis_queue_and_supplier_context_with_fake_client():
    test_app = AppTest.from_function(_run_dashboard, args=(FakeClient(),)).run()

    assert test_app.exception == []
    assert any(item.label == "Orders processed" and item.value == "10" for item in test_app.metric)
    assert any(item == "SLA_BREACH_RISK" for item in test_app.dataframe[0].value["exception_type"])
    assert any("Exception Queue" in item.value for item in test_app.header)
    assert any("**Operational status:** OPEN" in item.value for item in test_app.markdown)


def _run_exception_detail(client):
    import streamlit as st

    from control_tower.dashboard.ui import render_exception_detail

    st.session_state.setdefault("_dashboard_cache", {"stale": True})
    render_exception_detail(client, 1)


def test_dashboard_submits_successful_lifecycle_form_and_invalidates_cache(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr("control_tower.dashboard.ui.st.rerun", lambda: None)
    test_app = AppTest.from_function(_run_exception_detail, args=(client,)).run()

    test_app.text_input[0].set_value("operator")
    test_app.text_area[0].set_value("triaged by operations")
    test_app.button[0].click().run()

    assert client.updated == [(1, "ACKNOWLEDGED", "operator", "triaged by operations")]
    assert any("Exception status updated." in item.value for item in test_app.success)
    assert test_app.session_state["_dashboard_data_version"] == 1
    assert "_dashboard_cache" not in test_app.session_state


def test_dashboard_rejects_blank_actor_before_calling_api(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr("control_tower.dashboard.ui.st.rerun", lambda: None)
    test_app = AppTest.from_function(_run_exception_detail, args=(client,)).run()

    test_app.button[0].click().run()

    assert client.updated == []
    assert any("Actor is required." in item.value for item in test_app.error)


def test_dashboard_rejects_blank_terminal_reason_before_calling_api(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr("control_tower.dashboard.ui.st.rerun", lambda: None)
    test_app = AppTest.from_function(_run_exception_detail, args=(client,)).run()

    test_app.selectbox[0].set_value("DISMISSED")
    test_app.text_input[0].set_value("operator")
    test_app.button[0].click().run()

    assert client.updated == []
    assert any(
        "A reason is required for terminal statuses." in item.value for item in test_app.error
    )
