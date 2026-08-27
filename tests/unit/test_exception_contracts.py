from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from control_tower.enums import ExceptionType
from control_tower.exceptions.contracts import (
    DetectionRunResult,
    ExceptionDetection,
    normalize_as_of,
)

AS_OF = datetime(2025, 1, 15, 12, tzinfo=timezone.utc)


def make_detection() -> ExceptionDetection:
    return ExceptionDetection(
        exception_type=ExceptionType.SLA_BREACH_RISK,
        issue_key="ORDER:O-1",
        entity_type="ORDER",
        entity_id="O-1",
        expected_resolution=AS_OF,
        business_impact="order at risk",
        revenue_at_risk=Decimal("12.50"),
        orders_affected=1,
        root_cause="promise elapsed",
        recommended_action="expedite",
        confidence=Decimal("1.0000"),
        source_facts=(("promised_at", AS_OF.isoformat()), ("remaining", "1")),
    )


def test_detection_is_immutable_and_fingerprint_is_stable() -> None:
    detection = make_detection()

    assert detection.fingerprint == make_detection().fingerprint
    with pytest.raises((AttributeError, TypeError)):
        detection.issue_key = "ORDER:O-2"  # type: ignore[misc]


def test_detection_run_result_is_immutable_and_counts_detections() -> None:
    result = DetectionRunResult(as_of=AS_OF, detections=(make_detection(),))

    assert result.count == 1
    assert result.as_of == AS_OF
    with pytest.raises((AttributeError, TypeError)):
        result.detections = ()  # type: ignore[misc]


def test_as_of_requires_timezone_and_normalizes_to_utc() -> None:
    offset = timezone(timedelta(hours=1))
    assert normalize_as_of(datetime(2025, 1, 15, 13, tzinfo=offset)) == AS_OF
    with pytest.raises(ValueError, match="timezone"):
        normalize_as_of(datetime(2025, 1, 15, 12))
