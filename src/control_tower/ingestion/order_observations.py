"""Source-scoped immutable order observations and explicit projection promotion."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from control_tower.enums import (
    OrderObservationStatus,
    OrderStatus,
    VersionComparison,
    WarehouseCapability,
)
from control_tower.models import (
    Order,
    OrderObservation,
    OrderObservationReceipt,
    SourceOrderIdentity,
    Warehouse,
)

_ORDER_STATUSES = {status.value for status in OrderStatus}
_STATUS_ALIASES = {
    "DELIVERED": OrderStatus.FULFILLED.value,
    "COMPLETE": OrderStatus.FULFILLED.value,
    "COMPLETED": OrderStatus.FULFILLED.value,
    "CANCELED": OrderStatus.CANCELLED.value,
    "CANCELLED": OrderStatus.CANCELLED.value,
    "OPEN": OrderStatus.OPEN.value,
    "PENDING": OrderStatus.OPEN.value,
}
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_MAX_CONFLICT_RETRIES = 3


def _canonical_value(value: Any, *, field_name: str | None = None) -> Any:
    """Convert supported source values to deterministic JSON values."""

    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return (
            value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("decimal must be finite")
        return format(value.normalize(), "f")
    if isinstance(value, str):
        text = unicodedata.normalize("NFC", value)
        if field_name in {"amount", "total_amount", "order_total"}:
            try:
                return format(Decimal(text).normalize(), "f")
            except InvalidOperation:
                pass
        if field_name == "at" or field_name and field_name.endswith("_at"):
            try:
                return _canonical_value(datetime.fromisoformat(text.replace("Z", "+00:00")))
            except ValueError:
                pass
        return text
    if isinstance(value, dict):
        return {
            unicodedata.normalize("NFC", str(key)): _canonical_value(item, field_name=str(key))
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, bool | int | float):
        return value
    return _canonical_value(str(value), field_name=field_name)


def canonical_source_facts(facts: dict[str, Any]) -> str:
    """Canonical JSON for strict facts.

    NULL is JSON ``null``; text is NFC-normalized; decimals use plain notation
    with insignificant zeroes removed; timezone-aware timestamps become UTC
    ISO-8601 with six fractional digits; mapping keys are sorted and list order
    is retained. This serialization, encoded as UTF-8, is hashed verbatim.
    """

    return json.dumps(
        _canonical_value(facts), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def source_row_hash(facts: dict[str, Any]) -> str:
    """SHA-256 over canonical normalized strict source facts."""

    return hashlib.sha256(canonical_source_facts(facts).encode("utf-8")).hexdigest()


def canonical_replay_identity(
    source_namespace: str,
    source_order_id: str,
    source_version: str | None,
    source_row_hash: str,
) -> list[str | None]:
    """Authoritative structured replay identity; ``None`` is not empty text."""

    return [
        unicodedata.normalize("NFC", source_namespace),
        unicodedata.normalize("NFC", source_order_id),
        None if source_version is None else unicodedata.normalize("NFC", source_version),
        unicodedata.normalize("NFC", source_row_hash),
    ]


def replay_identity_digest(
    source_namespace: str,
    source_order_id: str,
    source_version: str | None,
    source_row_hash: str,
) -> str:
    """Digest canonical JSON of ``[namespace, order_id, version-or-null, row_hash]``."""

    payload = json.dumps(
        canonical_replay_identity(
            source_namespace, source_order_id, source_version, source_row_hash
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NormalizedOrderObservation:
    """Canonical source facts; absent source facts remain ``None``."""

    source_namespace: str
    source_order_id: str | None
    source_version: str | None
    order_number: str | None
    status: str | None
    region: str | None
    source_warehouse_id: str | None
    ordered_at: datetime | None
    promised_at: datetime | None
    fulfilled_at: datetime | None
    total_amount: Decimal | None
    currency: str | None
    source_row_hash: str
    source_facts: dict[str, Any]


@dataclass(frozen=True)
class OrderObservationAssessment:
    """Normalization result plus explicit capability and promotion reasons."""

    observation: NormalizedOrderObservation
    status: OrderObservationStatus
    source_status: OrderObservationStatus
    capabilities: dict[str, Any]
    non_promotion_reasons: tuple[str, ...]
    invalid_reasons: tuple[str, ...] = ()

    @property
    def financially_eligible(self) -> bool:
        return self.capabilities["financial"]

    @property
    def promotion_eligible(self) -> bool:
        return self.status is OrderObservationStatus.NORMALIZED


def _text(value: Any) -> str | None:
    if value is None:
        return None
    value = unicodedata.normalize("NFC", str(value)).strip()
    return value or None


def _source_version(value: Any) -> str | None:
    if value is None:
        return None
    return unicodedata.normalize("NFC", str(value))


def _first_value(raw: dict[str, Any], *names: str) -> tuple[Any, bool]:
    values = [(name, raw[name]) for name in names if name in raw and _text(raw[name]) is not None]
    if not values:
        return None, False
    first = values[0][1]
    if any(_text(value) != _text(first) for _, value in values[1:]):
        return values, True
    return first, False


def _timestamp(value: Any) -> datetime | None:
    if value is None or isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, str) and not value.strip():
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as error:
        raise ValueError("total amount must be a decimal") from error
    if not parsed.is_finite():
        raise ValueError("total amount must be finite")
    if parsed < 0:
        raise ValueError("total amount must not be negative")
    return parsed


def _status(value: Any) -> str | None:
    normalized = _text(value)
    if normalized is None:
        return None
    normalized = normalized.upper()
    return _STATUS_ALIASES.get(normalized, normalized)


def _base_observation(
    raw: dict[str, Any], source_namespace: str, source_version: str | None
) -> tuple[NormalizedOrderObservation, list[str]]:
    source_order_id, conflict = _first_value(raw, "source_order_id", "order_id")
    reasons: list[str] = ["CONFLICTING_SOURCE_ORDER_ID"] if conflict else []
    source_order_id = _text(source_order_id)

    amount, conflict = _first_value(raw, "total_amount", "amount", "order_total")
    if conflict:
        reasons.append("CONFLICTING_TOTAL_AMOUNT")
    currency, currency_conflict = _first_value(raw, "currency", "order_currency")
    if currency_conflict:
        reasons.append("CONFLICTING_CURRENCY")
    warehouse, warehouse_conflict = _first_value(
        raw, "source_warehouse_id", "warehouse_id", "warehouse"
    )
    if warehouse_conflict:
        reasons.append("CONFLICTING_WAREHOUSE")

    parsed_amount: Decimal | None = None
    if amount is not None:
        try:
            parsed_amount = _money(amount)
        except ValueError as error:
            reasons.append(
                "NEGATIVE_TOTAL_AMOUNT" if "negative" in str(error) else "MALFORMED_TOTAL_AMOUNT"
            )

    parsed_timestamps: dict[str, datetime | None] = {}
    for field, names in {
        "ordered_at": ("ordered_at", "order_purchase_timestamp", "order_date"),
        "promised_at": ("promised_at", "order_estimated_delivery_date"),
        "fulfilled_at": ("fulfilled_at", "order_delivered_customer_date"),
    }.items():
        value, timestamp_conflict = _first_value(raw, *names)
        if timestamp_conflict:
            reasons.append(f"CONFLICTING_{field.upper()}")
        if value is None:
            parsed_timestamps[field] = None
            continue
        try:
            parsed_timestamps[field] = _timestamp(value)
        except ValueError:
            parsed_timestamps[field] = None
            reasons.append(f"MALFORMED_{field.upper()}")

    normalized_currency = _text(currency)
    facts: dict[str, Any] = {
        "source_order_id": source_order_id,
        "order_number": _text(raw.get("order_number")),
        "status": _status(raw.get("status") or raw.get("order_status")),
        "region": _text(raw.get("region") or raw.get("customer_state")),
        "source_warehouse_id": _text(warehouse),
        "ordered_at": parsed_timestamps["ordered_at"],
        "promised_at": parsed_timestamps["promised_at"],
        "fulfilled_at": parsed_timestamps["fulfilled_at"],
        "total_amount": parsed_amount,
        "currency": normalized_currency.upper() if normalized_currency else None,
    }
    observation = NormalizedOrderObservation(
        source_namespace=_text(source_namespace) or "",
        source_order_id=source_order_id,
        source_version=source_version,
        source_row_hash=source_row_hash(facts),
        source_facts=facts,
        order_number=facts["order_number"],
        status=facts["status"],
        region=facts["region"],
        source_warehouse_id=facts["source_warehouse_id"],
        ordered_at=facts["ordered_at"],
        promised_at=facts["promised_at"],
        fulfilled_at=facts["fulfilled_at"],
        total_amount=facts["total_amount"],
        currency=facts["currency"],
    )
    return observation, reasons


def assess_order_observation(
    raw: dict[str, Any],
    *,
    source_namespace: str,
    batch_id: str,
    source_version: str | None = None,
    known_warehouse_ids: Iterable[str] | None = None,
) -> OrderObservationAssessment:
    """Normalize one row and assess capabilities without creating operational context."""

    del batch_id  # provenance is represented by OrderObservationReceipt, never identity
    observation, reasons = _base_observation(raw, source_namespace, _source_version(source_version))
    invalid_reasons = list(reasons)
    if not observation.source_namespace:
        invalid_reasons.append("MISSING_SOURCE_NAMESPACE")
    if not observation.source_order_id:
        invalid_reasons.append("MISSING_SOURCE_ORDER_ID")
    if observation.status is not None and observation.status not in _ORDER_STATUSES:
        invalid_reasons.append("MALFORMED_STATUS")
    if observation.currency is not None and not _CURRENCY_RE.fullmatch(observation.currency):
        invalid_reasons.append("MALFORMED_CURRENCY")
    if (
        observation.fulfilled_at is not None
        and observation.ordered_at is not None
        and observation.fulfilled_at < observation.ordered_at
    ):
        invalid_reasons.append("CONFLICTING_TIMESTAMPS")

    source_status = (
        OrderObservationStatus.REJECTED_INVALID
        if invalid_reasons
        else OrderObservationStatus.SOURCE_VALID
    )
    context_reasons: list[str] = []
    warehouse_ids = None if known_warehouse_ids is None else set(known_warehouse_ids)
    if not observation.source_warehouse_id:
        warehouse_capability = WarehouseCapability.CONTEXT_UNAVAILABLE.value
    elif warehouse_ids is None:
        warehouse_capability = WarehouseCapability.OBSERVED.value
    elif observation.source_warehouse_id in warehouse_ids:
        warehouse_capability = WarehouseCapability.VALIDATED.value
    else:
        warehouse_capability = WarehouseCapability.UNKNOWN.value
        context_reasons.append("UNKNOWN_WAREHOUSE")
    if observation.total_amount is None:
        context_reasons.append("MISSING_TOTAL_AMOUNT")
    if observation.currency is None:
        context_reasons.append("MISSING_CURRENCY")
    if observation.total_amount is not None and observation.currency is None:
        context_reasons.append("AMOUNT_WITHOUT_CURRENCY")
    if not observation.order_number:
        context_reasons.append("MISSING_ORDER_NUMBER")
    if not observation.status:
        context_reasons.append("MISSING_STATUS")
    if not observation.region:
        context_reasons.append("MISSING_REGION")
    if not observation.ordered_at:
        context_reasons.append("MISSING_ORDERED_AT")
    if not observation.promised_at:
        context_reasons.append("MISSING_PROMISED_AT")

    capabilities = {
        "warehouse": warehouse_capability,
        "financial": observation.total_amount is not None and observation.currency is not None,
        "timing": observation.ordered_at is not None and observation.promised_at is not None,
    }
    if invalid_reasons:
        status = OrderObservationStatus.REJECTED_INVALID
    elif context_reasons:
        status = OrderObservationStatus.CONTEXT_INCOMPLETE
    else:
        status = OrderObservationStatus.NORMALIZED
    return OrderObservationAssessment(
        observation=observation,
        status=status,
        source_status=source_status,
        capabilities=capabilities,
        non_promotion_reasons=tuple(dict.fromkeys(context_reasons)),
        invalid_reasons=tuple(dict.fromkeys(invalid_reasons)),
    )


def _get_or_create_identity(
    session: Session, namespace: str, source_order_id: str
) -> SourceOrderIdentity:
    statement = (
        select(SourceOrderIdentity)
        .where(
            SourceOrderIdentity.source_namespace == namespace,
            SourceOrderIdentity.source_order_id == source_order_id,
        )
        .with_for_update()
    )
    identity = session.scalar(statement)
    if identity is not None:
        return identity
    for attempt in range(_MAX_CONFLICT_RETRIES):
        try:
            with session.begin_nested():
                identity = SourceOrderIdentity(
                    source_namespace=namespace, source_order_id=source_order_id
                )
                session.add(identity)
                session.flush()
        except IntegrityError:
            identity = session.scalar(statement)
            if identity is not None:
                return identity
            if attempt == _MAX_CONFLICT_RETRIES - 1:
                raise
        else:
            return identity
    raise RuntimeError("bounded source identity retries exhausted")


def _receipt(session: Session, observation: OrderObservation, batch_id: str) -> None:
    """Create a separate provenance receipt, safely idempotent per batch."""

    for attempt in range(_MAX_CONFLICT_RETRIES):
        try:
            with session.begin_nested():
                session.add(OrderObservationReceipt(observation=observation, batch_id=batch_id))
                session.flush()
        except IntegrityError:
            exists = session.scalar(
                select(OrderObservationReceipt).where(
                    OrderObservationReceipt.observation_id == observation.id,
                    OrderObservationReceipt.batch_id == batch_id,
                )
            )
            if exists is not None:
                return
            if attempt == _MAX_CONFLICT_RETRIES - 1:
                raise
        else:
            return


def stage_order_observation(
    session: Session,
    raw: dict[str, Any],
    *,
    source_namespace: str,
    batch_id: str,
    source_version: str | None = None,
) -> OrderObservation:
    """Persist immutable evidence and a batch receipt; exact evidence replays one row."""

    assessment = assess_order_observation(
        raw,
        source_namespace=source_namespace,
        batch_id=batch_id,
        source_version=source_version,
    )
    item = assessment.observation
    if not item.source_namespace or not item.source_order_id:
        raise ValueError(
            "REJECTED_INVALID: "
            + ",".join(assessment.invalid_reasons or ("MISSING_SOURCE_IDENTITY",))
        )
    namespace = item.source_namespace
    source_order_id = item.source_order_id
    identity = _get_or_create_identity(session, namespace, source_order_id)
    replay_identity = canonical_replay_identity(
        namespace, source_order_id, item.source_version, item.source_row_hash
    )
    digest = replay_identity_digest(
        namespace, source_order_id, item.source_version, item.source_row_hash
    )
    existing = session.scalar(
        select(OrderObservation).where(OrderObservation.replay_identity_digest == digest)
    )
    if existing is not None:
        _receipt(session, existing, _text(batch_id) or "")
        return existing

    staged = OrderObservation(
        source_order_identity_id=identity.id,
        source_namespace=namespace,
        source_order_id=source_order_id,
        source_version=item.source_version,
        source_row_hash=item.source_row_hash,
        replay_identity=replay_identity,
        replay_identity_digest=digest,
        source_facts=json.loads(canonical_source_facts(item.source_facts)),
        order_number=item.order_number,
        order_status=OrderStatus(item.status) if item.status in _ORDER_STATUSES else None,
        region=item.region,
        source_warehouse_id=item.source_warehouse_id,
        ordered_at=item.ordered_at,
        promised_at=item.promised_at,
        fulfilled_at=item.fulfilled_at,
        total_amount=item.total_amount,
        currency=item.currency,
        status=assessment.status,
        source_status=assessment.source_status,
        capabilities=assessment.capabilities,
        non_promotion_reasons=list(assessment.invalid_reasons or assessment.non_promotion_reasons),
    )
    for attempt in range(_MAX_CONFLICT_RETRIES):
        try:
            with session.begin_nested():
                session.add(staged)
                session.flush()
        except IntegrityError:
            existing = session.scalar(
                select(OrderObservation).where(OrderObservation.replay_identity_digest == digest)
            )
            if existing is not None:
                _receipt(session, existing, _text(batch_id) or "")
                return existing
            if attempt == _MAX_CONFLICT_RETRIES - 1:
                raise
        else:
            _receipt(session, staged, _text(batch_id) or "")
            return staged
    raise RuntimeError("bounded observation staging retries exhausted")


def _same(value_a: Any, value_b: Any) -> bool:
    if isinstance(value_a, datetime) and isinstance(value_b, datetime):

        def as_utc(value: datetime) -> datetime:
            if value.tzinfo is None or value.utcoffset() is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)

        return as_utc(value_a) == as_utc(value_b)
    return value_a == value_b


def _projection_matches(order: Order, observation: OrderObservation) -> bool:
    return all(
        _same(getattr(order, field), expected)
        for field, expected in {
            "source_order_identity_id": observation.source_order_identity_id,
            "source_namespace": observation.source_namespace,
            "source_order_id": observation.source_order_id,
            "order_number": observation.order_number,
            "status": observation.order_status,
            "region": observation.region,
            "warehouse_id": observation.warehouse_id,
            "source_warehouse_id": observation.source_warehouse_id,
            "ordered_at": observation.ordered_at,
            "promised_at": observation.promised_at,
            "fulfilled_at": observation.fulfilled_at,
            "total_amount": observation.total_amount,
            "currency": observation.currency,
        }.items()
    )


def _version_outcome(
    version_comparator: Callable[[str | None, str | None], VersionComparison | str | bool] | None,
    current: str | None,
    incoming: str | None,
) -> VersionComparison:
    if version_comparator is None:
        return VersionComparison.UNORDERED
    try:
        outcome = version_comparator(current, incoming)
        if isinstance(outcome, VersionComparison):
            return outcome
        if isinstance(outcome, str):
            return VersionComparison(outcome)
        # Compatibility for the prior boolean API: false is stale, never newer.
        if isinstance(outcome, bool):
            return VersionComparison.INCOMING_NEWER if outcome else VersionComparison.CURRENT_NEWER
    except (Exception, ValueError):
        return VersionComparison.UNORDERED
    return VersionComparison.UNORDERED


def _block(observation: OrderObservation, reason: str) -> None:
    observation.status = OrderObservationStatus.CONFLICT_BLOCKED
    reasons = list(observation.non_promotion_reasons or [])
    if reason not in reasons:
        reasons.append(reason)
    observation.non_promotion_reasons = reasons


def _same_version_conflict(
    observations: Iterable[OrderObservation], current: OrderObservation
) -> str | None:
    for other in observations:
        if other.id == current.id or other.source_row_hash == current.source_row_hash:
            continue
        if other.source_version == current.source_version:
            return (
                "SOURCE_VERSION_CONTENT_CONFLICT"
                if current.source_version is not None
                else "UNVERSIONED_CONTENT_CONFLICT"
            )
    return None


def promote_order_observation(
    session: Session,
    observation: OrderObservation,
    *,
    version_comparator: Callable[[str | None, str | None], VersionComparison | str | bool]
    | None = None,
) -> Order | None:
    """Promote evidence only when projection semantics are explicitly proven.

    ``version_comparator(current_version, incoming_version)`` must return true
    only when the incoming source revision is newer. Without it, revisions are
    never ordered by lexical/numeric/arrival/batch/timestamp heuristics.
    """

    locked = session.scalar(
        select(OrderObservation).where(OrderObservation.id == observation.id).with_for_update()
    )
    if locked is None:
        return None
    observation = locked
    if observation.status is OrderObservationStatus.PROMOTED and observation.promoted_order_id:
        order = session.get(Order, observation.promoted_order_id)
        if order is not None and all(
            (
                observation.promoted_order_id == order.id,
                order.current_observation_id == observation.id,
                order.source_order_identity_id == observation.source_order_identity_id,
                order.source_namespace == observation.source_namespace,
                order.source_order_id == observation.source_order_id,
                order.projected_source_version == observation.source_version,
                _projection_matches(order, observation),
            )
        ):
            return order
        return None
    if observation.status in {
        OrderObservationStatus.REJECTED_INVALID,
        OrderObservationStatus.CONFLICT_BLOCKED,
    }:
        return None

    identity = session.scalar(
        select(SourceOrderIdentity)
        .where(SourceOrderIdentity.id == observation.source_order_identity_id)
        .with_for_update()
    )
    if identity is None:
        _block(observation, "MISSING_SOURCE_ORDER_IDENTITY")
        return None
    all_observations = list(
        session.scalars(
            select(OrderObservation).where(OrderObservation.source_order_identity_id == identity.id)
        )
    )
    conflict = _same_version_conflict(all_observations, observation)
    if conflict:
        _block(observation, conflict)
        return None

    warehouse = session.scalar(
        select(Warehouse).where(Warehouse.source_warehouse_id == observation.source_warehouse_id)
    )
    if warehouse is not None:
        observation.warehouse_id = warehouse.id
        observation.capabilities = dict(observation.capabilities or {})
        observation.capabilities["warehouse"] = WarehouseCapability.VALIDATED.value
        observation.non_promotion_reasons = [
            reason
            for reason in observation.non_promotion_reasons or []
            if reason not in {"MISSING_WAREHOUSE", "UNKNOWN_WAREHOUSE"}
        ]

    if warehouse is None:
        observation.status = OrderObservationStatus.CONTEXT_INCOMPLETE
        observation.capabilities = dict(observation.capabilities or {})
        capability = (
            WarehouseCapability.CONTEXT_UNAVAILABLE.value
            if not observation.source_warehouse_id
            else WarehouseCapability.UNKNOWN.value
        )
        observation.capabilities["warehouse"] = capability
        reasons = list(observation.non_promotion_reasons or [])
        reason = "MISSING_WAREHOUSE" if not observation.source_warehouse_id else "UNKNOWN_WAREHOUSE"
        if reason not in reasons:
            reasons.append(reason)
        observation.non_promotion_reasons = reasons
        return None

    if observation.status is not OrderObservationStatus.NORMALIZED:
        if observation.non_promotion_reasons:
            observation.status = OrderObservationStatus.CONTEXT_INCOMPLETE
            return None
        observation.status = OrderObservationStatus.NORMALIZED
    order = session.scalar(
        select(Order).where(Order.source_order_identity_id == identity.id).with_for_update()
    )
    if order is None:
        order = session.scalar(
            select(Order)
            .where(
                Order.source_namespace == identity.source_namespace,
                Order.source_order_id == identity.source_order_id,
            )
            .with_for_update()
        )
    if order is None and observation.source_version is not None:
        other_versions = {
            item.source_version
            for item in all_observations
            if item.id != observation.id and item.source_version != observation.source_version
        }
        if other_versions and version_comparator is None:
            outcome = _version_outcome(version_comparator, None, observation.source_version)
            if outcome is not VersionComparison.INCOMING_NEWER:
                _block(observation, "SOURCE_VERSION_ORDER_UNPROVEN")
                return None
    if order is not None:
        if order.source_order_identity_id is None:
            order.source_order_identity_id = identity.id
        outcome = VersionComparison.SAME
        if order.projected_source_version != observation.source_version:
            outcome = _version_outcome(
                version_comparator,
                order.projected_source_version,
                observation.source_version,
            )
            if outcome is not VersionComparison.INCOMING_NEWER:
                _block(
                    observation,
                    "STALE_SOURCE_VERSION"
                    if outcome is VersionComparison.CURRENT_NEWER
                    else "SOURCE_VERSION_ORDER_UNPROVEN",
                )
                return None
        if outcome is VersionComparison.SAME and not _projection_matches(order, observation):
            _block(observation, "SOURCE_VERSION_CONTENT_CONFLICT")
            return None
        if outcome is VersionComparison.INCOMING_NEWER:
            assert observation.order_number is not None
            assert observation.order_status is not None
            assert observation.region is not None
            assert observation.ordered_at is not None
            assert observation.promised_at is not None
            assert observation.total_amount is not None
            assert observation.currency is not None
            order.source_namespace = observation.source_namespace
            order.source_order_id = observation.source_order_id
            order.order_number = observation.order_number
            order.status = observation.order_status
            order.region = observation.region
            order.warehouse_id = warehouse.id
            order.source_warehouse_id = observation.source_warehouse_id
            order.ordered_at = observation.ordered_at
            order.promised_at = observation.promised_at
            order.fulfilled_at = observation.fulfilled_at
            order.total_amount = observation.total_amount
            order.currency = observation.currency
    if order is None:
        order = Order(
            source_namespace=identity.source_namespace,
            source_order_id=identity.source_order_id,
            source_order_identity_id=identity.id,
            projected_source_version=observation.source_version,
            order_number=observation.order_number,
            status=observation.order_status,
            region=observation.region,
            warehouse_id=warehouse.id,
            source_warehouse_id=observation.source_warehouse_id,
            ordered_at=observation.ordered_at,
            promised_at=observation.promised_at,
            fulfilled_at=observation.fulfilled_at,
            total_amount=observation.total_amount,
            currency=observation.currency,
        )
        try:
            with session.begin_nested():
                session.add(order)
                session.flush()
        except IntegrityError:
            conflicting = session.scalar(
                select(Order).where(
                    or_(
                        and_(
                            Order.source_namespace == identity.source_namespace,
                            Order.source_order_id == identity.source_order_id,
                        ),
                        and_(
                            Order.source_namespace == identity.source_namespace,
                            Order.order_number == observation.order_number,
                        ),
                    )
                )
            )
            if conflicting is None:
                raise
            if conflicting.source_order_identity_id not in (None, identity.id):
                observation.status = OrderObservationStatus.REJECTED_INVALID
                observation.non_promotion_reasons = ["OPERATIONAL_ORDER_CONFLICT"]
                return None
            order = conflicting

    if not _projection_matches(order, observation):
        # An INSERT race or an ambiguous existing legacy row must never be called promoted.
        _block(observation, "PROJECTION_MISMATCH")
        return None
    order.source_order_identity_id = identity.id
    order.projected_source_version = observation.source_version
    previous_current_id = order.current_observation_id
    if previous_current_id is not None and previous_current_id != observation.id:
        previous_current = session.scalar(
            select(OrderObservation)
            .where(OrderObservation.id == previous_current_id)
            .with_for_update()
        )
        if previous_current is not None:
            previous_current.superseded_by_observation_id = observation.id
    order.current_observation_id = observation.id
    observation.promoted_order_id = order.id
    observation.status = OrderObservationStatus.PROMOTED
    observation.non_promotion_reasons = []
    return order


def promote_staged_order_observations(
    session: Session,
    observations: Iterable[OrderObservation],
    *,
    version_comparator: Callable[[str | None, str | None], VersionComparison | str | bool]
    | None = None,
) -> list[Order]:
    """Promote supplied observations without treating iteration order as source order."""

    promoted: list[Order] = []
    for observation in observations:
        order = promote_order_observation(
            session, observation, version_comparator=version_comparator
        )
        if order is not None:
            promoted.append(order)
    return promoted


__all__ = [
    "NormalizedOrderObservation",
    "OrderObservationAssessment",
    "assess_order_observation",
    "canonical_replay_identity",
    "canonical_source_facts",
    "promote_order_observation",
    "promote_staged_order_observations",
    "replay_identity_digest",
    "source_row_hash",
    "stage_order_observation",
]
