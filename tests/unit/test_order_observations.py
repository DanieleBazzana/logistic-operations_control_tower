from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import JSON, create_engine, delete, func, select, text, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from control_tower.db import Base
from control_tower.enums import OrderObservationStatus
from control_tower.ingestion.order_observations import (
    assess_order_observation,
    promote_order_observation,
    replay_identity_digest,
    source_row_hash,
    stage_order_observation,
)
from control_tower.models import (
    Order,
    OrderObservation,
    OrderObservationReceipt,
    SourceOrderIdentity,
    Warehouse,
)

AS_OF = datetime(2018, 1, 1, 10, 0, tzinfo=timezone.utc)


def test_replay_identity_uses_structured_components_not_delimiters() -> None:
    first = replay_identity_digest("a|b", "c", "d", "row")
    second = replay_identity_digest("a", "b|c", "d", "row")

    assert first != second


def test_replay_identity_preserves_null_empty_and_nfc_distinctions() -> None:
    assert replay_identity_digest("ns", "id", None, "hash") != replay_identity_digest(
        "ns", "id", "", "hash"
    )
    assert replay_identity_digest("e\u0301", "id", "v", "hash") == replay_identity_digest(
        "é", "id", "v", "hash"
    )


def test_source_row_hash_canonicalizes_decimal_timestamps_and_unicode() -> None:
    first = source_row_hash({"label": "e\u0301", "amount": Decimal("10.00"), "at": AS_OF})
    second = source_row_hash({"at": "2018-01-01T10:00:00Z", "amount": "10", "label": "é"})

    assert first == second


def test_source_identity_is_authoritative_and_observation_identity_excludes_batch() -> None:
    assert {column.name for column in SourceOrderIdentity.__table__.constraints} >= {
        "uq_source_order_identity_namespace_order"
    }
    observation_columns = set(OrderObservation.__table__.columns.keys())
    assert {"source_namespace", "source_order_id", "source_version", "source_row_hash"} <= (
        observation_columns
    )
    assert "batch_id" not in {
        column.name for column in OrderObservation.__table__.constraints if hasattr(column, "name")
    }
    assert "observation_id" in OrderObservationReceipt.__table__.columns


def test_olist_like_partial_order_is_context_incomplete_without_fabrication() -> None:
    assessment = assess_order_observation(
        {
            "source_order_id": "o-1",
            "order_number": "o-1",
            "status": "FULFILLED",
            "region": "SP",
            "ordered_at": AS_OF,
            "promised_at": datetime(2018, 1, 10, tzinfo=timezone.utc),
            "fulfilled_at": datetime(2018, 1, 8, tzinfo=timezone.utc),
            "total_amount": "42.50",
            "currency": None,
            "source_warehouse_id": None,
        },
        source_namespace="olist",
        batch_id="olist-v2",
    )

    assert assessment.status is OrderObservationStatus.CONTEXT_INCOMPLETE
    assert assessment.observation.total_amount == Decimal("42.50")
    assert assessment.observation.currency is None
    assert assessment.observation.source_warehouse_id is None
    assert assessment.capabilities["financial"] is False
    assert assessment.capabilities["warehouse"] == "WAREHOUSE_CONTEXT_UNAVAILABLE"
    assert "MISSING_CURRENCY" in assessment.non_promotion_reasons
    assert assessment.source_status is OrderObservationStatus.SOURCE_VALID


def test_complete_order_observation_is_normalized_and_capable() -> None:
    assessment = assess_order_observation(
        {
            "source_order_id": "o-1",
            "order_number": "ord-1",
            "status": "OPEN",
            "region": "EU",
            "source_warehouse_id": "w-1",
            "ordered_at": "2018-01-01T10:00:00+00:00",
            "promised_at": "2018-01-10T00:00:00+00:00",
            "total_amount": "42.50",
            "currency": "EUR",
        },
        source_namespace="synthetic",
        batch_id="seed-1",
    )

    assert assessment.status is OrderObservationStatus.NORMALIZED
    assert assessment.capabilities == {
        "warehouse": "WAREHOUSE_OBSERVED",
        "financial": True,
        "timing": True,
    }
    assert assessment.non_promotion_reasons == ()


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _complete_row(source_order_id: str = "o-1") -> dict[str, object]:
    return {
        "source_order_id": source_order_id,
        "order_number": f"ord-{source_order_id}",
        "status": "OPEN",
        "region": "EU",
        "source_warehouse_id": "w-1",
        "ordered_at": AS_OF,
        "promised_at": datetime(2018, 1, 10, tzinfo=timezone.utc),
        "total_amount": "42.50",
        "currency": "EUR",
    }


