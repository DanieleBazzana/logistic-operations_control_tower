"""Deterministic, fail-closed dataset replacement primitives.

The service deliberately does not commit. Operators can compose stage, validate,
activate, and rollback in one transaction and decide when to commit.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Type

from sqlalchemy import Select, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from control_tower.models import (
    DatasetActivation,
    DatasetVersion,
    ExceptionHistory,
    ExceptionRecord,
    Inventory,
    InventoryMovement,
    Order,
    OrderItem,
    OrderObservation,
    OrderObservationReceipt,
    Product,
    PurchaseOrder,
    PurchaseOrderItem,
    Shipment,
    SourceOrderIdentity,
    Supplier,
    Warehouse,
)

LEGACY_DATASET_KEY = "M07.6"
LEGACY_MANIFEST_IDENTITY = "m07.6-legacy-backfill"
LEGACY_GENERATOR_REVISION = "m07.6"
LEGACY_SEED = 20250301
LEGACY_AS_OF = datetime(2025, 3, 7, 18, 0, tzinfo=timezone.utc)

SCOPED_MODELS: tuple[Type[Any], ...] = (
    Product,
    Warehouse,
    Inventory,
    InventoryMovement,
    SourceOrderIdentity,
    Order,
    OrderObservation,
    OrderObservationReceipt,
    OrderItem,
    Supplier,
    PurchaseOrder,
    PurchaseOrderItem,
    Shipment,
    ExceptionRecord,
    ExceptionHistory,
)


class DatasetLifecycleError(RuntimeError):
    """Raised when a dataset transition would violate the state machine."""


class DatasetIdentityConflict(DatasetLifecycleError):
    """The logical dataset identity already exists with different content."""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as_of must include a timezone")
    return value.astimezone(timezone.utc)


def dataset_identity(
    *,
    dataset_key: str,
    manifest_identity: str,
    content_hash: str,
    seed: int,
    as_of: datetime,
    generator_revision: str,
) -> str:
    """Return the stable identity digest for a complete dataset artifact."""

    payload = {
        "as_of": _utc(as_of).isoformat().replace("+00:00", "Z"),
        "content_hash": content_hash,
        "dataset_key": dataset_key,
        "generator_revision": generator_revision,
        "manifest_identity": manifest_identity,
        "seed": seed,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _advisory_lock(session: Session) -> None:
    """Serialize replacement operations on PostgreSQL; no-op for local SQLite tests."""

    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(70807701)"))


def _same_content(row: DatasetVersion, *, content_hash: str, identity_hash: str) -> bool:
    return row.content_hash == content_hash and row.identity_hash == identity_hash


def ensure_dataset_version(
    session: Session,
    *,
    dataset_key: str,
    manifest_identity: str,
    content_hash: str,
    seed: int,
    as_of: datetime,
    generator_revision: str,
) -> DatasetVersion:
    """Create or return one dataset row, safely retrying a concurrent insert."""

    as_of = _utc(as_of)
    identity_hash = dataset_identity(
        dataset_key=dataset_key,
        manifest_identity=manifest_identity,
        content_hash=content_hash,
        seed=seed,
        as_of=as_of,
        generator_revision=generator_revision,
    )
    logical = select(DatasetVersion).where(
        DatasetVersion.dataset_key == dataset_key,
        DatasetVersion.manifest_identity == manifest_identity,
        DatasetVersion.seed == seed,
        DatasetVersion.as_of == as_of,
        DatasetVersion.generator_revision == generator_revision,
    )
    _advisory_lock(session)
    existing = session.scalar(logical.order_by(DatasetVersion.id).limit(1))
    if existing is not None:
        if not _same_content(existing, content_hash=content_hash, identity_hash=identity_hash):
            raise DatasetIdentityConflict(
                f"dataset {dataset_key!r} logical identity exists with different content"
            )
        return existing

    candidate = DatasetVersion(
        dataset_key=dataset_key,
        identity_hash=identity_hash,
        manifest_identity=manifest_identity,
        content_hash=content_hash,
        seed=seed,
        as_of=as_of,
        generator_revision=generator_revision,
        status="STAGED",
    )
    try:
        with session.begin_nested():
            session.add(candidate)
            session.flush()
    except IntegrityError:
        # A concurrent writer won the unique logical-identity race. The savepoint
        # rollback keeps the caller's transaction usable; re-read is mandatory.
        existing = session.scalar(logical.order_by(DatasetVersion.id).limit(1))
        if existing is None:
            raise DatasetLifecycleError("dataset insert conflicted but winner is not visible")
        if not _same_content(existing, content_hash=content_hash, identity_hash=identity_hash):
            raise DatasetIdentityConflict(
                f"dataset {dataset_key!r} logical identity exists with different content"
            )
        return existing
    return candidate


def stage_dataset_version(session: Session, dataset: DatasetVersion | int) -> DatasetVersion:
    """Lock and confirm a dataset is stageable; staging is idempotent."""

    _advisory_lock(session)
    row = _locked_dataset(session, dataset)
    if row.status == "RETIRED":
        raise DatasetLifecycleError("retired dataset cannot be staged")
    if row.status not in {"STAGED", "READY", "ACTIVE"}:
        raise DatasetLifecycleError(f"unknown dataset status {row.status!r}")
    return row


def validate_dataset(session: Session, dataset: DatasetVersion | int) -> DatasetVersion:
    """Move STAGED to READY after the loader's transactional checks."""

    _advisory_lock(session)
    row = _locked_dataset(session, dataset)
    if row.status == "STAGED":
        row.status = "READY"
        session.flush()
    elif row.status not in {"READY", "ACTIVE"}:
        raise DatasetLifecycleError(f"dataset {row.id} is not validatable from {row.status}")
    return row


