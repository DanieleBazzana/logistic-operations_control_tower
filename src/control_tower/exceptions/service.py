"""Transactional detection persistence and active exception deduplication."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from control_tower.config import Settings
from control_tower.enums import ExceptionStatus
from control_tower.exceptions.contracts import (
    DetectionRunResult,
    ExceptionDetection,
    normalize_as_of,
)
from control_tower.exceptions.rules import detect_all
from control_tower.exceptions.severity import severity_for_detection
from control_tower.models import DATASET_SCOPE_EXPLICIT_KEY, ExceptionHistory, ExceptionRecord
from control_tower.replacement.service import active_dataset_version_id

ACTIVE_STATUSES = (
    ExceptionStatus.OPEN,
    ExceptionStatus.ACKNOWLEDGED,
    ExceptionStatus.IN_PROGRESS,
)


def _resolve_dataset_version_id(session: Session, dataset_version_id: int | None) -> int | None:
    """Require an explicit dataset in candidate scope, otherwise preserve legacy fallback."""

    if dataset_version_id is None:
        if session.info.get(DATASET_SCOPE_EXPLICIT_KEY, False):
            raise ValueError("dataset_version_id is required for candidate/replacement writes")
        return active_dataset_version_id(session)
    return dataset_version_id


def make_deduplication_key(detection: ExceptionDetection) -> str:
    """Build the stable identity from type, issue identity, and source fingerprint."""

    material = f"{detection.exception_type.value}|{detection.issue_key}|{detection.fingerprint}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _apply_derived_fields(
    record: ExceptionRecord, detection: ExceptionDetection, settings: Settings, instant: datetime
) -> None:
    record.severity = severity_for_detection(detection, settings)
    record.expected_resolution = detection.expected_resolution
    record.business_impact = detection.business_impact
    record.revenue_at_risk = detection.revenue_at_risk
    record.orders_affected = detection.orders_affected
    record.root_cause = detection.root_cause
    record.recommended_action = detection.recommended_action
    record.confidence = detection.confidence
    record.warehouse_id = detection.warehouse_id
    record.product_id = detection.product_id


def _persist_one(
    session: Session,
    detection: ExceptionDetection,
    settings: Settings,
    instant: datetime,
    dataset_version_id: int | None = None,
) -> str:
    scope = (
        (ExceptionRecord.dataset_version_id == dataset_version_id,)
        if dataset_version_id is not None
        else ()
    )
    active = session.scalar(
        select(ExceptionRecord)
        .where(
            ExceptionRecord.exception_type == detection.exception_type,
            ExceptionRecord.issue_key == detection.issue_key,
            ExceptionRecord.status.in_(ACTIVE_STATUSES),
            *scope,
        )
        .with_for_update()
    )
    if active is not None:
        # A recurring active issue refreshes explainability/derived metrics, but
        # never changes lifecycle state or creates audit noise.
        _apply_derived_fields(active, detection, settings, instant)
        session.flush()
        return "updated"

    deduplication_key = make_deduplication_key(detection)
    historical = session.scalar(
        select(ExceptionRecord)
        .where(ExceptionRecord.deduplication_key == deduplication_key, *scope)
        .with_for_update()
    )
    if historical is not None:
        # A resolved/dismissed issue with identical source facts is not raised
        # again. A changed fingerprint has a different key and is eligible.
        return "skipped"

    record = ExceptionRecord(
        deduplication_key=deduplication_key,
        exception_type=detection.exception_type,
        issue_key=detection.issue_key,
        entity_type=detection.entity_type,
        entity_id=detection.entity_id,
        severity=severity_for_detection(detection, settings),
        status=ExceptionStatus.OPEN,
        detected_at=instant,
        expected_resolution=detection.expected_resolution,
        business_impact=detection.business_impact,
        revenue_at_risk=detection.revenue_at_risk,
        orders_affected=detection.orders_affected,
        root_cause=detection.root_cause,
        recommended_action=detection.recommended_action,
        confidence=detection.confidence,
        warehouse_id=detection.warehouse_id,
        product_id=detection.product_id,
        dataset_version_id=dataset_version_id,
    )
    session.add(record)
    session.flush()
    session.add(
        ExceptionHistory(
            exception_id=record.id,
            dataset_version_id=record.dataset_version_id,
            from_status=None,
            to_status=ExceptionStatus.OPEN,
            changed_at=instant,
            actor="exception-engine",
            transition_reason=None,
        )
    )
    session.flush()
    return "created"


def persist_detections(
    session: Session,
    detections: Iterable[ExceptionDetection],
    as_of: datetime,
    settings: Settings | None = None,
    *,
    dataset_version_id: int | None = None,
) -> DetectionRunResult:
    """Persist one run in the caller's transaction and return its counters."""

    instant = normalize_as_of(as_of)
    configured = settings or Settings()
    dataset_version_id = _resolve_dataset_version_id(session, dataset_version_id)
    findings = tuple(detections)
    created = updated = skipped = 0
    for detection in findings:
        result = _persist_one(
            session, detection, configured, instant, dataset_version_id
        )
        if result == "created":
            created += 1
        elif result == "updated":
            updated += 1
        else:
            skipped += 1
    return DetectionRunResult(instant, findings, created, updated, skipped)


class ExceptionService:
    """Run all M03 rules and persist their findings transactionally."""

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or Settings()

    def detect(
        self, as_of: datetime, *, dataset_version_id: int | None = None
    ) -> DetectionRunResult:
        instant = normalize_as_of(as_of)
        dataset_version_id = _resolve_dataset_version_id(self.session, dataset_version_id)
        detections = detect_all(
            self.session,
            instant,
            self.settings,
            dataset_version_id=dataset_version_id,
        )
        return persist_detections(
            self.session,
            detections,
            instant,
            self.settings,
            dataset_version_id=dataset_version_id,
        )

    run = detect


def detect_and_persist(
    session: Session,
    as_of: datetime,
    settings: Settings | None = None,
    *,
    dataset_version_id: int | None = None,
) -> DetectionRunResult:
    """Functional entry point for applications that prefer no service object."""

    configured = settings or Settings()
    dataset_version_id = _resolve_dataset_version_id(session, dataset_version_id)
    return persist_detections(
        session,
        detect_all(session, as_of, configured, dataset_version_id=dataset_version_id),
        as_of,
        configured,
        dataset_version_id=dataset_version_id,
    )


__all__ = [
    "ACTIVE_STATUSES",
    "ExceptionService",
    "detect_and_persist",
    "make_deduplication_key",
    "persist_detections",
]
