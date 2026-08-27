"""Strict, side-effect-free validation for source rows."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from control_tower.ingestion.contracts import (
    SCHEMAS,
    SOURCE_FIELDS,
    FieldSpec,
    Rejection,
    ValidationResult,
)
from control_tower.ingestion.normalization import canonical_row
from control_tower.synthetic.artifacts import ARTIFACT_COLUMNS


def _source_id(artifact: str, row: dict[str, Any]) -> str | None:
    value = row.get(SOURCE_FIELDS.get(artifact, ""))
    return str(value).strip() if value not in (None, "") else None


def _known_values(known_ids: Any, relation: str) -> set[str] | None:
    if known_ids is None:
        return None
    if isinstance(known_ids, dict):
        values = known_ids.get(relation)
        return set(values) if values is not None else None
    return set(known_ids)


def _rejection(
    artifact: str,
    row_number: int,
    row: dict[str, Any],
    code: str,
    field: str | None,
    message: str,
) -> Rejection:
    return Rejection(artifact, row_number, _source_id(artifact, row), code, field, message)


def _check_rules(artifact: str, row_number: int, row: dict[str, Any]) -> list[Rejection]:
    errors: list[Rejection] = []
    if (
        artifact == "oms/order_items.csv"
        and row.get("fulfilled_quantity") is not None
        and row.get("ordered_quantity") is not None
    ):
        if row["fulfilled_quantity"] > row["ordered_quantity"]:
            errors.append(
                _rejection(
                    artifact,
                    row_number,
                    row,
                    "QUANTITY_BOUNDS",
                    "fulfilled_quantity",
                    "fulfilled quantity exceeds ordered quantity",
                )
            )
    if (
        artifact == "erp/purchase_order_items.csv"
        and row.get("received_quantity") is not None
        and row.get("ordered_quantity") is not None
    ):
        if row["received_quantity"] > row["ordered_quantity"]:
            errors.append(
                _rejection(
                    artifact,
                    row_number,
                    row,
                    "QUANTITY_BOUNDS",
                    "received_quantity",
                    "received quantity exceeds ordered quantity",
                )
            )
    if (
        artifact == "wms/inventory.csv"
        and row.get("reserved") is not None
        and row.get("on_hand") is not None
        and row["reserved"] > row["on_hand"]
    ):
        errors.append(
            _rejection(
                artifact,
                row_number,
                row,
                "QUANTITY_BOUNDS",
                "reserved",
                "reserved exceeds on-hand inventory",
            )
        )
    comparisons = {
        "oms/orders.csv": (("fulfilled_at", "ordered_at"),),
        "erp/purchase_orders.csv": (("received_at", "ordered_at"),),
        "carrier/shipments.csv": (("delivered_at", "shipped_at"),),
    }.get(artifact, ())
    for later, earlier in comparisons:
        if (
            row.get(later) is not None
            and row.get(earlier) is not None
            and row[later] < row[earlier]
        ):
            errors.append(
                _rejection(
                    artifact,
                    row_number,
                    row,
                    "TIMESTAMP_ORDER",
                    later,
                    f"{later} precedes {earlier}",
                )
            )
    return errors


def validate_rows(
    artifact: str,
    rows: Iterable[dict[str, Any]],
    *,
    known_ids: dict[str, set[str]] | set[str] | None = None,
) -> ValidationResult:
    """Normalize and validate rows from one known artifact without database access."""

    if artifact not in SCHEMAS:
        raise ValueError(f"unknown artifact: {artifact}")
    spec: dict[str, FieldSpec] = SCHEMAS[artifact]
    result = ValidationResult(artifact=artifact)
    seen: dict[str, dict[str, Any]] = {}
    inventory_keys: set[tuple[str, str]] = set()
    natural_seen: dict[tuple[str, str, datetime], dict[str, Any]] = {}
    for row_number, raw in enumerate(rows, start=2):
        errors: list[Rejection] = []
        raw = dict(raw)
        for field_name, field_spec in spec.items():
            value = raw.get(field_name)
            if field_spec.required and (value is None or not str(value).strip()):
                errors.append(
                    _rejection(
                        artifact, row_number, raw, "REQUIRED", field_name, "required field is blank"
                    )
                )
        try:
            row = canonical_row(raw, spec)
        except (TypeError, ValueError) as error:
            message = str(error)
            code = "NAIVE_TIMESTAMP" if "timezone" in message else "TYPE"
            field = next(
                (
                    name
                    for name, field_spec in spec.items()
                    if field_spec.kind
                    in {
                        "timestamp",
                        "money",
                        "positive_quantity",
                        "nonnegative_quantity",
                        "int",
                        "bool",
                    }
                    and raw.get(name)
                ),
                None,
            )
            errors.append(_rejection(artifact, row_number, raw, code, field, message))
            result.rejections.extend(errors)
            continue
        for field_name, field_spec in spec.items():
            value = row.get(field_name)
            if value is None:
                continue
            if field_spec.enum and value not in field_spec.enum:
                errors.append(
                    _rejection(
                        artifact,
                        row_number,
                        raw,
                        "ENUM",
                        field_name,
                        f"unsupported value {value!r}",
                    )
                )
            if field_spec.kind == "positive_quantity" and value <= 0:
                errors.append(
                    _rejection(
                        artifact,
                        row_number,
                        raw,
                        "POSITIVE_QUANTITY",
                        field_name,
                        "quantity must be positive",
                    )
                )
            if field_spec.kind == "nonnegative_quantity" and value < 0:
                errors.append(
                    _rejection(
                        artifact,
                        row_number,
                        raw,
                        "NONNEGATIVE_QUANTITY",
                        field_name,
                        "quantity must not be negative",
                    )
                )
            if field_spec.kind == "money" and value < 0:
                errors.append(
                    _rejection(
                        artifact,
                        row_number,
                        raw,
                        "NONNEGATIVE_MONEY",
                        field_name,
                        "money must not be negative",
                    )
                )
            if field_spec.relation and value not in (
                _known_values(known_ids, field_spec.relation) or set()
            ):
                if known_ids is not None:
                    errors.append(
                        _rejection(
                            artifact,
                            row_number,
                            raw,
                            "PARENT_SOURCE_ID",
                            field_name,
                            f"unknown {field_spec.relation} source ID {value}",
                        )
                    )
        errors.extend(_check_rules(artifact, row_number, row))
        if errors:
            result.rejections.extend(errors)
            continue
        source_field = SOURCE_FIELDS.get(artifact)
        if source_field:
            source_id = str(row[source_field])
            if source_id in seen:
                if seen[source_id] == row:
                    result.duplicate_identical += 1
                    continue
                result.rejections.append(
                    _rejection(
                        artifact,
                        row_number,
                        raw,
                        "DUPLICATE_CONFLICT",
                        source_field,
                        "duplicate source ID has conflicting values",
                    )
                )
                continue
            seen[source_id] = row
        if artifact == "wms/inventory.csv":
            natural_key = (row["source_product_id"], row["source_warehouse_id"])
            if natural_key in inventory_keys:
                result.rejections.append(
                    _rejection(
                        artifact,
                        row_number,
                        raw,
                        "INVENTORY_MULTIPLE_SNAPSHOTS",
                        "observed_at",
                        "multiple inventory snapshots for one product and warehouse "
                        "are not allowed in a bundle",
                    )
                )
                continue
            inventory_keys.add(natural_key)
            key = (*natural_key, row["observed_at"])
            if key in natural_seen and natural_seen[key] != row:
                result.rejections.append(
                    _rejection(
                        artifact,
                        row_number,
                        raw,
                        "INVENTORY_CONFLICT",
                        "observed_at",
                        "inventory snapshot conflicts at the same natural key and time",
                    )
                )
                continue
            if key in natural_seen:
                result.duplicate_identical += 1
                continue
            natural_seen[key] = row
        result.rows.append(row)
    return result


def validate_bundle_rows(
    rows_by_artifact: dict[str, list[dict[str, Any]]],
    *,
    headers: dict[str, tuple[str, ...] | None] | None = None,
    manifest_artifacts: Iterable[str] | None = None,
) -> dict[str, ValidationResult]:
    """Validate all bundle rows in dependency order and enforce source joins."""

    known: dict[str, set[str]] = {
        "products": set(),
        "warehouses": set(),
        "suppliers": set(),
        "orders": set(),
        "purchase_orders": set(),
    }
    results: dict[str, ValidationResult] = {}
    expected_artifacts = set(manifest_artifacts or ())
    artifact_names = set(rows_by_artifact) | expected_artifacts
    order = (
        "oms/products.csv",
        "wms/warehouses.csv",
        "erp/suppliers.csv",
        "oms/orders.csv",
        "oms/order_items.csv",
        "erp/purchase_orders.csv",
        "erp/purchase_order_items.csv",
        "wms/inventory.csv",
        "wms/inventory_movements.csv",
        "carrier/shipments.csv",
    )
    for artifact in order:
        if artifact not in rows_by_artifact:
            if artifact in expected_artifacts:
                results[artifact] = ValidationResult(
                    artifact=artifact,
                    rejections=[
                        Rejection(
                            artifact,
                            1,
                            None,
                            "MISSING_ARTIFACT",
                            None,
                            "manifest declares an artifact that is not present in the bundle",
                        )
                    ],
                )
            continue
        result = validate_rows(artifact, rows_by_artifact[artifact], known_ids=known)
        if headers is not None and artifact in headers:
            result.rejections[0:0] = _header_rejections(artifact, headers[artifact])
        results[artifact] = result
        source_field = SOURCE_FIELDS.get(artifact)
        relation_name = {
            "oms/products.csv": "products",
            "wms/warehouses.csv": "warehouses",
            "erp/suppliers.csv": "suppliers",
            "oms/orders.csv": "orders",
            "erp/purchase_orders.csv": "purchase_orders",
        }.get(artifact)
        if source_field and relation_name:
            known[relation_name].update(str(row[source_field]) for row in result.rows)
    for artifact in sorted(artifact_names - set(order)):
        results[artifact] = ValidationResult(
            artifact=artifact,
            rejections=[
                Rejection(
                    artifact,
                    1,
                    None,
                    "UNKNOWN_ARTIFACT",
                    None,
                    "artifact is not supported by the ingestion contract",
                )
            ],
        )
    return results


def _header_rejections(artifact: str, header: tuple[str, ...] | None) -> list[Rejection]:
    """Return structured errors when an artifact header is not an exact contract match."""

    expected = ARTIFACT_COLUMNS[artifact]
    if header is None or not header or any(not column.strip() for column in header):
        return [
            Rejection(
                artifact,
                1,
                None,
                "MALFORMED_HEADER",
                None,
                f"header must exactly match {expected!r}",
            )
        ]
    if len(set(header)) != len(header):
        return [
            Rejection(
                artifact,
                1,
                None,
                "MALFORMED_HEADER",
                None,
                "header contains duplicate columns",
            )
        ]
    rejections = [
        Rejection(
            artifact,
            1,
            None,
            "UNKNOWN_COLUMN",
            column,
            f"column {column!r} is not declared for this artifact",
        )
        for column in header
        if column not in expected
    ]
    rejections.extend(
        Rejection(
            artifact,
            1,
            None,
            "MISSING_COLUMN",
            column,
            f"required contract column {column!r} is missing",
        )
        for column in expected
        if column not in header
    )
    if not rejections and header != expected:
        rejections.append(
            Rejection(
                artifact,
                1,
                None,
                "MALFORMED_HEADER",
                None,
                f"header order must exactly match {expected!r}",
            )
        )
    return rejections


__all__ = ["validate_bundle_rows", "validate_rows"]
