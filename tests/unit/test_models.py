from sqlalchemy import BigInteger, Computed, Numeric

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
    Warehouse,
)

EXPECTED_TABLES = {
    "products",
    "warehouses",
    "inventory",
    "inventory_movements",
    "orders",
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


def test_exception_identity_allows_same_issue_key_for_different_types() -> None:
    active_index = next(
        index
        for index in ExceptionRecord.__table__.indexes
        if index.name == "uq_exceptions_active_type_issue_key"
    )

    assert {column.name for column in active_index.columns} == {"exception_type", "issue_key"}
    assert "OPEN" in str(active_index.dialect_options["postgresql"]["where"])
    assert ExceptionType.INVENTORY_SHORTAGE.value == "INVENTORY_SHORTAGE"
    assert ExceptionStatus.OPEN.value == "OPEN"


def test_order_and_purchase_order_bounds_are_constrained() -> None:
    order_checks = {check.name for check in OrderItem.__table__.constraints if check.name}
    po_checks = {check.name for check in PurchaseOrderItem.__table__.constraints if check.name}

    assert "ck_order_items_fulfilled_quantity_lte_ordered_quantity" in order_checks
    assert "ck_purchase_order_items_received_quantity_lte_ordered_quantity" in po_checks


def test_source_identifiers_are_unique() -> None:
    assert Product.__table__.c.source_product_id.unique
    assert Warehouse.__table__.c.source_warehouse_id.unique
