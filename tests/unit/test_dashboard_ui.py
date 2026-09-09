
from datetime import datetime, timezone

import pytest
from streamlit.testing.v1 import AppTest

import control_tower.dashboard.ui as dashboard_ui
from control_tower.dashboard.ui import (
    KPI_DEFINITIONS,
    _dashboard_styles,
    build_exception_filters,
    build_purchase_order_filters,
    exceptions_to_csv,
    faceted_exception_counts,
    format_confidence,
    format_currency,
    format_enum,
    format_timestamp,
)


def test_dashboard_styles_define_scoped_semantic_tokens():
    styles = _dashboard_styles()

    assert '<style data-testid="dashboard-styles">' in styles
    assert '[data-testid="dashboard-section"]' in styles
    assert '[data-testid="dashboard-kpi-band"]' in styles
    assert "--oc-app-bg" in styles
    assert "--oc-accent" in styles
    assert "--oc-status-critical" in styles
    assert "--oc-status-warning" in styles
    assert "--oc-status-success" in styles
    assert "--oc-status-neutral" in styles


def test_presentation_marker_escapes_dynamic_values(monkeypatch):
    rendered = []
    monkeypatch.setattr(
        dashboard_ui.st,
        "markdown",
        lambda value, **kwargs: rendered.append((value, kwargs)),
    )

    dashboard_ui._presentation_marker('queue" onclick="bad', '<script>alert("x")</script>')

    assert rendered == [
        (
            '<div data-testid="queue&quot; onclick=&quot;bad">'
            '&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;</div>',
            {"unsafe_allow_html": True},
        )
    ]


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
    "detected_at": "2025-03-01T12:00:00Z",
    "recommended_action": "Expedite",
    "root_cause": "Carrier delay",
    "confidence": "0.9000",
    "source_warehouse_id": "W1",
    "history": [],
}

QUEUE_AS_OF = datetime(2025, 3, 1, 12, tzinfo=timezone.utc)
SUMMARY_FALLBACK_AS_OF = "2025-01-15T12:00:00Z"


