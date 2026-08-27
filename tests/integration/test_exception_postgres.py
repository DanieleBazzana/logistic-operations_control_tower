"""M03 PostgreSQL end-to-end gate; skipped without a disposable test URL."""

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from control_tower.config import Settings, set_alembic_database_url
from control_tower.db import create_db_engine
from control_tower.enums import ExceptionStatus, ExceptionType
from control_tower.exceptions.lifecycle import transition_exception
from control_tower.exceptions.service import ExceptionService
from control_tower.ingestion.loader import ingest
from control_tower.models import ExceptionHistory, ExceptionRecord
from control_tower.synthetic.generator import generate


@pytest.mark.integration
def test_m03_postgres_upgrade_ingest_detect_dedupe_lifecycle_and_rollback(
    tmp_path: Path,
) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not set; use a disposable migrated PostgreSQL database")
    if not database_url.startswith("postgresql"):
        pytest.fail("TEST_DATABASE_URL must be a PostgreSQL URL")

    bundle = tmp_path / "bundle"
    as_of = datetime(2025, 1, 15, 12, tzinfo=timezone.utc)
    generate(bundle, seed=19, as_of=as_of, product_count=8, order_count=20)
    engine = create_db_engine(Settings(database_url=database_url))
    config = Config("alembic.ini")
    set_alembic_database_url(config, database_url)
    try:
        command.upgrade(config, "head")
        first = ingest(bundle, engine=engine)
        second = ingest(bundle, engine=engine)
        assert first.committed
        assert second.committed
        assert second.inserted == 0
        assert second.skipped > 0

        settings = Settings(
            database_url=database_url,
            safety_stock=20,
            inventory_mismatch_tolerance=1,
        )
        with Session(engine) as session:
            result = ExceptionService(session, settings).detect(as_of)
            session.commit()
            assert {finding.exception_type for finding in result.detections} == set(ExceptionType)
            assert result.detections
            assert result.created == len(result.detections)
            initial_count = session.scalar(select(func.count()).select_from(ExceptionRecord))

            rerun = ExceptionService(session, settings).detect(as_of)
            session.commit()
            assert rerun.created == 0
            assert rerun.updated == len(result.detections)
            assert (
                session.scalar(select(func.count()).select_from(ExceptionRecord)) == initial_count
            )

            record = session.scalar(
                select(ExceptionRecord).where(
                    ExceptionRecord.exception_type == ExceptionType.SLA_BREACH_RISK
                )
            )
            assert record is not None
            transition_exception(
                session,
                record.id,
                ExceptionStatus.ACKNOWLEDGED,
                actor="integration-test",
                changed_at=as_of,
            )
            transition_exception(
                session,
                record.id,
                ExceptionStatus.IN_PROGRESS,
                actor="integration-test",
                changed_at=as_of,
            )
            transition_exception(
                session,
                record.id,
                ExceptionStatus.RESOLVED,
                actor="integration-test",
                reason="integration fixture resolved",
                changed_at=as_of,
            )
            session.commit()
            assert record.status == ExceptionStatus.RESOLVED
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(ExceptionHistory)
                    .where(ExceptionHistory.exception_id == record.id)
                )
                == 4
            )

            after_resolution = ExceptionService(session, settings).detect(as_of)
            session.commit()
            assert after_resolution.skipped >= 1

        with engine.begin() as connection:
            history_id = connection.execute(
                text("SELECT id FROM exception_history LIMIT 1")
            ).scalar_one()
        with pytest.raises(SQLAlchemyError):
            with engine.begin() as connection:
                connection.execute(
                    text("UPDATE exception_history SET actor = 'tampered' WHERE id = :id"),
                    {"id": history_id},
                )
        with pytest.raises(SQLAlchemyError):
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM exception_history WHERE id = :id"),
                    {"id": history_id},
                )
        with pytest.raises(SQLAlchemyError):
            with engine.begin() as connection:
                connection.execute(text("TRUNCATE exception_history"))

        command.downgrade(config, "20250827_01")
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM pg_trigger "
                        "WHERE tgname IN ("
                        "'trg_exception_history_append_only', "
                        "'trg_exception_history_append_only_truncate')"
                    )
                ).scalar_one()
                == 0
            )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM pg_trigger "
                        "WHERE tgname IN ("
                        "'trg_exception_history_append_only', "
                        "'trg_exception_history_append_only_truncate')"
                    )
                ).scalar_one()
                == 2
            )
    except SQLAlchemyError as error:
        pytest.fail(f"PostgreSQL M03 integration gate failed: {error}")
    finally:
        engine.dispose()
