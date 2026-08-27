"""Database engine, session, metadata, and health helpers."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from control_tower.config import Settings

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base shared by every database model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp for application-created rows."""

    return datetime.now(timezone.utc)


def create_db_engine(settings: Settings | None = None) -> Engine:
    """Build a PostgreSQL engine from validated application settings.

    This function does not create schemas or tables. Apply ``alembic upgrade head``
    explicitly before using a new database.
    """

    configured_settings = settings or Settings()
    return create_engine(
        str(configured_settings.database_url),
        pool_pre_ping=True,
        future=True,
    )


def create_session_factory(
    settings: Settings | None = None,
    engine: Engine | None = None,
) -> sessionmaker[Session]:
    """Create a synchronous SQLAlchemy session factory."""

    bound_engine = engine or create_db_engine(settings)
    return sessionmaker(bind=bound_engine, autoflush=False, expire_on_commit=False)


@contextmanager
def session_scope(
    settings: Settings | None = None,
    engine: Engine | None = None,
) -> Iterator[Session]:
    """Yield a transactional session and roll it back on errors."""

    session = create_session_factory(settings=settings, engine=engine)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_database_health(engine: Engine) -> bool:
    """Run the required lightweight PostgreSQL health query."""

    with engine.connect() as connection:
        return connection.execute(text("SELECT 1")).scalar_one() == 1


# Public aliases keep the helper discoverable for API/bootstrap callers.
database_health_check = check_database_health
SessionFactory = sessionmaker[Any]
