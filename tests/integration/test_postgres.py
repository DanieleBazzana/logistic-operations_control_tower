"""PostgreSQL-only migration and health checks.

Run with TEST_DATABASE_URL pointing at an isolated disposable PostgreSQL database.
The test intentionally reports an explicit skip when that external prerequisite is
not supplied; the M01 handoff must still record the skipped integration gate.
"""

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from control_tower.config import Settings, set_alembic_database_url
from control_tower.db import check_database_health, create_db_engine


@pytest.mark.integration
def test_postgresql_migration_and_health() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not set; start Compose PostgreSQL for this gate")
    if not database_url.startswith("postgresql"):
        pytest.fail("TEST_DATABASE_URL must be a PostgreSQL URL")

    engine = create_db_engine(Settings(database_url=database_url))
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        config = Config("alembic.ini")
        set_alembic_database_url(config, database_url)
        command.upgrade(config, "head")
        assert check_database_health(engine)
        assert set(inspect(engine).get_table_names()) >= {
            "products",
            "warehouses",
            "inventory",
            "orders",
            "exceptions",
            "exception_history",
            "alembic_version",
        }
    except SQLAlchemyError as error:
        pytest.fail(f"PostgreSQL was supplied but migration/health failed: {error}")
    finally:
        engine.dispose()