@pytest.mark.filterwarnings("error::sqlalchemy.exc.SAWarning")
def test_staging_is_idempotent_and_namespaced() -> None:
    with _session() as session:
        first = stage_order_observation(
            session, _complete_row(), source_namespace="source-a", batch_id="batch-1"
        )
        same = stage_order_observation(
            session, _complete_row(), source_namespace="source-a", batch_id="batch-1"
        )
        replayed_batch = stage_order_observation(
            session, _complete_row(), source_namespace="source-a", batch_id="batch-2"
        )
        other_namespace = stage_order_observation(
            session, _complete_row(), source_namespace="source-b", batch_id="batch-1"
        )
        session.commit()

        assert first.id == same.id
        assert replayed_batch.id == first.id
        assert first.id is not None
        assert first.replay_identity == ["source-a", "o-1", None, first.source_row_hash]
        assert first.replay_identity_digest == replay_identity_digest(
            "source-a", "o-1", None, first.source_row_hash
        )
        first_receipt = session.scalar(
            select(OrderObservationReceipt).where(
                OrderObservationReceipt.observation_id == first.id,
                OrderObservationReceipt.batch_id == "batch-1",
            )
        )
        assert first_receipt is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(OrderObservationReceipt)
                .where(OrderObservationReceipt.observation_id == first.id)
            )
            == 2
        )
        assert other_namespace.id != first.id
        assert session.scalar(select(func.count()).select_from(OrderObservation)) == 2


def test_complete_observation_promotes_once_and_preserves_strict_projection() -> None:
    with _session() as session:
        session.add(
            Warehouse(
                id=1,
                source_warehouse_id="w-1",
                code="W1",
                name="Warehouse 1",
                region="EU",
                timezone="UTC",
            )
        )
        observation = stage_order_observation(
            session, _complete_row(), source_namespace="synthetic", batch_id="seed-1"
        )
        promoted = promote_order_observation(session, observation)
        repeated = promote_order_observation(session, observation)
        session.commit()

        assert promoted is not None
        assert repeated is not None and repeated.id == promoted.id
        assert observation.status is OrderObservationStatus.PROMOTED
        assert session.scalar(select(func.count()).select_from(Order)) == 1
        assert promoted.warehouse_id == 1
        assert promoted.currency == "EUR"
        assert promoted.total_amount == Decimal("42.50")


def test_promotions_separate_namespace_and_id_values_that_share_a_delimited_form() -> None:
    with _session() as session:
        session.add(
            Warehouse(
                id=1,
                source_warehouse_id="w-1",
                code="W1",
                name="Warehouse 1",
                region="EU",
                timezone="UTC",
            )
        )
        first = stage_order_observation(
            session,
            {**_complete_row("c"), "order_number": "shared-number"},
            source_namespace="a:b",
            batch_id="batch-1",
        )
        second = stage_order_observation(
            session,
            {**_complete_row("b:c"), "order_number": "shared-number"},
            source_namespace="a",
            batch_id="batch-1",
        )

        first_order = promote_order_observation(session, first)
        second_order = promote_order_observation(session, second)
        first_replay = promote_order_observation(session, first)
        second_replay = promote_order_observation(session, second)
        session.commit()

        assert first_order is not None
        assert second_order is not None
        assert first_order.id != second_order.id
        assert first_replay is not None and first_replay.id == first_order.id
        assert second_replay is not None and second_replay.id == second_order.id
        assert first.promoted_order_id == first_order.id
        assert second.promoted_order_id == second_order.id
        assert session.scalar(select(func.count()).select_from(Order)) == 2


def test_observation_json_columns_use_jsonb_on_postgresql_and_json_on_sqlite() -> None:
    for column_name in ("capabilities", "non_promotion_reasons"):
        column = OrderObservation.__table__.c[column_name]

        assert isinstance(column.type.dialect_impl(postgresql.dialect()), JSONB)
        assert isinstance(column.type.dialect_impl(sqlite.dialect()), JSON)


