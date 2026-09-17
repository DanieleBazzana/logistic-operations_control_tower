from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from control_tower.db import Base
from control_tower.exceptions import service as exception_service
from control_tower.models import (
    DatasetActivation,
    DatasetVersion,
    Order,
    Warehouse,
    explicit_dataset_scope,
)
from control_tower.replacement.service import (
    LEGACY_AS_OF,
    LEGACY_SEED,
    DatasetIdentityConflict,
    activate_dataset,
    backfill_m076,
    dataset_identity,
    ensure_dataset_version,
    get_active_dataset_version,
    rollback_dataset,
)


def _session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_identity_is_deterministic_and_includes_content() -> None:
    left = dataset_identity(
        dataset_key="M07.7", manifest_identity="manifest-a", seed=LEGACY_SEED,
        as_of=LEGACY_AS_OF, generator_revision="generator-r1", content_hash="content-a"
    )
    right = dataset_identity(
        dataset_key="M07.7", manifest_identity="manifest-a", seed=LEGACY_SEED,
        as_of=LEGACY_AS_OF, generator_revision="generator-r1", content_hash="content-a"
    )
    changed = dataset_identity(
        dataset_key="M07.7", manifest_identity="manifest-a", seed=LEGACY_SEED,
        as_of=LEGACY_AS_OF, generator_revision="generator-r1", content_hash="content-b"
    )
    assert left == right
    assert left != changed


def test_same_identity_same_content_is_idempotent_and_changed_content_fails_closed() -> None:
    with _session() as session:
        first = ensure_dataset_version(
            session,
            dataset_key="M07.7",
            manifest_identity="manifest-a",
            content_hash="content-a",
            seed=LEGACY_SEED,
            as_of=LEGACY_AS_OF,
            generator_revision="generator-r1",
        )
        second = ensure_dataset_version(
            session,
            dataset_key="M07.7",
            manifest_identity="manifest-a",
            content_hash="content-a",
            seed=LEGACY_SEED,
            as_of=LEGACY_AS_OF,
            generator_revision="generator-r1",
        )
        assert first.id == second.id
        with pytest.raises(DatasetIdentityConflict, match="different content"):
            ensure_dataset_version(
                session,
                dataset_key="M07.7",
                manifest_identity="manifest-a",
                content_hash="content-b",
                seed=LEGACY_SEED,
                as_of=LEGACY_AS_OF,
                generator_revision="generator-r1",
            )


def test_backfill_is_non_destructive_and_activation_is_pointer_only() -> None:
    with _session() as session:
        order = Order(
            id=1,
            source_order_id="O1",
            order_number="1001",
            status="OPEN",
            region="EU",
            warehouse_id=1,
            ordered_at=LEGACY_AS_OF,
            promised_at=LEGACY_AS_OF,
            total_amount=0,
        )
        session.add(order)
        session.flush()
        legacy = backfill_m076(session)
        assert legacy.dataset_key == "M07.6"
        assert session.scalar(select(Order.dataset_version_id)) == legacy.id
        assert session.scalar(select(DatasetActivation.id)) == 1
        assert get_active_dataset_version(session).id == legacy.id

        candidate = ensure_dataset_version(
            session,
            dataset_key="M07.7",
            manifest_identity="manifest-b",
            content_hash="content-b",
            seed=LEGACY_SEED,
            as_of=LEGACY_AS_OF,
            generator_revision="generator-r2",
        )
        candidate.status = "READY"
        session.flush()
        activate_dataset(session, candidate.id)
        assert get_active_dataset_version(session).id == candidate.id
        assert session.get(DatasetVersion, legacy.id).status == "RETIRED"
        rollback_dataset(session, candidate.id)
        assert get_active_dataset_version(session).id == legacy.id
        assert session.get(DatasetVersion, candidate.id).status == "RETIRED"


def test_as_of_contract_is_fixed() -> None:
    assert LEGACY_AS_OF == datetime(2025, 3, 7, 18, tzinfo=timezone.utc)
    assert LEGACY_SEED == 20250301


