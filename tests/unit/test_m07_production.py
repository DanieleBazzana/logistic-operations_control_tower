"""Focused M07 production-readiness contract tests."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from control_tower.api.request_logging import RequestLoggingMiddleware
from control_tower.config import Settings


@pytest.mark.anyio
async def test_public_demo_rejects_lifecycle_mutation_without_changing_state_or_history(
    api_client,
):
    application = api_client._transport.app
    application.state.settings = Settings(public_demo_read_only=True)

    async with api_client as client:
        before = await client.get("/api/v1/exceptions/1")
        response = await client.patch(
            "/api/v1/exceptions/1/status",
            json={"status": "ACKNOWLEDGED", "actor": "public-demo"},
        )
        after = await client.get("/api/v1/exceptions/1")

    assert response.status_code == 403
    assert response.json() == {"detail": "public demo is read-only"}
    assert after.json()["status"] == before.json()["status"]
    assert after.json()["history"] == before.json()["history"]


@pytest.mark.anyio
async def test_operational_read_only_endpoints_and_readiness_are_available(api_client):
    application = api_client._transport.app
    application.state.settings = Settings(public_demo_read_only=True)

    async with api_client as client:
        responses = await asyncio_gather(
            client.get("/api/v1/livez"),
            client.get("/api/v1/readyz"),
            client.get("/api/v1/orders"),
            client.get("/api/v1/inventory"),
            client.get("/api/v1/purchase-orders"),
            client.get("/api/v1/shipments"),
            client.get("/api/v1/exceptions"),
            client.get("/api/v1/kpis/summary"),
        )

    assert all(response.status_code == 200 for response in responses)
    assert responses[0].json() == {"status": "ok"}
    assert responses[1].json() == {"status": "ok"}


async def asyncio_gather(*awaitables):
    """Keep the test dependency surface on the already-declared asyncio stack."""

    import asyncio

    return await asyncio.gather(*awaitables)


@pytest.mark.anyio
async def test_request_log_is_structured_and_correlated_without_query_or_secrets(
    api_client, caplog: pytest.LogCaptureFixture
):
    application = api_client._transport.app
    application.state.settings = Settings(public_demo_read_only=True)

    request_logger = logging.getLogger("control_tower.request")
    caplog.set_level(logging.INFO, logger="control_tower.request")
    original_disabled = request_logger.disabled
    original_propagate = request_logger.propagate
    request_logger.disabled = False
    request_logger.addHandler(caplog.handler)
    request_logger.propagate = False
    try:
        async with api_client as client:
            response = await client.get(
                "/api/v1/exceptions",
                params={"status": "OPEN", "password": "synthetic-secret"},
                headers={"X-Request-ID": "m07-correlation"},
            )
    finally:
        request_logger.disabled = original_disabled
        request_logger.propagate = original_propagate
        request_logger.removeHandler(caplog.handler)

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "m07-correlation"
    request_records = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "control_tower.request"
    ]
    request_record = next(
        record for record in request_records if record["request_id"] == "m07-correlation"
    )
    assert request_record["method"] == "GET"
    assert request_record["path"] == "/api/v1/exceptions"
    assert request_record["status_code"] == 200
    assert "synthetic-secret" not in json.dumps(request_record)
    assert "password" not in request_record


@pytest.mark.anyio
async def test_request_logging_covers_success_handled_error_and_generated_id(
    api_client, caplog: pytest.LogCaptureFixture
):
    request_logger = logging.getLogger("control_tower.request")
    caplog.set_level(logging.INFO, logger="control_tower.request")
    original_disabled = request_logger.disabled
    original_propagate = request_logger.propagate
    request_logger.disabled = False
    request_logger.addHandler(caplog.handler)
    request_logger.propagate = False
    try:
        async with api_client as client:
            success = await client.get("/api/v1/livez", headers={"X-Request-ID": "m07-success"})
            handled_error = await client.get(
                "/api/v1/exceptions/999",
                params={"password": "synthetic-secret"},
                headers={"X-Request-ID": "invalid id"},
            )
    finally:
        request_logger.disabled = original_disabled
        request_logger.propagate = original_propagate
        request_logger.removeHandler(caplog.handler)

    assert success.status_code == 200
    assert handled_error.status_code == 404
    generated_id = handled_error.headers["x-request-id"]
    assert generated_id != "invalid id"
    records = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "control_tower.request"
    ]
    assert len(records) == 2
    assert {record["status_code"] for record in records} == {200, 404}
    assert all(record["request_id"] in {"m07-success", generated_id} for record in records)
    assert "synthetic-secret" not in json.dumps(records)
    assert all("password" not in record for record in records)


@pytest.mark.anyio
async def test_unhandled_exception_returns_correlated_generic_500_and_one_safe_log(
    caplog: pytest.LogCaptureFixture,
):
    application = FastAPI()

    @application.get("/boom")
    async def boom():
        raise RuntimeError("synthetic-secret should never be logged")

    application.add_middleware(RequestLoggingMiddleware)
    request_logger = logging.getLogger("control_tower.request")
    caplog.set_level(logging.INFO, logger="control_tower.request")
    original_disabled = request_logger.disabled
    original_propagate = request_logger.propagate
    request_logger.disabled = False
    request_logger.addHandler(caplog.handler)
    request_logger.propagate = False
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/boom?password=synthetic-secret",
                headers={"X-Request-ID": "m07-unhandled"},
            )
    finally:
        request_logger.disabled = original_disabled
        request_logger.propagate = original_propagate
        request_logger.removeHandler(caplog.handler)

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error"}
    assert response.headers["x-request-id"] == "m07-unhandled"
    records = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "control_tower.request"
    ]
    assert len(records) == 1
    assert records[0]["status_code"] == 500
    assert records[0]["request_id"] == "m07-unhandled"
    assert "synthetic-secret" not in json.dumps(records[0])
    assert "password" not in records[0]


def test_public_demo_flag_is_explicit_and_environment_configurable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PUBLIC_DEMO_READ_ONLY", "true")

    assert Settings().public_demo_read_only is True


def _run_public_exception_detail(client):
    from control_tower.dashboard.ui import render_exception_detail

    render_exception_detail(client, 1)


@pytest.mark.usefixtures("no_dashboard_public_flag")
def test_dashboard_hides_lifecycle_controls_for_public_demo(monkeypatch):
    monkeypatch.setenv("PUBLIC_DEMO_READ_ONLY", "true")
    from streamlit.testing.v1 import AppTest
    from tests.unit.test_dashboard_ui import FakeClient

    test_app = AppTest.from_function(_run_public_exception_detail, args=(FakeClient(),)).run()

    assert test_app.exception == []
    assert test_app.button == []
    assert any("read-only" in item.value.lower() for item in test_app.info)


@pytest.fixture
def no_dashboard_public_flag(monkeypatch):
    monkeypatch.delenv("PUBLIC_DEMO_READ_ONLY", raising=False)