def test_amount_without_currency_and_unknown_warehouse_never_promote() -> None:
    with _session() as session:
        missing_currency = stage_order_observation(
            session,
            {**_complete_row("missing-currency"), "currency": None},
            source_namespace="olist",
            batch_id="b1",
        )
        unknown_warehouse = stage_order_observation(
            session,
            {**_complete_row("unknown-warehouse"), "source_warehouse_id": "w-unknown"},
            source_namespace="olist",
            batch_id="b2",
        )
        assert promote_order_observation(session, missing_currency) is None
        assert promote_order_observation(session, unknown_warehouse) is None
        session.commit()

        assert session.scalar(select(func.count()).select_from(Order)) == 0
        assert "AMOUNT_WITHOUT_CURRENCY" in missing_currency.non_promotion_reasons
        assert "UNKNOWN_WAREHOUSE" in unknown_warehouse.non_promotion_reasons


def test_malformed_negative_and_conflicting_values_are_rejected_invalid() -> None:
    for raw in (
        {**_complete_row(), "total_amount": "not-money"},
        {**_complete_row(), "total_amount": "-1"},
        {**_complete_row(), "total_amount": "1", "amount": "2"},
        {**_complete_row(), "fulfilled_at": "2017-01-01T00:00:00+00:00"},
    ):
        assessment = assess_order_observation(raw, source_namespace="source", batch_id="invalid")
        assert assessment.status is OrderObservationStatus.REJECTED_INVALID
        assert assessment.source_status is OrderObservationStatus.REJECTED_INVALID


def test_capability_assessment_marks_unknown_warehouse_without_creating_one() -> None:
    assessment = assess_order_observation(
        _complete_row(),
        source_namespace="source",
        batch_id="b1",
        known_warehouse_ids={"w-known"},
    )

    assert assessment.status is OrderObservationStatus.CONTEXT_INCOMPLETE
    assert assessment.capabilities["warehouse"] == "WAREHOUSE_UNKNOWN"
    assert assessment.capabilities["financial"] is True
    assert assessment.non_promotion_reasons == ("UNKNOWN_WAREHOUSE",)


def test_invalid_observation_is_staged_as_rejected_instead_of_raising() -> None:
    with _session() as session:
        observation = stage_order_observation(
            session,
            {**_complete_row(), "status": "not-a-status"},
            source_namespace="source",
            batch_id="invalid-status",
        )

        assert observation.status is OrderObservationStatus.REJECTED_INVALID
        assert "MALFORMED_STATUS" in observation.non_promotion_reasons


def test_unversioned_conflict_preserves_existing_context_reasons() -> None:
    with _session() as session:
        first = stage_order_observation(
            session,
            {**_complete_row(), "currency": None},
            source_namespace="source",
            batch_id="b1",
        )
        second = stage_order_observation(
            session,
            {**_complete_row(), "total_amount": "43.50", "currency": None},
            source_namespace="source",
            batch_id="b2",
        )
        first_facts = first.source_facts
        first_hash = first.source_row_hash
        first_replay_identity = first.replay_identity

        assert promote_order_observation(session, first) is None
        assert first.status is OrderObservationStatus.CONFLICT_BLOCKED
        assert first.non_promotion_reasons == [
            "MISSING_CURRENCY",
            "AMOUNT_WITHOUT_CURRENCY",
            "UNVERSIONED_CONTENT_CONFLICT",
        ]
        assert first.source_facts == first_facts
        assert first.source_row_hash == first_hash
        assert first.replay_identity == first_replay_identity

        assert promote_order_observation(session, second) is None
        assert second.status is OrderObservationStatus.CONFLICT_BLOCKED
        assert second.non_promotion_reasons == [
            "MISSING_CURRENCY",
            "AMOUNT_WITHOUT_CURRENCY",
            "UNVERSIONED_CONTENT_CONFLICT",
        ]


