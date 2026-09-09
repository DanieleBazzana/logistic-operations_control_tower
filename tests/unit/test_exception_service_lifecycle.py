from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import delete, event, func, select, update
from tests.unit.test_exception_rules import AS_OF, session_with_fixture

from control_tower.bootstrap import _seed_lifecycle_sample
from control_tower.config import Settings
from control_tower.enums import ExceptionStatus, ExceptionType
from control_tower.exceptions.lifecycle import InvalidTransition, transition_exception
from control_tower.exceptions.service import ExceptionService
from control_tower.models import ExceptionHistory, ExceptionRecord, Order


@event.listens_for(ExceptionHistory, "before_insert")
def _sqlite_history_identity(mapper, connection, target) -> None:
    if target.id is None and connection.dialect.name == "sqlite":
        target.id = connection.scalar(select(func.max(ExceptionHistory.id))) or 0
        target.id += 1


def test_detection_persists_one_initial_history_row_per_new_exception() -> None:
    session = session_with_fixture()
    service = ExceptionService(
        session,
        Settings(safety_stock=Decimal("20"), inventory_mismatch_tolerance=Decimal("1")),
    )

    result = service.detect(AS_OF)
    session.commit()

    assert result.created == 6
    assert result.updated == 0
    assert session.scalar(select(func.count()).select_from(ExceptionRecord)) == 6
    assert session.scalar(select(func.count()).select_from(ExceptionHistory)) == 6
    assert all(row.actor == "exception-engine" for row in session.scalars(select(ExceptionHistory)))
    assert {row.exception_type for row in session.scalars(select(ExceptionRecord))} == set(
        ExceptionType
    )


def test_bootstrap_lifecycle_sample_is_deterministic_and_idempotent() -> None:
    session = session_with_fixture()
    ExceptionService(session).detect(AS_OF)
    session.commit()

    _seed_lifecycle_sample(session, changed_at=AS_OF)
    session.commit()

    expected_statuses = {
        ExceptionType.STOCKOUT_RISK: ExceptionStatus.ACKNOWLEDGED,
        ExceptionType.INVENTORY_MISMATCH: ExceptionStatus.IN_PROGRESS,
        ExceptionType.SUPPLIER_DELAY: ExceptionStatus.RESOLVED,
        ExceptionType.SHIPMENT_DELAY: ExceptionStatus.DISMISSED,
    }
    assert {
        record.exception_type: record.status
        for record in session.scalars(select(ExceptionRecord))
        if record.exception_type in expected_statuses
    } == expected_statuses
    assert {
        status: session.scalar(
            select(func.count())
            .select_from(ExceptionRecord)
            .where(ExceptionRecord.status == status)
        )
        for status in ExceptionStatus
    } == {
        ExceptionStatus.OPEN: 2,
        ExceptionStatus.ACKNOWLEDGED: 1,
        ExceptionStatus.IN_PROGRESS: 1,
        ExceptionStatus.RESOLVED: 1,
        ExceptionStatus.DISMISSED: 1,
    }
    history_count = session.scalar(select(func.count()).select_from(ExceptionHistory))

    _seed_lifecycle_sample(session, changed_at=AS_OF)
    session.commit()

    assert session.scalar(select(func.count()).select_from(ExceptionHistory)) == history_count


def _add_lower_sort_duplicate_records(session) -> None:
    """Add a second OPEN finding for every detected type with a larger DB id."""

    records = list(session.scalars(select(ExceptionRecord).order_by(ExceptionRecord.id)))
    session.add_all(
        [
            ExceptionRecord(
                id=100 + index,
                deduplication_key=f"{record.deduplication_key}:duplicate",
                exception_type=record.exception_type,
                issue_key=f"AAA:{record.issue_key}",
                entity_type=record.entity_type,
                entity_id=f"AAA:{record.entity_id}",
                severity=record.severity,
                status=ExceptionStatus.OPEN,
                detected_at=record.detected_at,
                expected_resolution=record.expected_resolution,
                business_impact=record.business_impact,
                revenue_at_risk=record.revenue_at_risk,
                orders_affected=record.orders_affected,
                root_cause=record.root_cause,
                recommended_action=record.recommended_action,
                confidence=record.confidence,
                warehouse_id=record.warehouse_id,
                product_id=record.product_id,
            )
            for index, record in enumerate(records)
        ]
    )
    session.flush()


