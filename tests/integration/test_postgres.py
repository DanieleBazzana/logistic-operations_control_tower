"""PostgreSQL-only migration and health checks.

Run with TEST_DATABASE_URL pointing at an isolated disposable PostgreSQL database.
The test intentionally reports an explicit skip when that external prerequisite is
not supplied; the M01 handoff must still record the skipped integration gate.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from control_tower.config import Settings, set_alembic_database_url
from control_tower.db import check_database_health, create_db_engine
from control_tower.enums import OrderObservationStatus
from control_tower.ingestion.order_observations import (
    promote_order_observation,
    stage_order_observation,
)
from control_tower.models import (
    Order,
    OrderObservation,
    OrderObservationReceipt,
    SourceOrderIdentity,
    Warehouse,
)


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
            "order_observations",
            "source_order_identities",
            "order_observation_receipts",
            "alembic_version",
        }
        order_columns = {column["name"] for column in inspect(engine).get_columns("orders")}
        assert {
            "source_namespace",
            "source_order_identity_id",
            "projected_source_version",
            "current_observation_id",
        } <= order_columns
        observation_columns = {
            column["name"] for column in inspect(engine).get_columns("order_observations")
        }
        assert {
            "source_order_identity_id",
            "source_version",
            "source_row_hash",
            "replay_identity",
            "replay_identity_digest",
            "source_facts",
            "capabilities",
            "non_promotion_reasons",
            "promoted_order_id",
            "superseded_by_observation_id",
        } <= observation_columns
        assert {
            constraint["name"]
            for constraint in inspect(engine).get_unique_constraints("source_order_identities")
        } >= {"uq_source_order_identity_namespace_order"}
        assert {
            constraint["name"]
            for constraint in inspect(engine).get_unique_constraints("order_observation_receipts")
        } >= {"uq_order_observation_receipt_observation_batch"}
    except SQLAlchemyError as error:
        pytest.fail(f"PostgreSQL was supplied but migration/health failed: {error}")
    finally:
        engine.dispose()


@pytest.mark.integration
@pytest.mark.usefixtures("reset_disposable_postgres_database")
def test_order_observation_migration_upgrade_and_downgrade() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not set; start Compose PostgreSQL for this gate")
    if not database_url.startswith("postgresql"):
        pytest.fail("TEST_DATABASE_URL must be a PostgreSQL URL")

    config = Config("alembic.ini")
    set_alembic_database_url(config, database_url)
    engine = create_db_engine(Settings(database_url=database_url))
    try:
        command.upgrade(config, "head")
        assert "order_observations" in inspect(engine).get_table_names()
        command.downgrade(config, "20250827_02")
        assert {
            "order_observations",
            "source_order_identities",
            "order_observation_receipts",
        }.isdisjoint(inspect(engine).get_table_names())
        command.upgrade(config, "head")
        assert "order_observations" in inspect(engine).get_table_names()
    except SQLAlchemyError as error:
        pytest.fail(f"PostgreSQL order-observation migration round trip failed: {error}")
    finally:
        engine.dispose()


@pytest.mark.integration
def test_postgresql_rejects_direct_observation_evidence_update_and_delete() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not set; start disposable PostgreSQL for this gate")
    if not database_url.startswith("postgresql"):
        pytest.fail("TEST_DATABASE_URL must be a PostgreSQL URL")

    source_order_id = f"immutable-{uuid4().hex}"
    engine = create_db_engine(Settings(database_url=database_url))
    try:
        with Session(engine) as session:
            observation = stage_order_observation(
                session,
                _complete_concurrency_row(source_order_id),
                source_namespace="immutable-test",
                batch_id="immutable-batch",
            )
            session.commit()
            observation_id = observation.id
            original_hash = observation.source_row_hash

        with engine.begin() as connection:
            with pytest.raises(SQLAlchemyError, match="immutable"):
                connection.execute(
                    text(
                        "UPDATE order_observations "
                        "SET source_row_hash = 'changed' WHERE id = :observation_id"
                    ),
                    {"observation_id": observation_id},
                )
        with engine.begin() as connection:
            with pytest.raises(SQLAlchemyError, match="append-only"):
                connection.execute(
                    text("DELETE FROM order_observations WHERE id = :observation_id"),
                    {"observation_id": observation_id},
                )

        with Session(engine) as session:
            persisted = session.get(OrderObservation, observation_id)
            assert persisted is not None
            assert persisted.source_row_hash == original_hash
    finally:
        engine.dispose()


def _complete_concurrency_row(source_order_id: str) -> dict[str, object]:
    return {
        "source_order_id": source_order_id,
        "order_number": f"ord-{source_order_id}",
        "status": "OPEN",
        "region": "EU",
        "source_warehouse_id": "w-concurrency",
        "ordered_at": datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        "promised_at": datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc),
        "total_amount": "42.50",
        "currency": "EUR",
    }


def _run_in_two_sessions(database_url: str, operation):
    engine = create_db_engine(Settings(database_url=database_url))
    barrier = Barrier(2)

    def worker():
        with Session(engine) as session:
            barrier.wait()
            result = operation(session)
            session.commit()
            return result

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(lambda _: worker(), range(2)))
    finally:
        engine.dispose()


def _run_different_operations_in_two_sessions(database_url: str, operations):
    engine = create_db_engine(Settings(database_url=database_url))
    barrier = Barrier(2)

    def worker(operation):
        with Session(engine) as session:
            barrier.wait()
            result = operation(session)
            session.commit()
            return result

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(worker, operation) for operation in operations]
            return [future.result() for future in futures]
    finally:
        engine.dispose()


def _promote_id(session, observation_id: int, version_comparator=None):
    order = promote_order_observation(
        session,
        session.get(OrderObservation, observation_id),
        version_comparator=version_comparator,
    )
    return order.id if order is not None else None


@pytest.mark.integration
def test_concurrent_staging_returns_one_observation_without_integrity_error() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not set; start disposable PostgreSQL for this gate")
    if not database_url.startswith("postgresql"):
        pytest.fail("TEST_DATABASE_URL must be a PostgreSQL URL")

    source_order_id = f"concurrent-stage-{uuid4().hex}"
    raw = _complete_concurrency_row(source_order_id)

    results = _run_in_two_sessions(
        database_url,
        lambda session: (
            stage_order_observation(
                session,
                raw,
                source_namespace="concurrency-test",
                batch_id="stage-race",
            ).id
        ),
    )

    engine = create_db_engine(Settings(database_url=database_url))
    try:
        with Session(engine) as session:
            assert results[0] == results[1]
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(OrderObservation)
                    .where(OrderObservation.id == results[0])
                )
                == 1
            )
    finally:
        engine.dispose()


@pytest.mark.integration
def test_two_session_conflicting_staging_persists_both_observations_and_receipts() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not set; start disposable PostgreSQL for this gate")
    if not database_url.startswith("postgresql"):
        pytest.fail("TEST_DATABASE_URL must be a PostgreSQL URL")

    source_order_id = f"conflicting-stage-{uuid4().hex}"
    operations = [
        lambda session: (
            stage_order_observation(
                session,
                {**_complete_concurrency_row(source_order_id), "total_amount": "10.00"},
                source_namespace="conflict-test",
                source_version="v1",
                batch_id="conflict-10",
            ).id
        ),
        lambda session: (
            stage_order_observation(
                session,
                {**_complete_concurrency_row(source_order_id), "total_amount": "20.00"},
                source_namespace="conflict-test",
                source_version="v1",
                batch_id="conflict-20",
            ).id
        ),
    ]
    results = _run_different_operations_in_two_sessions(database_url, operations)

    engine = create_db_engine(Settings(database_url=database_url))
    try:
        with Session(engine) as session:
            assert results[0] != results[1]
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(SourceOrderIdentity)
                    .where(SourceOrderIdentity.source_namespace == "conflict-test")
                )
                == 1
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(OrderObservation)
                    .where(OrderObservation.source_order_id == source_order_id)
                )
                == 2
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(OrderObservationReceipt)
                    .where(OrderObservationReceipt.batch_id.in_(["conflict-10", "conflict-20"]))
                )
                == 2
            )
    finally:
        engine.dispose()


@pytest.mark.integration
def test_concurrent_promotion_is_idempotent_without_integrity_error() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not set; start disposable PostgreSQL for this gate")
    if not database_url.startswith("postgresql"):
        pytest.fail("TEST_DATABASE_URL must be a PostgreSQL URL")

    source_order_id = f"concurrent-promote-{uuid4().hex}"
    warehouse_source_id = f"w-concurrency-{uuid4().hex}"
    raw = {**_complete_concurrency_row(source_order_id), "source_warehouse_id": warehouse_source_id}
    engine = create_db_engine(Settings(database_url=database_url))
    try:
        with Session(engine) as session:
            warehouse = Warehouse(
                source_warehouse_id=warehouse_source_id,
                code=f"WC-{uuid4().hex[:12]}",
                name="Concurrency Warehouse",
                region="EU",
                timezone="UTC",
            )
            session.add(warehouse)
            observation = stage_order_observation(
                session,
                raw,
                source_namespace="concurrency-test",
                batch_id="promotion-race",
            )
            session.commit()
            observation_id = observation.id
    finally:
        engine.dispose()

    results = _run_in_two_sessions(
        database_url,
        lambda session: (
            promote_order_observation(session, session.get(OrderObservation, observation_id)).id
        ),
    )

    engine = create_db_engine(Settings(database_url=database_url))
    try:
        with Session(engine) as session:
            assert results[0] == results[1]
            assert (
                session.scalar(
                    select(func.count()).select_from(Order).where(Order.id == results[0])
                )
                == 1
            )
            promoted = session.get(OrderObservation, observation_id)
            assert promoted is not None
            assert promoted.status is OrderObservationStatus.PROMOTED
            assert promoted.promoted_order_id == results[0]
    finally:
        engine.dispose()


@pytest.mark.integration
def test_concurrent_promotion_of_amount_10_and_20_keeps_one_newest_projection() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not set; start disposable PostgreSQL for this gate")
    if not database_url.startswith("postgresql"):
        pytest.fail("TEST_DATABASE_URL must be a PostgreSQL URL")

    source_order_id = f"concurrent-amount-{uuid4().hex}"
    warehouse_source_id = f"w-amount-{uuid4().hex}"
    raw = {
        **_complete_concurrency_row(source_order_id),
        "source_warehouse_id": warehouse_source_id,
    }
    engine = create_db_engine(Settings(database_url=database_url))
    try:
        with Session(engine) as session:
            session.add(
                Warehouse(
                    source_warehouse_id=warehouse_source_id,
                    code=f"WA-{uuid4().hex[:12]}",
                    name="Amount Concurrency Warehouse",
                    region="EU",
                    timezone="UTC",
                )
            )
            first = stage_order_observation(
                session,
                {**raw, "total_amount": "10.00"},
                source_namespace="amount-test",
                source_version="v1",
                batch_id="amount-10",
            )
            second = stage_order_observation(
                session,
                {**raw, "total_amount": "20.00"},
                source_namespace="amount-test",
                source_version="v2",
                batch_id="amount-20",
            )
            session.commit()
            first_id, second_id = first.id, second.id
    finally:
        engine.dispose()

    def comparator(current, incoming):
        return current == "v1" and incoming == "v2"

    results = _run_different_operations_in_two_sessions(
        database_url,
        [
            lambda session: _promote_id(session, first_id, comparator),
            lambda session: _promote_id(session, second_id, comparator),
        ],
    )

    engine = create_db_engine(Settings(database_url=database_url))
    try:
        with Session(engine) as session:
            assert any(result is not None for result in results)
            orders = session.scalars(
                select(Order).where(
                    Order.source_namespace == "amount-test",
                    Order.source_order_id == source_order_id,
                )
            ).all()
            assert len(orders) == 1
            order = orders[0]
            assert order is not None
            assert order.total_amount == Decimal("20.00")
            assert order.projected_source_version == "v2"
            assert order.current_observation_id == second_id
            v1 = session.get(OrderObservation, first_id)
            v2 = session.get(OrderObservation, second_id)
            assert v1 is not None
            assert v2 is not None
            assert v1.superseded_by_observation_id in {None, v2.id}
            # Both observations may retain historical promotion links; those
            # links are not a uniqueness invariant for the current projection.
            assert v2.promoted_order_id == order.id
            assert v1.promoted_order_id in {None, order.id}
    finally:
        engine.dispose()


@pytest.mark.integration
def test_postgresql_late_warehouse_registration_retries_context_incomplete_evidence() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not set; start disposable PostgreSQL for this gate")
    if not database_url.startswith("postgresql"):
        pytest.fail("TEST_DATABASE_URL must be a PostgreSQL URL")

    namespace = f"m076-late-warehouse-{uuid4().hex}"
    source_order_id = f"late-warehouse-order-{uuid4().hex}"
    source_warehouse_id = f"late-warehouse-source-{uuid4().hex}"
    batch_id = f"late-warehouse-batch-{uuid4().hex}"
    raw = {
        **_complete_concurrency_row(source_order_id),
        "source_warehouse_id": source_warehouse_id,
    }
    evidence_fields = (
        "source_order_identity_id",
        "source_namespace",
        "source_order_id",
        "source_version",
        "source_row_hash",
        "replay_identity",
        "replay_identity_digest",
        "source_facts",
        "order_number",
        "order_status",
        "region",
        "source_warehouse_id",
        "ordered_at",
        "promised_at",
        "fulfilled_at",
        "total_amount",
        "currency",
    )
    engine = create_db_engine(Settings(database_url=database_url))
    try:
        with Session(engine) as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(Warehouse)
                    .where(Warehouse.source_warehouse_id == source_warehouse_id)
                )
                == 0
            )
            observation = stage_order_observation(
                session, raw, source_namespace=namespace, batch_id=batch_id
            )
            session.commit()
            observation_id = observation.id
            evidence = {field: getattr(observation, field) for field in evidence_fields}

            assert promote_order_observation(session, observation) is None
            assert observation.status is OrderObservationStatus.CONTEXT_INCOMPLETE
            session.commit()

        with Session(engine) as session:
            persisted = session.get(OrderObservation, observation_id)
            assert persisted is not None
            assert persisted.status is OrderObservationStatus.CONTEXT_INCOMPLETE
            assert persisted.capabilities["warehouse"] == "WAREHOUSE_UNKNOWN"
            assert {field: getattr(persisted, field) for field in evidence_fields} == evidence
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(Order)
                    .where(
                        Order.source_namespace == namespace,
                        Order.source_order_id == source_order_id,
                    )
                )
                == 0
            )

            session.add(
                Warehouse(
                    source_warehouse_id=source_warehouse_id,
                    code=f"M076-LW-{uuid4().hex[:12]}",
                    name="M07.6 Late Warehouse",
                    region="EU",
                    timezone="UTC",
                )
            )
            session.commit()

        with Session(engine) as session:
            persisted = session.get(OrderObservation, observation_id)
            assert persisted is not None
            promoted = promote_order_observation(session, persisted)
            assert promoted is not None
            assert persisted.status is OrderObservationStatus.PROMOTED
            assert persisted.capabilities["warehouse"] == "WAREHOUSE_VALIDATED"
            assert promoted.current_observation_id == observation_id
            order_id = promoted.id
            session.commit()

        with Session(engine) as session:
            persisted = session.get(OrderObservation, observation_id)
            order = session.get(Order, order_id)
            assert persisted is not None
            assert order is not None
            assert persisted.status is OrderObservationStatus.PROMOTED
            assert persisted.capabilities["warehouse"] == "WAREHOUSE_VALIDATED"
            assert order.current_observation_id == observation_id
            assert {field: getattr(persisted, field) for field in evidence_fields} == evidence
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(OrderObservation)
                    .where(
                        OrderObservation.source_namespace == namespace,
                        OrderObservation.source_order_id == source_order_id,
                    )
                )
                == 1
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(OrderObservationReceipt)
                    .where(OrderObservationReceipt.observation_id == observation_id)
                )
                == 1
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(Order)
                    .where(
                        Order.source_namespace == namespace,
                        Order.source_order_id == source_order_id,
                    )
                )
                == 1
            )

            retried = promote_order_observation(session, persisted)
            assert retried is not None
            assert retried.id == order_id
            session.commit()

            assert (
                session.scalar(
                    select(func.count())
                    .select_from(OrderObservation)
                    .where(
                        OrderObservation.source_namespace == namespace,
                        OrderObservation.source_order_id == source_order_id,
                    )
                )
                == 1
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(OrderObservationReceipt)
                    .where(OrderObservationReceipt.observation_id == observation_id)
                )
                == 1
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(Order)
                    .where(
                        Order.source_namespace == namespace,
                        Order.source_order_id == source_order_id,
                    )
                )
                == 1
            )
    finally:
        engine.dispose()


@pytest.mark.integration
def test_postgresql_historical_v1_retry_after_v2_current_preserves_v2_projection() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not set; start disposable PostgreSQL for this gate")
    if not database_url.startswith("postgresql"):
        pytest.fail("TEST_DATABASE_URL must be a PostgreSQL URL")

    namespace = f"m076-historical-v1-{uuid4().hex}"
    source_order_id = f"historical-v1-order-{uuid4().hex}"
    source_warehouse_id = f"historical-v1-warehouse-{uuid4().hex}"
    raw = {
        **_complete_concurrency_row(source_order_id),
        "source_warehouse_id": source_warehouse_id,
    }

    def comparator(current: str | None, incoming: str | None) -> bool:
        return current == "v1" and incoming == "v2"

    engine = create_db_engine(Settings(database_url=database_url))
    try:
        with Session(engine) as session:
            session.add(
                Warehouse(
                    source_warehouse_id=source_warehouse_id,
                    code=f"M076-HV1-{uuid4().hex[:12]}",
                    name="M07.6 Historical Version Warehouse",
                    region="EU",
                    timezone="UTC",
                )
            )
            v1 = stage_order_observation(
                session,
                {**raw, "total_amount": "10.00"},
                source_namespace=namespace,
                source_version="v1",
                batch_id=f"historical-v1-batch-{uuid4().hex}",
            )
            v1_order = promote_order_observation(session, v1)
            assert v1_order is not None
            session.commit()
            v1_id = v1.id
            v1_order_id = v1_order.id

        with Session(engine) as session:
            v2 = stage_order_observation(
                session,
                {**raw, "total_amount": "20.00"},
                source_namespace=namespace,
                source_version="v2",
                batch_id=f"historical-v2-batch-{uuid4().hex}",
            )
            session.commit()
            v2_id = v2.id

        with Session(engine) as session:
            v2 = session.get(OrderObservation, v2_id)
            assert v2 is not None
            current_order = promote_order_observation(session, v2, version_comparator=comparator)
            assert current_order is not None
            assert current_order.id == v1_order_id
            assert current_order.current_observation_id == v2_id
            session.commit()
            order_id = current_order.id

        with Session(engine) as session:
            current_order = session.get(Order, order_id)
            v1 = session.get(OrderObservation, v1_id)
            v2 = session.get(OrderObservation, v2_id)
            assert current_order is not None
            assert v1 is not None
            assert v2 is not None
            assert current_order.current_observation_id == v2_id
            assert current_order.projected_source_version == "v2"
            assert v1.source_version == "v1"
            assert v2.source_version == "v2"
            assert v1.source_row_hash != v2.source_row_hash
            strict_projection = {
                field: getattr(current_order, field)
                for field in (
                    "source_order_identity_id",
                    "source_namespace",
                    "source_order_id",
                    "projected_source_version",
                    "order_number",
                    "status",
                    "region",
                    "warehouse_id",
                    "source_warehouse_id",
                    "ordered_at",
                    "promised_at",
                    "fulfilled_at",
                    "total_amount",
                    "currency",
                )
            }

            try:
                historical_retry = promote_order_observation(
                    session, v1, version_comparator=comparator
                )
            except IntegrityError as error:
                pytest.fail(f"historical v1 retry raised IntegrityError: {error}")
            assert historical_retry is None
            session.commit()

            current_order = session.get(Order, order_id)
            v1 = session.get(OrderObservation, v1_id)
            v2 = session.get(OrderObservation, v2_id)
            assert current_order is not None
            assert v1 is not None
            assert v2 is not None
            assert current_order.current_observation_id == v2_id
            assert current_order.current_observation_id != v1_id
            assert current_order.projected_source_version == "v2"
            assert v1.status is OrderObservationStatus.PROMOTED
            assert v1.promoted_order_id == order_id
            assert v1.superseded_by_observation_id == v2_id
            assert v2.status is OrderObservationStatus.PROMOTED
            for field, expected in strict_projection.items():
                assert getattr(current_order, field) == expected
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(Order)
                    .where(
                        Order.source_namespace == namespace,
                        Order.source_order_id == source_order_id,
                    )
                )
                == 1
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(OrderObservation)
                    .where(
                        OrderObservation.source_namespace == namespace,
                        OrderObservation.source_order_id == source_order_id,
                    )
                )
                == 2
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(OrderObservationReceipt)
                    .where(OrderObservationReceipt.observation_id.in_([v1_id, v2_id]))
                )
                == 2
            )
            queryable_observations = session.scalars(
                select(OrderObservation).where(
                    OrderObservation.source_namespace == namespace,
                    OrderObservation.source_order_id == source_order_id,
                    OrderObservation.id.in_([v1_id, v2_id]),
                )
            ).all()
            assert {observation.id for observation in queryable_observations} == {v1_id, v2_id}
    finally:
        engine.dispose()