def test_standalone_context_incomplete_observation_preserves_status_and_reasons() -> None:
    with _session() as session:
        session.add(
            Warehouse(
                id=1,
                source_warehouse_id="w-1",
                code="W1",
                name="Warehouse 1",
                region="EU",
                timezone="UTC",
            )
        )
        observation = stage_order_observation(
            session,
            {**_complete_row(), "currency": None},
            source_namespace="source",
            batch_id="b1",
        )

        assert promote_order_observation(session, observation) is None
        assert observation.status is OrderObservationStatus.CONTEXT_INCOMPLETE
        assert observation.non_promotion_reasons == [
            "MISSING_CURRENCY",
            "AMOUNT_WITHOUT_CURRENCY",
        ]


def test_versioned_same_version_conflict_is_blocked() -> None:
    with _session() as session:
        first = stage_order_observation(
            session,
            _complete_row(),
            source_namespace="source",
            source_version="v1",
            batch_id="b1",
        )
        second = stage_order_observation(
            session,
            {**_complete_row(), "total_amount": "43.50"},
            source_namespace="source",
            source_version="v1",
            batch_id="b2",
        )

        assert promote_order_observation(session, first) is None
        assert first.status is OrderObservationStatus.CONFLICT_BLOCKED
        assert first.non_promotion_reasons == ["SOURCE_VERSION_CONTENT_CONFLICT"]
        assert promote_order_observation(session, second) is None
        assert second.status is OrderObservationStatus.CONFLICT_BLOCKED
        assert second.non_promotion_reasons == ["SOURCE_VERSION_CONTENT_CONFLICT"]


def test_different_versions_without_comparator_are_not_ordered() -> None:
    with _session() as session:
        session.add(
            Warehouse(
                id=1,
                source_warehouse_id="w-1",
                code="W1",
                name="Warehouse 1",
                region="EU",
                timezone="UTC",
            )
        )
        first = stage_order_observation(
            session,
            _complete_row(),
            source_namespace="source",
            source_version="v1",
            batch_id="b1",
        )
        second = stage_order_observation(
            session,
            {**_complete_row(), "total_amount": "43.50"},
            source_namespace="source",
            source_version="v2",
            batch_id="b2",
        )

        assert promote_order_observation(session, first) is None
        assert first.status is OrderObservationStatus.CONFLICT_BLOCKED
        assert first.non_promotion_reasons == ["SOURCE_VERSION_ORDER_UNPROVEN"]
        assert promote_order_observation(session, second) is None
        assert second.status is OrderObservationStatus.CONFLICT_BLOCKED
        assert second.non_promotion_reasons == ["SOURCE_VERSION_ORDER_UNPROVEN"]


def test_explicit_version_comparator_promotes_newer_projection_without_duplicate_order() -> None:
    with _session() as session:
        session.add(
            Warehouse(
                id=1,
                source_warehouse_id="w-1",
                code="W1",
                name="Warehouse 1",
                region="EU",
                timezone="UTC",
            )
        )
        first = stage_order_observation(
            session,
            {**_complete_row(), "total_amount": "10.00"},
            source_namespace="source",
            source_version="v1",
            batch_id="b1",
        )
        first_order = promote_order_observation(session, first)
        newer = stage_order_observation(
            session,
            {**_complete_row(), "total_amount": "20.00"},
            source_namespace="source",
            source_version="v2",
            batch_id="b2",
        )

        updated_order = promote_order_observation(
            session,
            newer,
            version_comparator=lambda current, incoming: current == "v1" and incoming == "v2",
        )
        session.commit()

        assert first_order is not None
        assert updated_order is not None
        assert updated_order.id == first_order.id
        assert updated_order.total_amount == Decimal("20.00")
        assert updated_order.projected_source_version == "v2"
        assert newer.status is OrderObservationStatus.PROMOTED
        assert session.scalar(select(func.count()).select_from(Order)) == 1


def test_missing_source_identity_is_rejected_before_identity_creation() -> None:
    assessment = assess_order_observation(
        _complete_row("  "), source_namespace="source", batch_id="missing"
    )
    assert assessment.status is OrderObservationStatus.REJECTED_INVALID
    assert "MISSING_SOURCE_ORDER_ID" in assessment.invalid_reasons

    with _session() as session:
        with pytest.raises(ValueError, match="REJECTED_INVALID.*MISSING_SOURCE_ORDER_ID"):
            stage_order_observation(
                session,
                _complete_row("  "),
                source_namespace="source",
                batch_id="missing",
            )
        assert session.scalar(select(func.count()).select_from(SourceOrderIdentity)) == 0