def test_bootstrap_lifecycle_sample_selects_one_stable_representative_per_bucket() -> None:
    session = session_with_fixture()
    ExceptionService(session).detect(AS_OF)
    session.commit()
    _add_lower_sort_duplicate_records(session)
    session.commit()

    _seed_lifecycle_sample(session, changed_at=AS_OF)
    session.commit()

    assert session.scalar(select(func.count()).select_from(ExceptionRecord)) == 12
    assert {
        status: session.scalar(
            select(func.count())
            .select_from(ExceptionRecord)
            .where(ExceptionRecord.status == status)
        )
        for status in ExceptionStatus
    } == {
        ExceptionStatus.OPEN: 8,
        ExceptionStatus.ACKNOWLEDGED: 1,
        ExceptionStatus.IN_PROGRESS: 1,
        ExceptionStatus.RESOLVED: 1,
        ExceptionStatus.DISMISSED: 1,
    }
    selected = {
        record.exception_type: record.issue_key
        for record in session.scalars(select(ExceptionRecord))
        if record.status != ExceptionStatus.OPEN
    }
    assert selected == {
        ExceptionType.STOCKOUT_RISK: "AAA:PRODUCT_WAREHOUSE:P1:W1",
        ExceptionType.INVENTORY_MISMATCH: "AAA:PRODUCT_WAREHOUSE:P1:W1",
        ExceptionType.SUPPLIER_DELAY: "AAA:PO:PO1",
        ExceptionType.SHIPMENT_DELAY: "AAA:SHIPMENT:S1",
    }


def test_bootstrap_lifecycle_selection_is_stable_when_record_ids_and_order_differ() -> None:
    def snapshot(reverse: bool) -> dict[tuple[ExceptionType, str], ExceptionStatus]:
        session = session_with_fixture()
        ExceptionService(session).detect(AS_OF)
        session.commit()
        records = list(session.scalars(select(ExceptionRecord).order_by(ExceptionRecord.id)))
        duplicates = [
            ExceptionRecord(
                id=100 + (len(records) - index if reverse else index),
                deduplication_key=f"stable:{record.deduplication_key}",
                exception_type=record.exception_type,
                issue_key=f"000:{record.issue_key}",
                entity_type=record.entity_type,
                entity_id=f"000:{record.entity_id}",
                severity=record.severity,
                status=ExceptionStatus.OPEN,
                detected_at=record.detected_at,
                expected_resolution=record.expected_resolution,
                business_impact=record.business_impact,
                revenue_at_risk=record.revenue_at_risk,
                orders_affected=record.orders_affected,
                root_cause=record.root_cause,
                recommended_action=record.recommended_action,
                confidence=record.confidence,
                warehouse_id=record.warehouse_id,
                product_id=record.product_id,
            )
            for index, record in enumerate(records)
        ]
        session.add_all(list(reversed(duplicates)) if reverse else duplicates)
        session.flush()
        _seed_lifecycle_sample(session, changed_at=AS_OF)
        session.commit()
        return {
            (record.exception_type, record.issue_key): record.status
            for record in session.scalars(select(ExceptionRecord))
        }

    first = snapshot(False)
    second = snapshot(True)
    assert first == second
    assert {
        key
        for key, status in first.items()
        if status != ExceptionStatus.OPEN
    } == {
        (ExceptionType.STOCKOUT_RISK, "000:PRODUCT_WAREHOUSE:P1:W1"),
        (ExceptionType.INVENTORY_MISMATCH, "000:PRODUCT_WAREHOUSE:P1:W1"),
        (ExceptionType.SUPPLIER_DELAY, "000:PO:PO1"),
        (ExceptionType.SHIPMENT_DELAY, "000:SHIPMENT:S1"),
    }


