"""Small, safe HTTP client for the versioned Operations API.

The dashboard deliberately has no database imports. API response values are kept
as decoded JSON (not converted to floats), so Decimal and money strings retain
M04's wire precision all the way to the UI and CSV export.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

import httpx


class DashboardAPIError(RuntimeError):
    """A user-safe error raised for unavailable or unsuccessful API calls."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class DashboardClient:
    """Synchronous API adapter intended for Streamlit's request-per-run model."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not 0 < timeout <= 60:
            raise ValueError("timeout must be greater than 0 and no greater than 60 seconds")
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self._client = http_client or httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(self.timeout),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DashboardClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @staticmethod
    def _timestamp(value: datetime) -> str:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp filters must include a timezone")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @classmethod
    def _value(cls, value: Any) -> str:
        if isinstance(value, datetime):
            return cls._timestamp(value)
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, Enum):
            return str(value.value)
        return str(value)

    @classmethod
    def _params(cls, params: Mapping[str, Any] | None) -> list[tuple[str, str]]:
        """Build query tuples, retaining repeated values and exact numerics."""

        result: list[tuple[str, str]] = []
        for name, value in (params or {}).items():
            if value is None:
                continue
            values = (
                value
                if isinstance(value, Sequence) and not isinstance(value, (str, bytes))
                else [value]
            )
            result.extend((name, cls._value(item)) for item in values)
        return result

    @staticmethod
    def _safe_error(status_code: int) -> str:
        return {
            400: "The API rejected the request.",
            401: "The Operations API requires authorization.",
            403: "The Operations API denied the request.",
            404: "The requested operational record was not found.",
            409: "The requested lifecycle change is not allowed.",
            422: "The API rejected the supplied filters or values.",
            503: "The Operations API is unavailable.",
        }.get(status_code, "The Operations API request failed.")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> Any:
        try:
            response = self._client.request(
                method, path, params=self._params(params), json=json
            )
        except httpx.TimeoutException as exc:
            raise DashboardAPIError("The Operations API request timed out.") from exc
        except httpx.RequestError as exc:
            raise DashboardAPIError("The Operations API cannot be reached.") from exc
        if response.status_code >= 400:
            raise DashboardAPIError(
                self._safe_error(response.status_code), status_code=response.status_code
            )
        try:
            return response.json()
        except ValueError as exc:
            raise DashboardAPIError("The Operations API returned an invalid response.") from exc

    def summary(self, *, as_of: datetime | None = None) -> dict[str, Any]:
        return self._request("GET", "/kpis/summary", params={"as_of": as_of})

    def list_exceptions(
        self,
        *,
        page: int = 1,
        page_size: int = 25,
        filters: Mapping[str, Any] | None = None,
        **filter_values: Any,
    ) -> dict[str, Any]:
        params = dict(filters or {})
        params.update(filter_values)
        params.update(page=page, page_size=page_size)
        return self._request("GET", "/exceptions", params=params)

    def get_all_exceptions(
        self, filters: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Fetch every matching page so export is not limited to visible rows."""

        page = 1
        page_size = 100
        rows: list[dict[str, Any]] = []
        total = None
        while total is None or len(rows) < total:
            body = self.list_exceptions(page=page, page_size=page_size, filters=filters)
            items = body.get("items", [])
            rows.extend(items)
            total = int(body.get("total", len(rows)))
            if not items:
                break
            page += 1
        return rows

    def get_exception(self, exception_id: int) -> dict[str, Any]:
        return self._request("GET", f"/exceptions/{int(exception_id)}")

    def update_exception_status(
        self,
        exception_id: int,
        status: str,
        *,
        actor: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": status, "actor": actor.strip()}
        if reason is not None:
            payload["reason"] = reason.strip()
        return self._request("PATCH", f"/exceptions/{int(exception_id)}/status", json=payload)

    def list_purchase_orders(
        self,
        *,
        page: int = 1,
        page_size: int = 25,
        filters: Mapping[str, Any] | None = None,
        **filter_values: Any,
    ) -> dict[str, Any]:
        params = dict(filters or {})
        params.update(filter_values)
        params.update(page=page, page_size=page_size)
        return self._request("GET", "/purchase-orders", params=params)

    def get_all_purchase_orders(
        self, filters: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        page = 1
        rows: list[dict[str, Any]] = []
        total = None
        while total is None or len(rows) < total:
            body = self.list_purchase_orders(page=page, page_size=100, filters=filters)
            items = body.get("items", [])
            rows.extend(items)
            total = int(body.get("total", len(rows)))
            if not items:
                break
            page += 1
        return rows


__all__ = ["DashboardAPIError", "DashboardClient"]
