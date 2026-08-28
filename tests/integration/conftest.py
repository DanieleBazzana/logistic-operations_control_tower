"""Shared fixtures for PostgreSQL integration gates."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from control_tower.config import set_alembic_database_url


@pytest.fixture
def reset_disposable_postgres_database() -> Iterator[None]:
    """Reset the migration-managed schema for one disposable integration test.

    The fixture is intentionally a no-op unless the explicitly named integration
    URL is present. Because the reset destroys and recreates the schema, it also
    requires an explicit opt-in and a local, disposable PostgreSQL database.
    """
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        yield
        return

    try:
        parsed_url = make_url(database_url)
    except (ArgumentError, ValueError):
        pytest.fail("TEST_DATABASE_URL must be a valid SQLAlchemy PostgreSQL URL")

    if parsed_url.query:
        pytest.fail("TEST_DATABASE_URL must not include query parameters")

    if parsed_url.get_backend_name() != "postgresql":
        pytest.fail("TEST_DATABASE_URL must be a PostgreSQL URL")
    if os.getenv("ALLOW_DESTRUCTIVE_TEST_DB") != "1":
        pytest.fail(
            "TEST_DATABASE_URL points to PostgreSQL; destructive integration reset is disabled. "
            "Set ALLOW_DESTRUCTIVE_TEST_DB=1 only for an isolated disposable database."
        )

    if (parsed_url.host or "").lower() not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail(
            "destructive integration reset requires an isolated local test database "
            "(host must be localhost, 127.0.0.1, or ::1)"
        )
    if parsed_url.database != "control_tower_m04" and not (
        parsed_url.database and parsed_url.database.endswith("_test")
    ):
        pytest.fail(
            "destructive integration reset requires an isolated local test database "
            "(database must be control_tower_m04 or end with _test)"
        )

    alembic_config = Config("alembic.ini")
    set_alembic_database_url(alembic_config, database_url)
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")
    yield