def test_legacy_scoped_orm_write_inherits_active_dataset_before_flush() -> None:
    with _session() as session:
        session.add(
            DatasetVersion(
                id=1,
                dataset_key="M07.6",
                identity_hash="legacy-identity",
                manifest_identity="legacy-manifest",
                content_hash="legacy-content",
                seed=LEGACY_SEED,
                as_of=LEGACY_AS_OF,
                generator_revision="m07.6",
                status="ACTIVE",
            )
        )
        session.add(DatasetActivation(id=1, active_dataset_version_id=1))
        warehouse = Warehouse(
            id=1,
            source_warehouse_id="legacy-warehouse",
            code="LEGACY",
            name="Legacy Warehouse",
            region="EU",
            timezone="UTC",
        )
        session.add(warehouse)

        session.flush()

        assert warehouse.dataset_version_id == 1


def test_candidate_scoped_orm_write_fails_closed_without_explicit_dataset() -> None:
    with _session() as session:
        session.add(
            DatasetVersion(
                id=1,
                dataset_key="M07.6",
                identity_hash="candidate-identity",
                manifest_identity="candidate-manifest",
                content_hash="candidate-content",
                seed=LEGACY_SEED,
                as_of=LEGACY_AS_OF,
                generator_revision="candidate",
                status="ACTIVE",
            )
        )
        session.add(DatasetActivation(id=1, active_dataset_version_id=1))
        session.add(
            Warehouse(
                id=1,
                source_warehouse_id="candidate-warehouse",
                code="CANDIDATE",
                name="Candidate Warehouse",
                region="EU",
                timezone="UTC",
            )
        )

        with explicit_dataset_scope(session), pytest.raises(
            ValueError, match="dataset_version_id is required"
        ):
            session.flush()


@pytest.mark.parametrize(
    "entry_point", ["service.detect", "detect_and_persist", "persist_detections"]
)
def test_exception_entry_points_require_dataset_id_in_explicit_scope(
    monkeypatch: pytest.MonkeyPatch, entry_point: str
) -> None:
    with _session() as session:
        session.add(
            DatasetVersion(
                id=1,
                dataset_key="M07.6",
                identity_hash="legacy-identity",
                manifest_identity="legacy-manifest",
                content_hash="legacy-content",
                seed=LEGACY_SEED,
                as_of=LEGACY_AS_OF,
                generator_revision="m07.6",
                status="ACTIVE",
            )
        )
        session.add(DatasetActivation(id=1, active_dataset_version_id=1))
        session.flush()
        monkeypatch.setattr(exception_service, "detect_all", lambda *args, **kwargs: ())

        with explicit_dataset_scope(session), pytest.raises(
            ValueError, match="dataset_version_id is required"
        ):
            if entry_point == "service.detect":
                exception_service.ExceptionService(session).detect(LEGACY_AS_OF)
            elif entry_point == "detect_and_persist":
                exception_service.detect_and_persist(session, LEGACY_AS_OF)
            else:
                exception_service.persist_detections(session, (), LEGACY_AS_OF)


def test_exception_detection_and_persistence_use_active_or_explicit_dataset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _session() as session:
        session.add(
            DatasetVersion(
                id=1,
                dataset_key="M07.6",
                identity_hash="legacy-identity",
                manifest_identity="legacy-manifest",
                content_hash="legacy-content",
                seed=LEGACY_SEED,
                as_of=LEGACY_AS_OF,
                generator_revision="m07.6",
                status="ACTIVE",
            )
        )
        session.add(DatasetActivation(id=1, active_dataset_version_id=1))
        session.flush()
        calls: list[tuple[str, int | None]] = []

        def fake_detect_all(*args, **kwargs):
            calls.append(("detect", kwargs["dataset_version_id"]))
            return ()

        def fake_persist_detections(*args, **kwargs):
            calls.append(("persist", kwargs["dataset_version_id"]))
            return object()

        monkeypatch.setattr(exception_service, "detect_all", fake_detect_all)
        monkeypatch.setattr(exception_service, "persist_detections", fake_persist_detections)

        exception_service.ExceptionService(session).detect(LEGACY_AS_OF)
        assert calls == [("detect", 1), ("persist", 1)]

        calls.clear()
        with explicit_dataset_scope(session):
            exception_service.ExceptionService(session).detect(
                LEGACY_AS_OF, dataset_version_id=2
            )
        assert calls == [("detect", 2), ("persist", 2)]