def test_bootstrap_lifecycle_sample_preserves_manual_state_and_history_on_rerun() -> None:
    session = session_with_fixture()
    ExceptionService(session).detect(AS_OF)
    session.commit()
    _add_lower_sort_duplicate_records(session)
    session.commit()
    record = session.scalar(
        select(ExceptionRecord).where(
            ExceptionRecord.exception_type == ExceptionType.STOCKOUT_RISK,
            ExceptionRecord.issue_key == "AAA:PRODUCT_WAREHOUSE:P1:W1",
        )
    )
    assert record is not None
    transition_exception(session, record.id, ExceptionStatus.ACKNOWLEDGED, actor="operator")
    session.commit()
    before_history = [
        (row.from_status, row.to_status, row.actor)
        for row in session.scalars(
            select(ExceptionHistory).where(ExceptionHistory.exception_id == record.id)
        )
    ]

    _seed_lifecycle_sample(session, changed_at=AS_OF)
    session.commit()

    refreshed = session.get(ExceptionRecord, record.id)
    assert refreshed is not None
    assert refreshed.status == ExceptionStatus.ACKNOWLEDGED
    assert [
        (row.from_status, row.to_status, row.actor)
        for row in session.scalars(
            select(ExceptionHistory).where(ExceptionHistory.exception_id == record.id)
        )
    ] == before_history


def test_active_rerun_updates_derived_fields_without_history_or_status_change() -> None:
    session = session_with_fixture()
    service = ExceptionService(
        session, Settings(safety_stock=Decimal("20"), inventory_mismatch_tolerance=Decimal("1"))
    )
    service.detect(AS_OF)
    session.commit()
    record = session.scalar(
        select(ExceptionRecord).where(
            ExceptionRecord.exception_type == ExceptionType.SLA_BREACH_RISK
        )
    )
    assert record is not None
    detected_at = record.detected_at
    transition_exception(
        session,
        record.id,
        ExceptionStatus.ACKNOWLEDGED,
        actor="operator",
        changed_at=AS_OF,
    )
    session.commit()

    second = service.detect(AS_OF)
    session.commit()

    assert second.created == 0
    assert second.updated == 6
    refreshed = session.get(ExceptionRecord, record.id)
    assert refreshed is not None
    assert refreshed.status == ExceptionStatus.ACKNOWLEDGED
    assert refreshed.detected_at == detected_at
    ack_history = session.scalar(
        select(ExceptionHistory).where(
            ExceptionHistory.exception_id == record.id,
            ExceptionHistory.to_status == ExceptionStatus.ACKNOWLEDGED,
        )
    )
    assert ack_history is not None
    assert ack_history.actor == "operator"
    assert session.scalar(select(func.count()).select_from(ExceptionHistory)) == 7


def test_resolved_same_fingerprint_is_not_recreated_but_changed_fingerprint_is() -> None:
    session = session_with_fixture()
    service = ExceptionService(
        session, Settings(safety_stock=Decimal("20"), inventory_mismatch_tolerance=Decimal("1"))
    )
    service.detect(AS_OF)
    session.commit()
    record = session.scalar(
        select(ExceptionRecord).where(
            ExceptionRecord.exception_type == ExceptionType.SLA_BREACH_RISK
        )
    )
    assert record is not None
    transition_exception(
        session, record.id, ExceptionStatus.ACKNOWLEDGED, actor="operator", changed_at=AS_OF
    )
    transition_exception(
        session, record.id, ExceptionStatus.IN_PROGRESS, actor="operator", changed_at=AS_OF
    )
    transition_exception(
        session,
        record.id,
        ExceptionStatus.RESOLVED,
        actor="operator",
        reason="delivered",
        changed_at=AS_OF,
    )
    session.commit()

    same = service.detect(AS_OF)
    assert same.skipped >= 1
    session.commit()
    assert session.scalar(select(func.count()).select_from(ExceptionRecord)) == 6

    order = session.get(Order, 1)
    assert order is not None
    order.promised_at = AS_OF + timedelta(hours=1)
    session.commit()
    changed = service.detect(AS_OF)
    session.commit()
    assert changed.created >= 1
    assert (
        session.scalar(
            select(func.count())
            .select_from(ExceptionRecord)
            .where(ExceptionRecord.exception_type == ExceptionType.SLA_BREACH_RISK)
        )
        == 2
    )


