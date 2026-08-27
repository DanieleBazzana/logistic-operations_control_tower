"""M02 PostgreSQL end-to-end gate; skipped when no disposable URL is supplied."""

import os
from pathlib import Path

import pytest
from sqlalchemy import text

from control_tower.config import Settings
from control_tower.db import create_db_engine
from control_tower.ingestion.loader import ingest
from control_tower.synthetic.generator import generate


@pytest.mark.integration
def test_generated_bundle_ingests_idempotently_in_postgres(tmp_path: Path) -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not set; use a disposable migrated PostgreSQL database")
    if not database_url.startswith("postgresql"):
        pytest.fail("TEST_DATABASE_URL must be a PostgreSQL URL")
    bundle = tmp_path / "bundle"
    generate(bundle, seed=19, as_of="2025-01-15T12:00:00+00:00", product_count=8, order_count=20)
    engine = create_db_engine(Settings(database_url=database_url))
    try:
        first = ingest(bundle, engine=engine)
        second = ingest(bundle, engine=engine)
        assert first.committed
        assert second.committed
        assert second.inserted == 0
        assert second.skipped > 0
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM products")).scalar_one() == 8
    finally:
        engine.dispose()
