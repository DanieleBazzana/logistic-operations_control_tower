from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from control_tower.dashboard.client import DashboardAPIError, DashboardClient


class RecordingTransport(httpx.BaseTransport):
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def handle_request(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        return httpx.Response(
            response[0],
            json=response[1],
            request=request,
            headers={"content-type": "application/json"},
        )


def test_client_serializes_repeated_filters_timezone_and_decimal_without_float_loss():
    transport = RecordingTransport(
        [(200, {"items": [], "page": 1, "page_size": 100, "total": 0})]
    )
    client = DashboardClient("http://api.test/api/v1", transport=transport)

    client.list_exceptions(
        page=1,
        page_size=100,
        exception_type=["SLA_BREACH_RISK", "STOCKOUT_RISK"],
        severity=["HIGH", "CRITICAL"],
        detected_from=datetime(2025, 1, 15, 12, tzinfo=timezone.utc),
        revenue_at_risk_min=Decimal("0.0001"),
    )

    request = transport.requests[0]
    assert request.url.path == "/api/v1/exceptions"
    assert request.url.params.get_list("exception_type") == [
        "SLA_BREACH_RISK",
        "STOCKOUT_RISK",
    ]
    assert request.url.params["detected_from"] == "2025-01-15T12:00:00Z"
    assert request.url.params["revenue_at_risk_min"] == "0.0001"


def test_client_returns_all_filtered_pages_for_export_not_only_first_page():
    transport = RecordingTransport(
        [
            (200, {"items": [{"id": 1}], "page": 1, "page_size": 2, "total": 3}),
            (200, {"items": [{"id": 2}, {"id": 3}], "page": 2, "page_size": 2, "total": 3}),
        ]
    )
    client = DashboardClient("http://api.test/api/v1", transport=transport)

    rows = client.get_all_exceptions({"status": ["OPEN"]})

    assert [row["id"] for row in rows] == [1, 2, 3]
    assert [request.url.params["page"] for request in transport.requests] == ["1", "2"]
    assert all(request.url.params["status"] == "OPEN" for request in transport.requests)


def test_client_uses_bounded_timeout_and_redacts_api_error_details():
    transport = RecordingTransport(
        [(503, {"detail": "password=secret host=internal db=production SQL SELECT ..."})]
    )
    client = DashboardClient("http://api.test/api/v1", timeout=4.5, transport=transport)

    with pytest.raises(DashboardAPIError) as caught:
        client.summary()

    assert client.timeout == 4.5
    assert caught.value.status_code == 503
    assert str(caught.value) == "The Operations API is unavailable."
    assert "secret" not in str(caught.value)
    assert "SELECT" not in str(caught.value)


def test_client_sends_actor_and_reason_for_lifecycle_transition():
    transport = RecordingTransport([(200, {"id": 9, "status": "ACKNOWLEDGED"})])
    client = DashboardClient("http://api.test/api/v1", transport=transport)

    client.update_exception_status(9, "ACKNOWLEDGED", actor="  Alice ", reason="  triage ")

    request = transport.requests[0]
    assert request.method == "PATCH"
    assert request.url.path == "/api/v1/exceptions/9/status"
    assert request.read() == b'{"status":"ACKNOWLEDGED","actor":"Alice","reason":"triage"}'