def test_resolved_shortage_ignores_safety_stock_only_configuration_changes() -> None:
    session = session_with_fixture()
    first_service = ExceptionService(
        session, Settings(safety_stock=Decimal("20"), inventory_mismatch_tolerance=Decimal("1"))
    )
    first_service.detect(AS_OF)
    session.commit()
    record = session.scalar(
        select(ExceptionRecord).where(
            ExceptionRecord.exception_type == ExceptionType.INVENTORY_SHORTAGE
        )
    )
    assert record is not None
    original_key = record.deduplication_key
    transition_exception(
        session, record.id, ExceptionStatus.ACKNOWLEDGED, actor="operator", changed_at=AS_OF
    )
    transition_exception(
        session, record.id, ExceptionStatus.IN_PROGRESS, actor="operator", changed_at=AS_OF
    )
    transition_exception(
        session,
        record.id,
        ExceptionStatus.RESOLVED,
        actor="operator",
        reason="replenished",
        changed_at=AS_OF,
    )
    session.commit()

    second = ExceptionService(
        session, Settings(safety_stock=Decimal("999"), inventory_mismatch_tolerance=Decimal("1"))
    ).detect(AS_OF)
    session.commit()

    assert second.skipped >= 1
    assert session.scalar(
        select(func.count())
        .select_from(ExceptionRecord)
        .where(ExceptionRecord.exception_type == ExceptionType.INVENTORY_SHORTAGE)
    ) == 1
    refreshed = session.get(ExceptionRecord, record.id)
    assert refreshed is not None
    assert refreshed.deduplication_key == original_key


def test_active_changed_fingerprint_revert_preserves_active_identity_key() -> None:
    session = session_with_fixture()
    service = ExceptionService(session)
    service.detect(AS_OF)
    session.commit()
    record = session.scalar(
        select(ExceptionRecord).where(
            ExceptionRecord.exception_type == ExceptionType.SLA_BREACH_RISK
        )
    )
    order = session.get(Order, 1)
    assert record is not None and order is not None
    original_key = record.deduplication_key

    transition_exception(
        session, record.id, ExceptionStatus.ACKNOWLEDGED, actor="operator", changed_at=AS_OF
    )
    transition_exception(
        session, record.id, ExceptionStatus.IN_PROGRESS, actor="operator", changed_at=AS_OF
    )
    transition_exception(
        session,
        record.id,
        ExceptionStatus.RESOLVED,
        actor="operator",
        reason="replanned",
        changed_at=AS_OF,
    )
    session.commit()

    order.promised_at = AS_OF + timedelta(hours=1)
    session.commit()
    changed = service.detect(AS_OF)
    session.commit()
    assert changed.created >= 1
    active = session.scalar(
        select(ExceptionRecord).where(
            ExceptionRecord.exception_type == ExceptionType.SLA_BREACH_RISK,
            ExceptionRecord.status.in_(
                (ExceptionStatus.OPEN, ExceptionStatus.ACKNOWLEDGED, ExceptionStatus.IN_PROGRESS)
            ),
        )
    )
    assert active is not None
    changed_key = active.deduplication_key
    assert changed_key != original_key

    order.promised_at = AS_OF - timedelta(hours=1)
    session.commit()
    reverted = service.detect(AS_OF)
    session.commit()

    assert reverted.updated >= 1
    refreshed_active = session.get(ExceptionRecord, active.id)
    assert refreshed_active is not None
    assert refreshed_active.deduplication_key == changed_key
    assert session.scalar(
        select(func.count())
        .select_from(ExceptionRecord)
        .where(ExceptionRecord.exception_type == ExceptionType.SLA_BREACH_RISK)
    ) == 2


def test_lifecycle_validates_paths_terminal_state_actor_and_reason() -> None:
    session = session_with_fixture()
    service = ExceptionService(
        session, Settings(safety_stock=Decimal("20"), inventory_mismatch_tolerance=Decimal("1"))
    )
    service.detect(AS_OF)
    session.commit()
    record = session.scalar(select(ExceptionRecord))
    assert record is not None

    with pytest.raises(ValueError, match="actor"):
        transition_exception(session, record.id, ExceptionStatus.ACKNOWLEDGED, actor=" ")
    with pytest.raises(InvalidTransition):
        transition_exception(session, record.id, ExceptionStatus.IN_PROGRESS, actor="operator")
    transition_exception(
        session, record.id, ExceptionStatus.ACKNOWLEDGED, actor="operator", changed_at=AS_OF
    )
    transition_exception(
        session, record.id, ExceptionStatus.IN_PROGRESS, actor="operator", changed_at=AS_OF
    )
    with pytest.raises(ValueError, match="reason"):
        transition_exception(
            session, record.id, ExceptionStatus.RESOLVED, actor="operator", changed_at=AS_OF
        )
    transition_exception(
        session,
        record.id,
        ExceptionStatus.RESOLVED,
        actor="operator",
        reason="fixed",
        changed_at=AS_OF,
    )
    with pytest.raises(InvalidTransition):
        transition_exception(
            session, record.id, ExceptionStatus.DISMISSED, actor="operator", reason="late"
        )


