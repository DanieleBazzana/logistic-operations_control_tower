"""Atomic, PostgreSQL-only loading with source-ID idempotency."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from control_tower.config import Settings
from control_tower.db import create_db_engine, create_session_factory
from control_tower.ingestion.contracts import (
    SCHEMAS,
    IngestionResult,
    Rejection,
    SourceSummary,
)
from control_tower.ingestion.readers import read_bundle
from control_tower.ingestion.validation import validate_bundle_rows
from control_tower.models import (
    Inventory,
    InventoryMovement,
    Order,
    OrderItem,
    Product,
    PurchaseOrder,
    PurchaseOrderItem,
    Shipment,
    Supplier,
    Warehouse,
)
from control_tower.synthetic.artifacts import manifest_identity

ORDER = (
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
MODEL_BY_ARTIFACT = {
    "oms/products.csv": (Product, "source_product_id"),
    "wms/warehouses.csv": (Warehouse, "source_warehouse_id"),
    "erp/suppliers.csv": (Supplier, "source_supplier_id"),
    "oms/orders.csv": (Order, "source_order_id"),
    "oms/order_items.csv": (OrderItem, "source_order_item_id"),
    "erp/purchase_orders.csv": (PurchaseOrder, "source_purchase_order_id"),
    "erp/purchase_order_items.csv": (PurchaseOrderItem, "source_purchase_order_item_id"),
    "wms/inventory_movements.csv": (InventoryMovement, "source_movement_id"),
    "carrier/shipments.csv": (Shipment, "source_shipment_id"),
}


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _value(value: Any) -> Any:
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, datetime):
        return _utc(value)
    return value


def _relation_fields(artifact: str) -> dict[str, tuple[str, str]]:
    return {
        source_field: (source_field.removeprefix("source_"), field_spec.relation)
        for source_field, field_spec in SCHEMAS.get(artifact, {}).items()
        if field_spec.relation is not None
    }


def _equal(artifact: str, row: dict[str, Any], obj: Any) -> bool:
    relation_fields = _relation_fields(artifact)
    for field, expected in row.items():
        if field in relation_fields:
            db_field, _ = relation_fields[field]
            actual = getattr(obj, db_field.removesuffix("_id"), None)
            actual = getattr(actual, field, None) if actual is not None else None
        else:
            actual = getattr(obj, field, None)
        if _value(actual) != _value(expected):
            return False
    return True


def _fields_for(model: type[Any]) -> tuple[str, ...]:
    ignored = {"id", "created_at", "updated_at", "available"}
    return tuple(column.name for column in model.__table__.columns if column.name not in ignored)


def _row_to_model(artifact: str, row: dict[str, Any], ids: dict[str, dict[str, int]]) -> Any:
    values = dict(row)
    relation_fields = _relation_fields(artifact)
    for source_field, (db_field, collection) in relation_fields.items():
        if source_field in values:
            values[db_field] = ids[collection][values.pop(source_field)]
    if artifact == "oms/products.csv":
        return Product(**values)
    if artifact == "wms/warehouses.csv":
        return Warehouse(**values)
    if artifact == "erp/suppliers.csv":
        return Supplier(**values)
    if artifact == "oms/orders.csv":
        return Order(**values)
    if artifact == "oms/order_items.csv":
        return OrderItem(**values)
    if artifact == "erp/purchase_orders.csv":
        return PurchaseOrder(**values)
    if artifact == "erp/purchase_order_items.csv":
        return PurchaseOrderItem(**values)
    if artifact == "wms/inventory.csv":
        return Inventory(**values)
    if artifact == "wms/inventory_movements.csv":
        return InventoryMovement(**values)
    if artifact == "carrier/shipments.csv":
        return Shipment(**values)
    raise ValueError(f"unknown artifact: {artifact}")


def _rejection(
    artifact: str, code: str, field: str | None, message: str, source_id: str | None = None
) -> Rejection:
    return Rejection(artifact, 1, source_id, code, field, message)


def _build_summaries(bundle: Any, results: dict[str, Any]) -> list[SourceSummary]:
    summaries: list[SourceSummary] = []
    artifacts = list(ORDER) + sorted(set(results) - set(ORDER))
    for artifact in artifacts:
        if artifact not in results:
            continue
        result = results[artifact]
        summaries.append(
            SourceSummary(
                source=artifact,
                rows_read=len(bundle.rows.get(artifact, [])),
                accepted=result.accepted,
                rejected=len(result.rejections),
                rejection_details=list(result.rejections),
            )
        )
    return summaries


def _manifest_rejections(bundle: Any) -> list[Rejection]:
    rejections: list[Rejection] = []
    for artifact, metadata in bundle.manifest.get("artifacts", {}).items():
        actual = len(bundle.rows.get(artifact, []))
        expected = metadata.get("row_count") if isinstance(metadata, dict) else None
        if expected != actual:
            rejections.append(
                _rejection(
                    artifact,
                    "MANIFEST_COUNT",
                    None,
                    f"manifest says {expected} rows but file has {actual}",
                )
            )
    return rejections


def _preflight(
    session: Session,
    results: dict[str, Any],
    summaries: dict[str, SourceSummary],
) -> list[Rejection]:
    conflicts: list[Rejection] = []
    for artifact in ORDER:
        if artifact not in results or artifact == "wms/inventory.csv":
            continue
        model, source_field = MODEL_BY_ARTIFACT[artifact]
        existing = {getattr(obj, source_field): obj for obj in session.scalars(select(model)).all()}
        for row in results[artifact].rows:
            current = existing.get(row[source_field])
            if current is not None and not _equal(artifact, row, current):
                conflicts.append(
                    _rejection(
                        artifact,
                        "SOURCE_ID_CONFLICT",
                        source_field,
                        "same source ID has conflicting persisted values",
                        row[source_field],
                    )
                )
            elif current is not None:
                summaries[artifact].skipped += 1
            else:
                summaries[artifact].inserted += 1
    if "wms/inventory.csv" in results:
        products = {obj.source_product_id: obj.id for obj in session.scalars(select(Product)).all()}
        warehouses = {
            obj.source_warehouse_id: obj.id for obj in session.scalars(select(Warehouse)).all()
        }
        existing_inventory = {
            (obj.product_id, obj.warehouse_id): obj
            for obj in session.scalars(select(Inventory)).all()
        }
        for row in results["wms/inventory.csv"].rows:
            key = (
                products.get(row["source_product_id"]),
                warehouses.get(row["source_warehouse_id"]),
            )
            current = existing_inventory.get(key)
            if current is None:
                summaries["wms/inventory.csv"].inserted += 1
            elif row["observed_at"] > _utc(current.observed_at):
                summaries["wms/inventory.csv"].updated += 1
            elif row["observed_at"] < _utc(current.observed_at):
                conflicts.append(
                    _rejection(
                        "wms/inventory.csv",
                        "STALE_INVENTORY",
                        "observed_at",
                        "older inventory snapshot rejected",
                        f"{row['source_product_id']}-{row['source_warehouse_id']}",
                    )
                )
            elif not _equal("wms/inventory.csv", row, current):
                conflicts.append(
                    _rejection(
                        "wms/inventory.csv",
                        "INVENTORY_CONFLICT",
                        "observed_at",
                        "equal-time inventory snapshot conflicts",
                        f"{row['source_product_id']}-{row['source_warehouse_id']}",
                    )
                )
            else:
                summaries["wms/inventory.csv"].skipped += 1
    return conflicts


def _apply(session: Session, results: dict[str, Any], summaries: dict[str, SourceSummary]) -> None:
    ids: dict[str, dict[str, int]] = {
        "products": {},
        "warehouses": {},
        "suppliers": {},
        "orders": {},
        "purchase_orders": {},
    }
    for artifact, collection in (
        ("oms/products.csv", "products"),
        ("wms/warehouses.csv", "warehouses"),
        ("erp/suppliers.csv", "suppliers"),
    ):
        if artifact not in results:
            continue
        model, source_field = MODEL_BY_ARTIFACT[artifact]
        existing = {getattr(obj, source_field): obj for obj in session.scalars(select(model)).all()}
        for row in results[artifact].rows:
            if row[source_field] not in existing:
                obj = _row_to_model(artifact, row, ids)
                session.add(obj)
                session.flush()
                existing[row[source_field]] = obj
        ids[collection] = {key: obj.id for key, obj in existing.items()}
    for artifact, collection in (
        ("oms/orders.csv", "orders"),
        ("erp/purchase_orders.csv", "purchase_orders"),
    ):
        if artifact not in results:
            continue
        model, source_field = MODEL_BY_ARTIFACT[artifact]
        existing = {getattr(obj, source_field): obj for obj in session.scalars(select(model)).all()}
        for row in results[artifact].rows:
            if row[source_field] not in existing:
                obj = _row_to_model(artifact, row, ids)
                session.add(obj)
                session.flush()
                existing[row[source_field]] = obj
        ids[collection] = {key: obj.id for key, obj in existing.items()}
    for artifact in ORDER:
        if artifact not in results or artifact in {
            "oms/products.csv",
            "wms/warehouses.csv",
            "erp/suppliers.csv",
            "oms/orders.csv",
            "erp/purchase_orders.csv",
            "wms/inventory.csv",
        }:
            continue
        model, source_field = MODEL_BY_ARTIFACT[artifact]
        existing = {getattr(obj, source_field): obj for obj in session.scalars(select(model)).all()}
        for row in results[artifact].rows:
            if row[source_field] not in existing:
                session.add(_row_to_model(artifact, row, ids))
        session.flush()
    if "wms/inventory.csv" in results:
        product_ids = ids["products"]
        warehouse_ids = ids["warehouses"]
        existing = {
            (obj.product_id, obj.warehouse_id): obj
            for obj in session.scalars(select(Inventory)).all()
        }
        for row in results["wms/inventory.csv"].rows:
            key = (product_ids[row["source_product_id"]], warehouse_ids[row["source_warehouse_id"]])
            current = existing.get(key)
            if current is None:
                session.add(_row_to_model("wms/inventory.csv", row, ids))
            elif row["observed_at"] > _utc(current.observed_at):
                current.on_hand = row["on_hand"]
                current.reserved = row["reserved"]
                current.observed_at = row["observed_at"]
        session.flush()


def ingest(
    input_dir: str | Path,
    *,
    settings: Settings | None = None,
    engine: Engine | None = None,
) -> IngestionResult:
    """Validate then atomically ingest a generated bundle into PostgreSQL."""

    bundle = read_bundle(input_dir)
    validation = validate_bundle_rows(
        bundle.rows,
        headers=bundle.headers,
        manifest_artifacts=(bundle.manifest.get("artifacts", {}) or {}).keys(),
    )
    summaries = _build_summaries(bundle, validation)
    summary_by_artifact = {summary.source: summary for summary in summaries}
    manifest_rejections = _manifest_rejections(bundle)
    if manifest_rejections:
        summary_by_artifact[manifest_rejections[0].artifact].rejection_details.extend(
            manifest_rejections
        )
        summary_by_artifact[manifest_rejections[0].artifact].rejected += len(manifest_rejections)
    all_rejections = [
        rejection for result in validation.values() for rejection in result.rejections
    ] + manifest_rejections
    manifest = bundle.manifest
    identity = manifest.get("manifest_identity") or manifest_identity(
        {key: value for key, value in manifest.items() if key != "manifest_identity"}
    )
    as_of = datetime.fromisoformat(str(manifest["as_of"]).replace("Z", "+00:00")).astimezone(
        timezone.utc
    )
    result = IngestionResult(identity, int(manifest["seed"]), as_of, summaries, False, manifest)
    if all_rejections:
        return result
    configured_engine = engine or create_db_engine(settings)
    if configured_engine.dialect.name != "postgresql":
        raise ValueError("M02 ingestion supports PostgreSQL only")
    session_factory = create_session_factory(engine=configured_engine)
    with session_factory() as session:
        with session.begin():
            conflicts = _preflight(session, validation, summary_by_artifact)
            if conflicts:
                for conflict in conflicts:
                    summary = summary_by_artifact[conflict.artifact]
                    summary.rejected += 1
                    summary.conflicted += 1
                    summary.rejection_details.append(conflict)
                return result
            _apply(session, validation, summary_by_artifact)
            for artifact, summary in summary_by_artifact.items():
                model = MODEL_BY_ARTIFACT.get(artifact, (Inventory, ""))[0]
                summary.final_count = session.scalar(select(func.count()).select_from(model)) or 0
        result.committed = True
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest a deterministic M02 bundle")
    parser.add_argument("ingest", nargs="?", help="compatibility subcommand")
    parser.add_argument("--input-dir", default="data/generated")
    args = parser.parse_args(argv)
    result = ingest(args.input_dir)
    print(
        json.dumps(
            {
                "committed": result.committed,
                "summaries": [summary.as_dict() for summary in result.summaries],
            },
            default=str,
            indent=2,
        )
    )
    return 0 if result.committed else 1


if __name__ == "__main__":
    import json

    raise SystemExit(main())
