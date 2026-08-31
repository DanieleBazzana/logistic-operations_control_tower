"""Small request-correlation middleware with secret-safe structured logs."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

LOGGER = logging.getLogger("control_tower.request")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _request_id(request: Request) -> str:
    candidate = request.headers.get("x-request-id", "")
    return candidate if _SAFE_REQUEST_ID.fullmatch(candidate) else uuid.uuid4().hex


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Attach a correlation response header and log only non-sensitive metadata."""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = _request_id(request)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            response = JSONResponse({"detail": "Internal Server Error"}, status_code=500)
            log_level = logging.ERROR
        else:
            log_level = logging.INFO
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        LOGGER.log(
            log_level,
            json.dumps(
                {
                    "event": "http_request",
                    "method": request.method,
                    "path": request.url.path,
                    "request_id": request_id,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                },
                sort_keys=True,
            )
        )
        return response