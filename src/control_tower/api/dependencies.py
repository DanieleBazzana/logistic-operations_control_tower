"""Database dependencies for the versioned HTTP API."""

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from control_tower.config import Settings


def get_engine(request: Request) -> Engine:
    return request.app.state.engine


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_session(request: Request) -> Iterator[Session]:
    """Yield one request-scoped session; route code owns commit boundaries."""

    session_factory = request.app.state.session_factory
    with session_factory() as session:
        yield session