def test_observation_evidence_is_immutable_at_sqlite_orm_and_core_boundaries() -> None:
    with _session() as session:
        observation = stage_order_observation(
            session, _complete_row(), source_namespace="source", batch_id="b1"
        )
        session.flush()
        observation.source_version = "mutated"
        with pytest.raises(ValueError, match="immutable"):
            session.flush()
        session.rollback()

        with pytest.raises((ValueError, IntegrityError), match="immutable|append-only"):
            session.execute(
                update(OrderObservation)
                .where(OrderObservation.id == observation.id)
                .values(source_row_hash="changed")
            )
        session.rollback()
        with pytest.raises((ValueError, IntegrityError), match="immutable|append-only"):
            session.execute(
                text("UPDATE order_observations SET source_version = 'changed' WHERE id = :id"),
                {"id": observation.id},
            )
        session.rollback()
        with pytest.raises((ValueError, IntegrityError), match="immutable|append-only"):
            session.execute(delete(OrderObservation).where(OrderObservation.id == observation.id))
        session.rollback()


def test_sqlite_observation_trigger_rejects_direct_evidence_mutation() -> None:
    with _session() as session:
        observation = stage_order_observation(
            session, _complete_row(), source_namespace="source", batch_id="b1"
        )
        session.commit()
        with pytest.raises(IntegrityError, match="immutable"):
            session.connection().exec_driver_sql(
                "UPDATE order_observations SET source_facts = '{\"changed\": true}' WHERE id = ?",
                (observation.id,),
            )
        session.rollback()


def test_warehouse_is_observed_until_a_real_registry_row_validates_it() -> None:
    assessment = assess_order_observation(_complete_row(), source_namespace="source", batch_id="b1")
    assert assessment.capabilities["warehouse"] == "WAREHOUSE_OBSERVED"
    with _session() as session:
        observation = stage_order_observation(
            session, _complete_row(), source_namespace="source", batch_id="b1"
        )
        assert promote_order_observation(session, observation) is None
        assert observation.capabilities["warehouse"] == "WAREHOUSE_UNKNOWN"
        assert session.scalar(select(func.count()).select_from(Order)) == 0


def test_version_gate_uses_explicit_outcomes_and_current_pointer() -> None:
    with _session() as session:
        session.add(
            Warehouse(
                id=1, source_warehouse_id="w-1", code="W1", name="One", region="EU", timezone="UTC"
            )
        )
        first = stage_order_observation(
            session,
            {**_complete_row(), "total_amount": "10.00"},
            source_namespace="source",
            source_version="v1",
            batch_id="b1",
        )
        first_order = promote_order_observation(session, first)
        newer = stage_order_observation(
            session,
            {**_complete_row(), "total_amount": "20.00"},
            source_namespace="source",
            source_version="v2",
            batch_id="b2",
        )
        updated = promote_order_observation(
            session, newer, version_comparator=lambda current, incoming: "INCOMING_NEWER"
        )
        assert first_order is not None and updated is not None
        assert updated.current_observation_id == newer.id
        assert first_order.current_observation_id == newer.id
        assert first.superseded_by_observation_id == newer.id
        stale = stage_order_observation(
            session,
            {**_complete_row(), "total_amount": "5.00"},
            source_namespace="source",
            source_version="v1-old",
            batch_id="b3",
        )
        before = updated.current_observation_id
        assert (
            promote_order_observation(
                session, stale, version_comparator=lambda current, incoming: "CURRENT_NEWER"
            )
            is None
        )
        assert updated.current_observation_id == before
        assert stale.source_row_hash is not None


def test_warehouse_identity_mismatch_is_not_a_projection_match() -> None:
    with _session() as session:
        session.add_all(
            [
                Warehouse(
                    id=1,
                    source_warehouse_id="w-1",
                    code="W1",
                    name="One",
                    region="EU",
                    timezone="UTC",
                ),
                Warehouse(
                    id=2,
                    source_warehouse_id="w-2",
                    code="W2",
                    name="Two",
                    region="EU",
                    timezone="UTC",
                ),
            ]
        )
        first = stage_order_observation(
            session, _complete_row(), source_namespace="source", source_version="v1", batch_id="b1"
        )
        order = promote_order_observation(session, first)
        incoming = stage_order_observation(
            session,
            {**_complete_row(), "source_warehouse_id": "w-2"},
            source_namespace="source",
            source_version="v2",
            batch_id="b2",
        )
        assert order is not None
        assert (
            promote_order_observation(
                session, incoming, version_comparator=lambda current, new: "SAME"
            )
            is None
        )
        assert order.warehouse_id == 1
        assert order.current_observation_id == first.id