class FakeClient:
    def __init__(self):
        self.updated = []
        self.status = "OPEN"
        self.list_calls = []
        self.summary_calls = []

    def summary(self, **kwargs):
        self.summary_calls.append(kwargs)
        as_of = kwargs.get("as_of", SUMMARY_FALLBACK_AS_OF)
        if isinstance(as_of, datetime):
            as_of = as_of.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return {
            "as_of": as_of,
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
        self.list_calls.append(kwargs)
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


def test_dashboard_formats_wire_values_for_operations_users():
    assert format_enum("SLA_BREACH_RISK") == "SLA breach risk"
    assert format_enum("IN_PROGRESS") == "In progress"
    assert format_currency("1234567.50") == "$1,234,567.50"
    assert format_timestamp("2025-01-15T12:00:00Z") == "2025-01-15 12:00 UTC"
    assert format_confidence("0.9000") == "90%"


@pytest.fixture(autouse=True)
def _disable_public_demo(monkeypatch):
    monkeypatch.delenv("PUBLIC_DEMO_READ_ONLY", raising=False)


def test_dashboard_requests_kpis_independently_from_queue_snapshot():
    client = FakeClient()

    test_app = AppTest.from_function(_run_dashboard, args=(client,)).run()

    assert test_app.exception == []
    assert client.summary_calls == [{}]


def test_dashboard_keeps_queue_snapshot_when_filters_change():
    client = FakeClient()
    test_app = AppTest.from_function(_run_dashboard, args=(client,)).run()

    test_app.sidebar.multiselect[0].set_value(["SLA_BREACH_RISK"])
    test_app.run()

    assert test_app.exception == []
    assert client.list_calls[-1]["filters"]["exception_type"] == ["SLA_BREACH_RISK"]
    assert client.summary_calls == [{}]


def test_queue_actions_reset_all_filters_and_pagination():
    client = FakeClient()
    test_app = AppTest.from_function(_run_dashboard, args=(client,)).run()

    test_app.sidebar.multiselect[0].set_value(["SLA_BREACH_RISK"])
    test_app.sidebar.multiselect[1].set_value(["HIGH"])
    test_app.sidebar.multiselect[2].set_value(["RESOLVED"])
    for index, value in enumerate(("W9", "shipment", "S9", "SUP9")):
        test_app.sidebar.text_input[index].set_value(value)
    test_app.sidebar.selectbox[0].set_value(100)
    test_app.number_input[0].set_value(4)
    test_app.run()

    test_app.sidebar.button[0].click().run()
    assert test_app.session_state["filter_status"] == ["OPEN", "ACKNOWLEDGED", "IN_PROGRESS"]
    assert test_app.session_state["filter_exception_type"] == []
    assert test_app.session_state["filter_severity"] == []
    assert all(
        test_app.session_state[key] == ""
        for key in (
            "filter_warehouse_id",
            "filter_entity_type",
            "filter_entity_id",
            "filter_supplier_id",
        )
    )
    assert test_app.session_state["queue_page"] == 1
    assert test_app.session_state["filter_page_size"] == 25

    test_app.sidebar.multiselect[0].set_value(["SHIPMENT_DELAY"])
    test_app.sidebar.multiselect[1].set_value(["CRITICAL"])
    test_app.sidebar.multiselect[2].set_value(["DISMISSED"])
    for index, value in enumerate(("W8", "order", "O8", "SUP8")):
        test_app.sidebar.text_input[index].set_value(value)
    test_app.sidebar.selectbox[0].set_value(100)
    test_app.number_input[0].set_value(3)
    test_app.run()
    test_app.sidebar.button[1].click().run()
    assert test_app.session_state["filter_status"] == ["OPEN", "ACKNOWLEDGED", "IN_PROGRESS"]
    assert test_app.session_state["filter_exception_type"] == []
    assert test_app.session_state["filter_severity"] == []
    assert all(
        test_app.session_state[key] == ""
        for key in (
            "filter_warehouse_id",
            "filter_entity_type",
            "filter_entity_id",
            "filter_supplier_id",
        )
    )
    assert test_app.session_state["queue_page"] == 1
    assert test_app.session_state["filter_page_size"] == 25


def test_faceted_counts_use_total_from_contextual_exception_queries():
    import streamlit as st

    st.session_state.clear()

    class FacetClient:
        def __init__(self):
            self.calls = []

        def list_exceptions(self, **kwargs):
            self.calls.append(kwargs)
            return {"items": [], "total": len(kwargs["filters"].get("status", [])) + 2}

    client = FacetClient()
    counts = faceted_exception_counts(
        client,
        {"status": ["OPEN"], "severity": ["HIGH"], "exception_type": ["SLA_BREACH_RISK"]},
    )

    assert set(counts) == {"status", "exception_type", "severity"}
    assert set(counts["status"]) == {
        "OPEN",
        "ACKNOWLEDGED",
        "IN_PROGRESS",
        "RESOLVED",
        "DISMISSED",
    }
    assert set(counts["exception_type"]) == {
        "SLA_BREACH_RISK",
        "INVENTORY_SHORTAGE",
        "STOCKOUT_RISK",
        "INVENTORY_MISMATCH",
        "SUPPLIER_DELAY",
        "SHIPMENT_DELAY",
    }
    assert set(counts["severity"]) == {"CRITICAL", "HIGH", "MEDIUM", "LOW"}
    assert all(value == 3 for group in counts.values() for value in group.values())
    assert client.calls[0] == {
        "page": 1,
        "page_size": 1,
        "filters": {
            "severity": ["HIGH"],
            "exception_type": ["SLA_BREACH_RISK"],
            "status": ["OPEN"],
        },
    }
    assert any(
        call["filters"] == {
            "status": ["OPEN"],
            "severity": ["HIGH"],
            "exception_type": ["INVENTORY_SHORTAGE"],
        }
        for call in client.calls
    )
    assert any(
        call["filters"] == {
            "status": ["OPEN"],
            "exception_type": ["SLA_BREACH_RISK"],
            "severity": ["CRITICAL"],
        }
        for call in client.calls
    )


def test_dashboard_empty_state_offers_exact_message_and_queue_actions():
    class EmptyClient(FakeClient):
        def list_exceptions(self, **kwargs):
            self.list_calls.append(kwargs)
            return {"items": [], "page": 1, "page_size": 25, "total": 0}

    test_app = AppTest.from_function(_run_dashboard, args=(EmptyClient(),)).run()

    assert any(
        item.value == "No exceptions match this combination of filters." for item in test_app.info
    )
    assert {item.label for item in test_app.button} >= {"Reset filters", "Show active queue"}


def test_dashboard_exposes_stable_presentation_markers():
    test_app = AppTest.from_function(_run_dashboard, args=(FakeClient(),)).run()

    markdown = [item.value for item in test_app.markdown]
    assert any('data-testid="dashboard-styles"' in item for item in markdown)
    assert any('data-testid="dashboard-header"' in item for item in markdown)
    assert any('data-testid="dashboard-kpi-band"' in item for item in markdown)
    assert any('data-testid="dashboard-queue-surface"' in item for item in markdown)
    assert any('data-testid="dashboard-facet-context"' in item for item in markdown)
    assert any('data-testid="dashboard-detail-surface"' in item for item in markdown)


def _run_dashboard(client):
    from control_tower.dashboard.ui import render_dashboard

    render_dashboard(client)


def test_dashboard_renders_kpis_queue_and_supplier_context_with_fake_client():
    test_app = AppTest.from_function(_run_dashboard, args=(FakeClient(),)).run()

    assert test_app.exception == []
    assert any(item.label == "Orders processed" and item.value == "10" for item in test_app.metric)
    assert any(item == "SLA breach risk" for item in test_app.dataframe[0].value["Exception type"])
    assert any("Exception queue" in item.value for item in test_app.header)
    assert any("**Operational status:** Open" in item.value for item in test_app.markdown)
    captions = [item.value for item in test_app.caption]
    assert any(item.startswith("KPI snapshot: ") for item in captions)
    assert any(item.startswith("Queue evaluation: ") for item in captions)
    assert not any("Queue evaluation: 2025-03-01 12:00 UTC" == item for item in captions)
    assert any(item.startswith("Lifecycle status: ") for item in captions)
    assert any(item.startswith("Exception type: ") for item in captions)
    assert any(item.startswith("Severity: ") for item in captions)


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
