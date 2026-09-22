"""Live PostgreSQL verification for dataset replacement and activation semantics.

The suite never selects a default database.  It requires TEST_DATABASE_URL and
accepts only a loopback PostgreSQL database whose name is clearly disposable.
Each behavior test uses one rolled-back transaction, so existing disposable data
is observed but not changed.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from control_tower.enums import ExceptionType
from control_tower.exceptions.contracts import ExceptionDetection
from control_tower.exceptions.service import persist_detections
from control_tower.models import (
    DatasetActivation,
    DatasetVersion,
    ExceptionRecord,
    Product,
    Warehouse,
    explicit_dataset_scope,
)
from control_tower.replacement.service import (
    LEGACY_AS_OF,
    LEGACY_DATASET_KEY,
    LEGACY_SEED,
    SCOPED_MODELS,
    DatasetIdentityConflict,
    DatasetLifecycleError,
    activate_dataset,
    backfill_m076,
    ensure_dataset_version,
    get_active_dataset_version,
    rollback_dataset,
    scope_statement,
    stage_dataset_version,
    validate_dataset,
)

HEAD_REVISION = "20260917_01"
UTC = timezone.utc


def _validate_disposable_url(raw_url: str) -> None:
    """Reject remote, non-PostgreSQL, and non-test database targets."""

    try:
        parsed = make_url(raw_url)
    except (TypeError, ValueError) as error:
        pytest.fail(f"TEST_DATABASE_URL is not a valid SQLAlchemy URL: {error}")
    if parsed.get_backend_name() != "postgresql":
        pytest.fail("TEST_DATABASE_URL must use a PostgreSQL backend")
    if parsed.query:
        pytest.fail("TEST_DATABASE_URL must not contain query parameters")
    if (parsed.host or "").lower() not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("TEST_DATABASE_URL must target a loopback PostgreSQL host")
    database = parsed.database or ""
    suffix = database.removeprefix("control_tower_")
    if not database.startswith("control_tower_") or not (
        database.endswith("_test") or suffix.startswith("m") and suffix[1:].isdigit()
    ):
        pytest.fail(
            "TEST_DATABASE_URL must target a disposable "
            "control_tower_*_test or control_tower_m<id> database"
        )


@pytest.fixture(scope="module")
def database_url() -> str:
    raw_url = os.getenv("TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("TEST_DATABASE_URL is not set; replacement PostgreSQL gate skipped")
    _validate_disposable_url(raw_url)
    return raw_url


@pytest.fixture(scope="module")
def postgres_engine(database_url: str) -> Iterator[Engine]:
    engine = create_engine(database_url, pool_pre_ping=True, future=True)
    try:
        with engine.connect() as connection:
            try:
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                tables = {
                    "dataset_versions",
                    "dataset_activation",
                    "products",
                    "warehouses",
                }
                present = {
                    row[0]
                    for row in connection.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = 'public'"
                        )
                    )
                }
            except SQLAlchemyError as error:
                pytest.fail(
                    "TEST_DATABASE_URL must point to a database already migrated to head: "
                    f"{error}"
                )
        if revision != HEAD_REVISION:
            pytest.fail(
                f"replacement tests require Alembic head {HEAD_REVISION}, found {revision!r}"
            )
        missing = tables - present
        if missing:
            pytest.fail(f"migrated PostgreSQL database is missing tables: {sorted(missing)}")
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_session(postgres_engine: Engine) -> Iterator[Session]:
    """Run each scenario in a transaction and never commit test data."""

    session = Session(postgres_engine)
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _legacy_snapshot(session: Session) -> tuple[object, ...]:
    legacy = session.scalar(
        select(DatasetVersion).where(DatasetVersion.dataset_key == LEGACY_DATASET_KEY)
    )
    pointer = session.get(DatasetActivation, 1)
    assert legacy is not None
    assert pointer is not None
    rows = tuple(
        (
            model.__tablename__,
            tuple(
                session.execute(
                    select(model.id, model.dataset_version_id).order_by(model.id)
                ).all()
            ),
        )
        for model in SCOPED_MODELS
    )
    return (
        legacy.id,
        legacy.identity_hash,
        legacy.content_hash,
        legacy.status,
        legacy.created_at,
        legacy.updated_at,
        pointer.active_dataset_version_id,
        pointer.updated_at,
        rows,
    )


def _candidate(session: Session, suffix: str) -> DatasetVersion:
    return ensure_dataset_version(
        session,
        dataset_key="M07.7",
        manifest_identity=f"replacement-integration-{suffix}",
        content_hash=hashlib.sha256(suffix.encode("utf-8")).hexdigest(),
        seed=LEGACY_SEED,
        as_of=datetime(2025, 3, 8, 18, tzinfo=UTC),
        generator_revision=f"replacement-integration-{suffix}",
    )


@pytest.mark.integration
def test_m076_backfill_is_deterministic_non_destructive_and_active(
    db_session: Session,
) -> None:
    before = _legacy_snapshot(db_session)
    legacy = backfill_m076(db_session)
    db_session.flush()
    after_first = _legacy_snapshot(db_session)
    retried = backfill_m076(db_session)
    db_session.flush()
    after_second = _legacy_snapshot(db_session)

    assert legacy.id == retried.id == before[0]
    assert legacy.dataset_key == LEGACY_DATASET_KEY
    assert legacy.status == "ACTIVE"
    assert legacy.as_of == LEGACY_AS_OF
    assert legacy.seed == LEGACY_SEED
    assert after_first == before
    assert after_second == before
    active = get_active_dataset_version(db_session)
    assert active is not None
    assert active.id == legacy.id
    assert db_session.scalar(select(DatasetActivation.active_dataset_version_id)) == legacy.id


@pytest.mark.integration
def test_staged_candidate_is_invisible_until_activation_and_can_roll_back(
    db_session: Session,
) -> None:
    legacy = get_active_dataset_version(db_session)
    assert legacy is not None
    assert legacy.dataset_key == LEGACY_DATASET_KEY

    candidate = _candidate(db_session, "visibility")
    assert candidate.status == "STAGED"
    stage_dataset_version(db_session, candidate)
    with pytest.raises(DatasetLifecycleError, match="READY"):
        activate_dataset(db_session, candidate)
    with pytest.raises(DatasetLifecycleError, match="rollback target"):
        rollback_dataset(db_session, candidate)

    marker = "replacement-integration-visible"
    with explicit_dataset_scope(db_session):
        product = Product(
            dataset_version_id=candidate.id,
            source_product_id=marker,
            sku=marker,
            name="Replacement candidate product",
            unit_price=Decimal("12.34"),
        )
        db_session.add(product)
        db_session.flush()

    active_rows = db_session.scalars(
        scope_statement(select(Product), Product, legacy.id)
    ).all()
    candidate_rows = db_session.scalars(
        scope_statement(select(Product), Product, candidate.id)
    ).all()
    assert marker not in {row.source_product_id for row in active_rows}
    assert [row.id for row in candidate_rows] == [product.id]
    active = get_active_dataset_version(db_session)
    assert active is not None
    assert active.id == legacy.id

    validate_dataset(db_session, candidate)
    activated = activate_dataset(db_session, candidate)
    assert activated.id == candidate.id
    assert activated.status == "ACTIVE"
    active = get_active_dataset_version(db_session)
    assert active is not None
    assert active.id == candidate.id
    retired_legacy = db_session.get(DatasetVersion, legacy.id)
    assert retired_legacy is not None
    assert retired_legacy.status == "RETIRED"
    assert marker in {
        row.source_product_id
        for row in db_session.scalars(
            scope_statement(select(Product), Product, candidate.id)
        ).all()
    }

    activated_again = activate_dataset(db_session, candidate)
    assert activated_again.id == candidate.id
    assert db_session.scalar(select(DatasetActivation.active_dataset_version_id)) == candidate.id

    rolled_back = rollback_dataset(db_session, candidate)
    assert rolled_back.id == legacy.id
    active = get_active_dataset_version(db_session)
    assert active is not None
    assert active.id == legacy.id
    active_legacy = db_session.get(DatasetVersion, legacy.id)
    retired_candidate = db_session.get(DatasetVersion, candidate.id)
    assert active_legacy is not None
    assert retired_candidate is not None
    assert active_legacy.status == "ACTIVE"
    assert retired_candidate.status == "RETIRED"


@pytest.mark.integration
def test_source_ids_can_be_reused_across_staged_dataset_versions_and_retry_is_idempotent(
    db_session: Session,
) -> None:
    first = _candidate(db_session, "reuse-one")
    retried = _candidate(db_session, "reuse-one")
    second = _candidate(db_session, "reuse-two")
    assert retried.id == first.id
    with pytest.raises(DatasetIdentityConflict, match="different content"):
        ensure_dataset_version(
            db_session,
            dataset_key="M07.7",
            manifest_identity="replacement-integration-reuse-one",
            content_hash=hashlib.sha256(b"changed-content").hexdigest(),
            seed=LEGACY_SEED,
            as_of=datetime(2025, 3, 8, 18, tzinfo=UTC),
            generator_revision="replacement-integration-reuse-one",
        )

    shared_source_id = "replacement-integration-shared-source"
    with explicit_dataset_scope(db_session):
        first_product = Product(
            dataset_version_id=first.id,
            source_product_id=shared_source_id,
            sku=shared_source_id,
            name="First dataset product",
            unit_price=Decimal("1.00"),
        )
        second_product = Product(
            dataset_version_id=second.id,
            source_product_id=shared_source_id,
            sku=shared_source_id,
            name="Second dataset product",
            unit_price=Decimal("2.00"),
        )
        first_warehouse = Warehouse(
            dataset_version_id=first.id,
            source_warehouse_id=shared_source_id,
            code=shared_source_id,
            name="First dataset warehouse",
            region="EU",
            timezone="UTC",
        )
        second_warehouse = Warehouse(
            dataset_version_id=second.id,
            source_warehouse_id=shared_source_id,
            code=shared_source_id,
            name="Second dataset warehouse",
            region="EU",
            timezone="UTC",
        )
        db_session.add_all([first_product, second_product, first_warehouse, second_warehouse])
        db_session.flush()

    assert first_product.id != second_product.id
    assert first_warehouse.id != second_warehouse.id
    assert first_product.dataset_version_id == first.id
    assert second_product.dataset_version_id == second.id
    assert first_warehouse.dataset_version_id == first.id
    assert second_warehouse.dataset_version_id == second.id
    assert db_session.scalar(
        select(Product.id).where(
            Product.dataset_version_id == first.id,
            Product.source_product_id == shared_source_id,
        )
    ) == first_product.id
    assert db_session.scalar(
        select(Product.id).where(
            Product.dataset_version_id == second.id,
            Product.source_product_id == shared_source_id,
        )
    ) == second_product.id


@pytest.mark.integration
def test_candidate_exception_write_without_dataset_id_fails_closed(
    db_session: Session,
) -> None:
    active = get_active_dataset_version(db_session)
    assert active is not None
    assert active.dataset_key == LEGACY_DATASET_KEY
    _candidate(db_session, "exception-isolation")
    marker = "replacement-integration-exception-isolation"
    before = db_session.scalar(
        select(func.count())
        .select_from(ExceptionRecord)
        .where(ExceptionRecord.dataset_version_id == active.id)
    )
    detection = ExceptionDetection(
        exception_type=ExceptionType.SLA_BREACH_RISK,
        issue_key=marker,
        entity_type="ORDER",
        entity_id=marker,
        expected_resolution=None,
        business_impact="candidate isolation test",
        root_cause="candidate isolation test",
        recommended_action="candidate isolation test",
    )

    with explicit_dataset_scope(db_session), pytest.raises(
        ValueError, match="dataset_version_id is required"
    ):
        persist_detections(db_session, (detection,), LEGACY_AS_OF)

    after = db_session.scalar(
        select(func.count())
        .select_from(ExceptionRecord)
        .where(ExceptionRecord.dataset_version_id == active.id)
    )
    assert after == before
    assert db_session.scalar(
        select(func.count())
        .select_from(ExceptionRecord)
        .where(ExceptionRecord.issue_key == marker)
    ) == 0


@pytest.mark.integration
def test_disposable_pg_dump_restore_round_trip(
    postgres_engine: Engine,
    database_url: str,
) -> None:
    required_tools = ("pg_dump", "pg_restore", "createdb", "dropdb")
    missing = [tool for tool in required_tools if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"backup round-trip unavailable; missing command(s): {', '.join(missing)}")

    parsed = make_url(database_url)
    assert parsed.database is not None
    restore_database = f"{parsed.database}_backup_{uuid4().hex[:8]}"
    command_env = os.environ.copy()
    if parsed.password is not None:
        command_env["PGPASSWORD"] = parsed.password
    connection_args = [
        "-h",
        parsed.host or "127.0.0.1",
        "-p",
        str(parsed.port or 5432),
        "-U",
        parsed.username or "",
    ]
    created = False
    try:
        with tempfile.TemporaryDirectory(prefix="replacement-pgdump-") as directory:
            dump_path = Path(directory) / "control_tower.dump"
            subprocess.run(
                [
                    "pg_dump",
                    *connection_args,
                    "--format=custom",
                    "--no-owner",
                    "--file",
                    dump_path,
                    parsed.database,
                ],
                check=True,
                env=command_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["createdb", *connection_args, restore_database],
                check=True,
                env=command_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            created = True
            subprocess.run(
                [
                    "pg_restore",
                    *connection_args,
                    "--exit-on-error",
                    "--no-owner",
                    "--dbname",
                    restore_database,
                    str(dump_path),
                ],
                check=True,
                env=command_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            restored_engine = create_engine(parsed.set(database=restore_database))
            try:
                with restored_engine.connect() as connection:
                    revision = connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalar_one()
                    versions = connection.execute(
                        text("SELECT count(*) FROM dataset_versions")
                    ).scalar_one()
                    activations = connection.execute(
                        text("SELECT count(*) FROM dataset_activation")
                    ).scalar_one()
                    assert revision == HEAD_REVISION
                    assert versions >= 1
                    assert activations == 1
            finally:
                restored_engine.dispose()
    except subprocess.CalledProcessError:
        pytest.fail("pg_dump/pg_restore disposable backup round-trip failed")
    finally:
        if created:
            subprocess.run(
                ["dropdb", *connection_args, "--if-exists", restore_database],
                check=False,
                env=command_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
