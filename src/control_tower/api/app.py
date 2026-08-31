"""FastAPI application factory for the versioned Operations API."""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from control_tower.api.errors import database_error_handler
from control_tower.api.request_logging import RequestLoggingMiddleware
from control_tower.api.routes import router
from control_tower.config import Settings
from control_tower.db import create_db_engine, create_session_factory


def create_app(*, engine: Engine | None = None, settings: Settings | None = None) -> FastAPI:
    configured_settings = settings or Settings()
    bound_engine = engine or create_db_engine(configured_settings)
    application = FastAPI(
        title="Supply Chain Operations Control Tower API",
        version="1.0.0",
        description="Read-only operational resources and controlled exception lifecycle updates.",
    )
    application.state.engine = bound_engine
    application.state.settings = configured_settings
    application.state.session_factory = create_session_factory(engine=bound_engine)
    application.add_middleware(RequestLoggingMiddleware)
    application.add_exception_handler(SQLAlchemyError, database_error_handler)
    application.include_router(router, prefix="/api/v1")
    return application


app = create_app()

__all__ = ["app", "create_app"]