def test_history_is_immutable_at_the_orm_boundary() -> None:
    session = session_with_fixture()
    service = ExceptionService(
        session,
        Settings(safety_stock=Decimal("20"), inventory_mismatch_tolerance=Decimal("1")),
    )
    service.detect(AS_OF)
    session.commit()
    history = session.scalar(select(ExceptionHistory))
    assert history is not None

    history.actor = "tampered"
    with pytest.raises(ValueError, match="append-only"):
        session.flush()
    session.rollback()

    with pytest.raises(ValueError, match="append-only"):
        session.execute(update(ExceptionHistory).values(actor="bulk-tampered"))
    session.rollback()

    with pytest.raises(ValueError, match="append-only"):
        session.bulk_update_mappings(
            ExceptionHistory,
            [{"id": history.id, "actor": "legacy-bulk-tampered"}],
        )
    session.rollback()

    history = session.scalar(select(ExceptionHistory))
    assert history is not None
    assert history.actor == "exception-engine"
    session.delete(history)
    with pytest.raises(ValueError, match="append-only"):
        session.flush()
    session.rollback()

    with pytest.raises(ValueError, match="append-only"):
        session.execute(delete(ExceptionHistory))
    session.rollback()


def test_m03_migration_declares_database_history_immutability() -> None:
    migration = Path("migrations/versions/20250827_02_exception_history_immutable.py").read_text()

    assert "ON DELETE RESTRICT" in migration
    assert "BEFORE UPDATE OR DELETE ON exception_history" in migration
    assert "BEFORE TRUNCATE ON exception_history" in migration
    assert "trg_exception_history_append_only_truncate" in migration
    assert "RAISE EXCEPTION 'exception_history is append-only'" in migration
    assert "ON DELETE CASCADE" in migration


def test_all_legal_lifecycle_paths_and_terminal_states_are_enforced() -> None:
    session = session_with_fixture()
    ExceptionService(session).detect(AS_OF)
    session.commit()
    records = list(session.scalars(select(ExceptionRecord).order_by(ExceptionRecord.id)).all())
    assert len(records) >= 4

    transition_exception(
        session,
        records[0].id,
        ExceptionStatus.DISMISSED,
        actor="operator",
        reason="not actionable",
    )

    transition_exception(session, records[1].id, ExceptionStatus.ACKNOWLEDGED, actor="operator")
    transition_exception(
        session, records[1].id, ExceptionStatus.DISMISSED, actor="operator", reason="duplicate"
    )

    transition_exception(session, records[2].id, ExceptionStatus.ACKNOWLEDGED, actor="operator")
    transition_exception(session, records[2].id, ExceptionStatus.IN_PROGRESS, actor="operator")
    transition_exception(
        session, records[2].id, ExceptionStatus.DISMISSED, actor="operator", reason="superseded"
    )

    transition_exception(session, records[3].id, ExceptionStatus.ACKNOWLEDGED, actor="operator")
    transition_exception(session, records[3].id, ExceptionStatus.IN_PROGRESS, actor="operator")
    transition_exception(
        session, records[3].id, ExceptionStatus.RESOLVED, actor="operator", reason="fixed"
    )
    session.commit()

    for record in records[:4]:
        with pytest.raises(InvalidTransition):
            transition_exception(
                session, record.id, ExceptionStatus.OPEN, actor="operator", reason="reopen"
            )
    assert [
        session.scalar(
            select(func.count())
            .select_from(ExceptionHistory)
            .where(ExceptionHistory.exception_id == record.id)
        )
        for record in records[:4]
    ] == [2, 3, 4, 4]


def test_dismissal_requires_nonblank_reason() -> None:
    session = session_with_fixture()
    ExceptionService(session).detect(AS_OF)
    session.commit()
    record = session.scalar(select(ExceptionRecord))
    assert record is not None

    with pytest.raises(ValueError, match="reason"):
        transition_exception(
            session, record.id, ExceptionStatus.DISMISSED, actor="operator", reason=" "
        )
