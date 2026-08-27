from pydantic import ValidationError
from sqlalchemy import create_engine

from control_tower.config import Settings
from control_tower.db import check_database_health, create_db_engine, create_session_factory


def test_database_engine_uses_postgresql_driver() -> None:
    settings = Settings()
    engine = create_db_engine(settings)

    assert engine.url.get_backend_name() == "postgresql"
    assert engine.url.get_driver_name() == "psycopg"
    engine.dispose()


def test_sqlite_database_urls_are_rejected() -> None:
    try:
        Settings(database_url="sqlite:///not-supported.db")
    except ValidationError as error:
        assert "database_url" in str(error)
    else:
        raise AssertionError("SQLite must not be accepted as an application database")


def test_session_factory_can_be_bound_to_a_postgresql_test_engine() -> None:
    engine = create_engine("postgresql+psycopg://user:password@localhost:5432/db")

    factory = create_session_factory(engine=engine)

    assert factory.kw["bind"] is engine
    engine.dispose()


def test_health_helper_executes_select_one_without_schema_creation() -> None:
    class StubConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, statement):
            assert str(statement) == "SELECT 1"
            return self

        def scalar_one(self):
            return 1

    class StubEngine:
        def connect(self):
            return StubConnection()

    assert check_database_health(StubEngine()) is True
