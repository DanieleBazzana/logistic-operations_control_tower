"""Canonical scalar conversion used before validation and persistence."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


def trim(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def decimal(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as error:
        raise ValueError("must be a decimal") from error
    if not parsed.is_finite():
        raise ValueError("must be a finite decimal")
    return parsed


def timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def boolean(value: Any) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ValueError("must be boolean")


def canonical_row(row: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Trim every string and coerce fields according to a schema."""

    converted: dict[str, Any] = {}
    for field_name, field_spec in spec.items():
        value = trim(row.get(field_name))
        if value is None:
            converted[field_name] = None
            continue
        if field_spec.kind == "timestamp":
            converted[field_name] = timestamp(value)
        elif field_spec.kind in {"money", "positive_quantity", "nonnegative_quantity"}:
            converted[field_name] = decimal(value)
        elif field_spec.kind == "int":
            converted[field_name] = int(value)
        elif field_spec.kind == "bool":
            converted[field_name] = boolean(value)
        elif field_spec.enum:
            converted[field_name] = value.upper()
        else:
            converted[field_name] = value
    return converted


__all__ = ["boolean", "canonical_row", "decimal", "timestamp", "trim"]
