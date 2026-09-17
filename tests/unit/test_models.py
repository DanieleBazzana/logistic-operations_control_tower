from pathlib import Path

from sqlalchemy import BigInteger, Computed, Numeric, UniqueConstraint

from control_tower.db import Base
from control_tower.enums import ExceptionStatus, ExceptionType
from control_tower.models import (
    ExceptionHistory,
    ExceptionRecord,
    Inventory,
    Order,
    OrderItem,
    Product,
    PurchaseOrderItem,
    Shipment,
    SourceOrderIdentity,
    Warehouse,
)

EXPECTED_TABLES = {
    "dataset_versions",
    "dataset_activation",
    "products",
    "warehouses",
    "inventory",
    "inventory_movements",
    "orders",
    "source_order_identities",
    "order_observations",
    "order_observation_receipts",
    "order_items",
    "suppliers",
    "purchase_orders",
    "purchase_order_items",
    "shipments",
    "exceptions",
    "exception_history",
}


def test_metadata_contains_charter_minimum_tables() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_entities_use_bigint_identity_keys() -> None:
    for table in Base.metadata.tables.values():
        primary_key = next(iter(table.primary_key.columns))
        assert isinstance(primary_key.type, BigInteger)
        assert primary_key.identity is not None


def test_inventory_available_is_database_computed() -> None:
    available = Inventory.__table__.c.available

    assert isinstance(available.type, Numeric)
    assert isinstance(available.server_default, Computed)
    assert available.server_default.sqltext.text == "on_hand - reserved"


def test_relationships_cover_operational_joins() -> None:
    assert "items" in Order.__mapper__.relationships
    assert "order" in OrderItem.__mapper__.relationships
    assert "product" in OrderItem.__mapper__.relationships
    assert "warehouse" in Inventory.__mapper__.relationships
    assert "exception" in ExceptionHistory.__mapper__.relationships
    assert "order" in Shipment.__mapper__.relationships
    assert "purchase_order" in PurchaseOrderItem.__mapper__.relationships
    assert "source_identity" in Order.__mapper__.relationships
    assert "observations" in SourceOrderIdentity.__mapper__.relationships


def test_order_observation_linkage_constraints_are_present() -> None:
    order_columns = Order.__table__.c
    assert {"source_namespace", "source_order_identity_id", "projected_source_version"} <= {
        column.name for column in order_columns
    }
    assert order_columns.source_order_identity_id.nullable
    assert order_columns.projected_source_version.nullable
    assert order_columns.source_namespace.nullable


def test_strategy_two_observation_migration_declares_complete_schema() -> None:
    migration = Path("migrations/versions/20260911_01_order_observations.py").read_text(
        encoding="utf-8"
    )

    assert 'revision: str = "20260911_01"' in migration
    assert 'down_revision: str | None = "20250827_02"' in migration
    assert "CONFLICT_BLOCKED" in migration
    assert "CREATE TABLE source_order_identities" in migration
    assert "uq_source_order_identity_namespace_order" in migration
    assert "CREATE TABLE order_observation_receipts" in migration
    assert "uq_order_observation_receipt_observation_batch" in migration
    assert "replay_identity_digest VARCHAR(64) NOT NULL" in migration
    assert "uq_order_observations_replay_identity_digest" in migration
    assert "idempotency_key" not in migration
    assert "order_observations_source_batch" not in migration
    assert "ADD COLUMN source_namespace VARCHAR(100)" in migration
    assert "DEFAULT 'legacy'" not in migration


def test_exception_identity_allows_same_issue_key_for_different_types() -> None:
    active_index = next(
        index
        for index in ExceptionRecord.__table__.indexes
        if index.name == "uq_exceptions_active_type_issue_key"
    )

    assert {column.name for column in active_index.columns} == {
        "dataset_version_id",
        "exception_type",
        "issue_key",
    }
    assert "OPEN" in str(active_index.dialect_options["postgresql"]["where"])
    assert ExceptionType.INVENTORY_SHORTAGE.value == "INVENTORY_SHORTAGE"
    assert ExceptionStatus.OPEN.value == "OPEN"


def test_order_and_purchase_order_bounds_are_constrained() -> None:
    order_checks = {check.name for check in OrderItem.__table__.constraints if check.name}
    po_checks = {check.name for check in PurchaseOrderItem.__table__.constraints if check.name}

    assert "ck_order_items_fulfilled_quantity_lte_ordered_quantity" in order_checks
    assert "ck_purchase_order_items_received_quantity_lte_ordered_quantity" in po_checks


def test_source_identifiers_are_unique() -> None:
    product_constraints = {
        constraint.name: constraint for constraint in Product.__table__.constraints
    }
    warehouse_constraints = {
        constraint.name: constraint for constraint in Warehouse.__table__.constraints
    }

    product_source_constraint = product_constraints["uq_products_source_product_id"]
    warehouse_source_constraint = warehouse_constraints["uq_warehouses_source_warehouse_id"]
    assert isinstance(product_source_constraint, UniqueConstraint)
    assert isinstance(warehouse_source_constraint, UniqueConstraint)
    assert {column.name for column in product_source_constraint.columns} == {
        "dataset_version_id",
        "source_product_id",
    }
    assert {column.name for column in warehouse_source_constraint.columns} == {
        "dataset_version_id",
        "source_warehouse_id",
    }