def activate_dataset(session: Session, dataset: DatasetVersion | int) -> DatasetVersion:
    """Atomically move the singleton read pointer to a READY dataset."""

    _advisory_lock(session)
    candidate = _locked_dataset(session, dataset)
    if candidate.status not in {"READY", "ACTIVE"}:
        raise DatasetLifecycleError("only READY datasets can be activated")
    pointer = session.scalar(
        select(DatasetActivation).where(DatasetActivation.id == 1).with_for_update()
    )
    if pointer is not None and pointer.active_dataset_version_id == candidate.id:
        if candidate.status == "READY":
            candidate.status = "ACTIVE"
            session.flush()
        return candidate
    previous = None
    if pointer is not None:
        previous = session.get(
            DatasetVersion, pointer.active_dataset_version_id, with_for_update=True
        )
    if previous is not None and previous.id != candidate.id:
        if previous.status == "ACTIVE":
            previous.status = "RETIRED"
    candidate.status = "ACTIVE"
    if pointer is None:
        pointer = DatasetActivation(id=1, active_dataset_version_id=candidate.id)
        session.add(pointer)
    else:
        pointer.active_dataset_version_id = candidate.id
    session.flush()
    return candidate


def rollback_dataset(
    session: Session, dataset: DatasetVersion | int | None = None
) -> DatasetVersion:
    """Move the pointer back to the most recent retired dataset."""

    _advisory_lock(session)
    pointer = session.scalar(
        select(DatasetActivation).where(DatasetActivation.id == 1).with_for_update()
    )
    if pointer is None:
        raise DatasetLifecycleError("no active dataset pointer exists")
    current = session.get(DatasetVersion, pointer.active_dataset_version_id, with_for_update=True)
    if current is None:
        raise DatasetLifecycleError("active dataset pointer is dangling")
    if dataset is not None:
        requested = _locked_dataset(session, dataset)
        if requested.id != current.id:
            raise DatasetLifecycleError("rollback target is not the active dataset")
    previous = session.scalar(
        select(DatasetVersion)
        .where(DatasetVersion.status == "RETIRED", DatasetVersion.id != current.id)
        .order_by(DatasetVersion.created_at.desc(), DatasetVersion.id.desc())
        .with_for_update()
    )
    if previous is None:
        raise DatasetLifecycleError("no retired dataset is available for rollback")
    current.status = "RETIRED"
    previous.status = "ACTIVE"
    pointer.active_dataset_version_id = previous.id
    session.flush()
    return previous


def get_active_dataset_version(session: Session) -> DatasetVersion | None:
    pointer = session.scalar(select(DatasetActivation).where(DatasetActivation.id == 1))
    if pointer is None:
        return None
    return session.get(DatasetVersion, pointer.active_dataset_version_id)


def active_dataset_version_id(session: Session) -> int | None:
    active = get_active_dataset_version(session)
    return active.id if active is not None else None


def scope_statement(
    statement: Select[Any], model: Type[Any], dataset_version_id: int | None
) -> Select[Any]:
    """Apply an explicit dataset boundary to a query."""

    if dataset_version_id is None:
        return statement
    return statement.where(model.dataset_version_id == dataset_version_id)


def _locked_dataset(session: Session, dataset: DatasetVersion | int) -> DatasetVersion:
    row = (
        dataset
        if isinstance(dataset, DatasetVersion)
        else session.get(DatasetVersion, dataset, with_for_update=True)
    )
    if row is None:
        raise DatasetLifecycleError("dataset version does not exist")
    if isinstance(dataset, DatasetVersion):
        row = session.get(DatasetVersion, dataset.id, with_for_update=True) or dataset
    return row


def backfill_m076(session: Session) -> DatasetVersion:
    """Assign existing unscoped M07.6 rows without rewriting their values."""

    legacy = ensure_dataset_version(
        session,
        dataset_key=LEGACY_DATASET_KEY,
        manifest_identity=LEGACY_MANIFEST_IDENTITY,
        content_hash=hashlib.sha256(b"M07.6 legacy backfill").hexdigest(),
        seed=LEGACY_SEED,
        as_of=LEGACY_AS_OF,
        generator_revision=LEGACY_GENERATOR_REVISION,
    )
    for model in SCOPED_MODELS:
        statement = (
            update(model)
            .where(model.dataset_version_id.is_(None))
            .values(dataset_version_id=legacy.id)
        )
        # Exception history is append-only. This narrowly-scoped execution
        # option is accepted by the model guard only for this one-time metadata
        # assignment; all other history UPDATE/DELETE operations remain blocked.
        if model.__tablename__ == "exception_history":
            statement = statement.execution_options(_m076_metadata_backfill=True)
        session.execute(statement)
    if legacy.status == "STAGED":
        legacy.status = "ACTIVE"
    pointer = session.scalar(select(DatasetActivation).where(DatasetActivation.id == 1))
    if pointer is None:
        session.add(DatasetActivation(id=1, active_dataset_version_id=legacy.id))
    session.flush()
    return legacy


__all__ = [
    "LEGACY_AS_OF", "LEGACY_DATASET_KEY", "LEGACY_SEED", "SCOPED_MODELS",
    "DatasetIdentityConflict", "DatasetLifecycleError", "active_dataset_version_id",
    "activate_dataset", "backfill_m076", "dataset_identity", "ensure_dataset_version",
    "get_active_dataset_version", "rollback_dataset", "scope_statement", "stage_dataset_version",
    "validate_dataset",
]
