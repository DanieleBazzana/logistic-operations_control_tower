"""Immutable contracts for deterministic exception detection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from control_tower.enums import ExceptionType


def normalize_as_of(value: datetime) -> datetime:
    """Require and normalize an explicit timezone-aware evaluation instant."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as_of must include a timezone")
    return value.astimezone(timezone.utc)


def canonical_facts(facts: tuple[tuple[str, str], ...]) -> str:
    """Serialize source facts in a stable form for fingerprinting."""

    return json.dumps(dict(sorted(facts)), ensure_ascii=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class ExceptionDetection:
    """A complete, immutable finding produced by one M03 rule."""

    exception_type: ExceptionType
    issue_key: str
    entity_type: str
    entity_id: str
    expected_resolution: datetime | None
    business_impact: str
    root_cause: str
    recommended_action: str
    revenue_at_risk: Decimal = Decimal("0")
    orders_affected: int = 0
    confidence: Decimal = Decimal("1.0000")
    overdue_hours: Decimal = Decimal("0")
    warehouse_id: int | None = None
    product_id: int | None = None
    source_facts: tuple[tuple[str, str], ...] = ()

    @property
    def fingerprint(self) -> str:
        """Return a stable digest of source facts, deliberately excluding ``as_of``."""

        return hashlib.sha256(canonical_facts(self.source_facts).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class DetectionRunResult:
    """Result of one deterministic detection/persistence run."""

    as_of: datetime
    detections: tuple[ExceptionDetection, ...]
    created: int = 0
    updated: int = 0
    skipped: int = 0

    @property
    def count(self) -> int:
        return len(self.detections)

    @property
    def persisted(self) -> int:
        return self.created + self.updated


Detection = ExceptionDetection


__all__ = [
    "Detection",
    "DetectionRunResult",
    "ExceptionDetection",
    "canonical_facts",
    "normalize_as_of",
]