def test_context_incomplete_observation_retries_after_warehouse_is_created() -> None:
    with _session() as session:
        observation = stage_order_observation(
            session, _complete_row(), source_namespace="source", batch_id="missing-warehouse"
        )
        source_facts = observation.source_facts
        source_row_hash = observation.source_row_hash

        assert promote_order_observation(session, observation) is None
        assert observation.status is OrderObservationStatus.CONTEXT_INCOMPLETE
        assert observation.capabilities["warehouse"] == "WAREHOUSE_UNKNOWN"
        assert session.scalar(select(func.count()).select_from(Order)) == 0

        observation_id = observation.id
        session.commit()
        session.expire_all()
        observation = session.get(OrderObservation, observation_id)
        assert observation is not None

        session.add(
            Warehouse(
                id=1,
                source_warehouse_id="w-1",
                code="W1",
                name="Warehouse 1",
                region="EU",
                timezone="UTC",
            )
        )
        promoted = promote_order_observation(session, observation)
        session.commit()

        assert promoted is not None
        assert observation.status is OrderObservationStatus.PROMOTED
        assert observation.capabilities["warehouse"] == "WAREHOUSE_VALIDATED"
        assert promoted.current_observation_id == observation.id
        assert observation.source_facts == source_facts
        assert observation.source_row_hash == source_row_hash
        assert session.scalar(select(func.count()).select_from(Order)) == 1


def test_context_incomplete_observation_stays_blocked_without_warehouse() -> None:
    with _session() as session:
        observation = stage_order_observation(
            session, _complete_row("still-missing"), source_namespace="source", batch_id="missing"
        )

        assert promote_order_observation(session, observation) is None
        assert promote_order_observation(session, observation) is None
        session.commit()

        assert observation.status is OrderObservationStatus.CONTEXT_INCOMPLETE
        assert observation.capabilities["warehouse"] == "WAREHOUSE_UNKNOWN"
        assert session.scalar(select(func.count()).select_from(Order)) == 0


def test_retrying_historical_promoted_observation_is_not_current() -> None:
    with _session() as session:
        session.add(
            Warehouse(
                id=1,
                source_warehouse_id="w-1",
                code="W1",
                name="Warehouse 1",
                region="EU",
                timezone="UTC",
            )
        )
        first = stage_order_observation(
            session,
            {**_complete_row(), "total_amount": "10.00"},
            source_namespace="source",
            source_version="v1",
            batch_id="b1",
        )
        first_order = promote_order_observation(session, first)
        newer = stage_order_observation(
            session,
            {**_complete_row(), "total_amount": "20.00"},
            source_namespace="source",
            source_version="v2",
            batch_id="b2",
        )
        current_order = promote_order_observation(
            session,
            newer,
            version_comparator=lambda current, incoming: current == "v1" and incoming == "v2",
        )
        assert first_order is not None
        assert current_order is not None
        assert current_order.id == first_order.id
        assert current_order.current_observation_id == newer.id

        first_id = first.id
        newer_id = newer.id
        current_order_id = current_order.id
        session.commit()
        session.expire_all()
        first = session.get(OrderObservation, first_id)
        newer = session.get(OrderObservation, newer_id)
        current_order = session.get(Order, current_order_id)
        assert first is not None
        assert newer is not None
        assert current_order is not None

        historical_retry = promote_order_observation(
            session,
            first,
            version_comparator=lambda current, incoming: current == "v1" and incoming == "v2",
        )
        session.commit()

        assert historical_retry is None
        assert first.status is OrderObservationStatus.PROMOTED
        assert first.promoted_order_id == current_order.id
        assert first.superseded_by_observation_id == newer.id
        assert current_order.current_observation_id == newer.id
        assert session.scalar(select(func.count()).select_from(Order)) == 1
        assert session.scalar(select(func.count()).select_from(OrderObservation)) == 2
