from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from control_tower.config import Settings
from control_tower.db import Base
from control_tower.enums import ExceptionSeverity, InventoryMovementType, PurchaseOrderStatus
from control_tower.exceptions.rules import (
    detect_inventory_mismatch,
    detect_inventory_shortage,
    detect_shipment_delay,
    detect_sla_breach_risk,
    detect_stockout_risk,
    detect_supplier_delay,
)
from control_tower.exceptions.severity import severity_for_detection
from control_tower.models import (
    ExceptionRecord,
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


@event.listens_for(ExceptionRecord, "before_insert")
def _sqlite_exception_identity(mapper, connection, target) -> None:
    if target.id is None and connection.dialect.name == "sqlite":
        target.id = connection.scalar(select(func.max(ExceptionRecord.id))) or 0
        target.id += 1


AS_OF = datetime(2025, 1, 15, 12, tzinfo=timezone.utc)


def session_with_fixture() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    if engine.dialect.name == "sqlite":
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP INDEX uq_exceptions_active_type_issue_key")
            connection.exec_driver_sql(
                "CREATE UNIQUE INDEX uq_exceptions_active_type_issue_key "
                "ON exceptions (exception_type, issue_key) "
                "WHERE status IN ('OPEN', 'ACKNOWLEDGED', 'IN_PROGRESS')"
            )
    session = Session(engine)
    product = Product(
        id=1, source_product_id="P1", sku="SKU1", name="One", unit_price=Decimal("10")
    )
    second_product = Product(
        id=2, source_product_id="P2", sku="SKU2", name="Two", unit_price=Decimal("20")
    )
    warehouse = Warehouse(id=1, source_warehouse_id="W1", code="W1", name="Main", region="EU")
    supplier = Supplier(id=1, source_supplier_id="SUP1", code="SUP1", name="Supplier", region="EU")
    order = Order(
        id=1,
        source_order_id="O1",
        order_number="O1",
        status="OPEN",
        region="EU",
        warehouse_id=1,
        ordered_at=AS_OF - timedelta(days=2),
        promised_at=AS_OF - timedelta(hours=1),
        total_amount=Decimal("100"),
    )
    session.add_all([product, second_product, warehouse, supplier, order])
    session.flush()
    session.add_all(
        [
            OrderItem(
                id=1,
                source_order_item_id="OI1",
                order_id=1,
                product_id=1,
                line_number=1,
                ordered_quantity=Decimal("10"),
                fulfilled_quantity=Decimal("0"),
                unit_price=Decimal("10"),
            ),
            Inventory(
                id=1,
                product_id=1,
                warehouse_id=1,
                on_hand=Decimal("2"),
                reserved=Decimal("0"),
                observed_at=AS_OF - timedelta(hours=2),
            ),
            InventoryMovement(
                id=1,
                source_movement_id="M1",
                product_id=1,
                warehouse_id=1,
                movement_type=InventoryMovementType.RECEIPT,
                quantity=Decimal("8"),
                occurred_at=AS_OF - timedelta(days=1),
            ),
            PurchaseOrder(
                id=1,
                source_purchase_order_id="PO1",
                po_number="PO1",
                supplier_id=1,
                warehouse_id=1,
                status="OPEN",
                ordered_at=AS_OF - timedelta(days=5),
                expected_delivery_at=AS_OF - timedelta(days=1),
            ),
            PurchaseOrderItem(
                id=1,
                source_purchase_order_item_id="POI1",
                purchase_order_id=1,
                product_id=2,
                ordered_quantity=Decimal("5"),
                received_quantity=Decimal("0"),
                unit_cost=Decimal("4"),
            ),
            Shipment(
                id=1,
                source_shipment_id="S1",
                order_id=1,
                carrier="Carrier",
                tracking_id="T1",
                status="IN_TRANSIT",
                eta=AS_OF - timedelta(hours=1),
            ),
        ]
    )
    session.commit()
    return session


def test_each_rule_detects_its_triggering_fixture() -> None:
    session = session_with_fixture()
    settings = Settings(safety_stock=Decimal("20"), inventory_mismatch_tolerance=Decimal("1"))

    assert [d.entity_id for d in detect_sla_breach_risk(session, AS_OF, settings)] == ["O1"]
    assert [d.entity_id for d in detect_inventory_shortage(session, AS_OF, settings)] == ["P1:W1"]
    assert [d.entity_id for d in detect_stockout_risk(session, AS_OF, settings)] == ["P1:W1"]
    assert [d.entity_id for d in detect_inventory_mismatch(session, AS_OF, settings)] == ["P1:W1"]
    assert [d.entity_id for d in detect_supplier_delay(session, AS_OF, settings)] == ["PO1"]
    assert [d.entity_id for d in detect_shipment_delay(session, AS_OF, settings)] == ["S1"]


def test_sla_detects_overdue_and_inclusive_risk_window_but_not_outside() -> None:
    session = session_with_fixture()
    order = session.get(Order, 1)
    assert order is not None
    settings = Settings(sla_risk_window_hours=4)

    order.promised_at = AS_OF + timedelta(hours=4)
    session.flush()
    assert [d.entity_id for d in detect_sla_breach_risk(session, AS_OF, settings)] == ["O1"]

    order.promised_at = AS_OF + timedelta(hours=4, microseconds=1)
    session.flush()
    assert detect_sla_breach_risk(session, AS_OF, settings) == ()

    order.promised_at = AS_OF - timedelta(microseconds=1)
    session.flush()
    overdue = detect_sla_breach_risk(session, AS_OF, settings)
    assert overdue[0].overdue_hours == Decimal("0.0000")


def test_shortage_equality_and_stockout_safety_equality_do_not_trigger() -> None:
    session = session_with_fixture()
    inventory = session.get(Inventory, 1)
    assert inventory is not None
    inventory.on_hand = Decimal("10")
    settings = Settings(safety_stock=Decimal("0"), inventory_mismatch_tolerance=Decimal("100"))
    session.flush()

    assert detect_inventory_shortage(session, AS_OF, settings) == ()
    assert detect_stockout_risk(session, AS_OF, settings) == ()


def test_shortage_and_stockout_are_distinct_findings() -> None:
    session = session_with_fixture()
    order = session.get(Order, 1)
    inventory = session.get(Inventory, 1)
    assert order is not None and inventory is not None
    item = order.items[0]
    item.ordered_quantity = Decimal("10")
    inventory.on_hand = Decimal("10")
    session.flush()

    settings = Settings(safety_stock=Decimal("1"), inventory_mismatch_tolerance=Decimal("100"))
    assert detect_inventory_shortage(session, AS_OF, settings) == ()
    assert [d.entity_id for d in detect_stockout_risk(session, AS_OF, settings)] == ["P1:W1"]


def test_inventory_mismatch_tolerance_is_inclusive() -> None:
    session = session_with_fixture()
    settings = Settings(inventory_mismatch_tolerance=Decimal("6"))

    assert detect_inventory_mismatch(session, AS_OF, settings) == ()
    settings.inventory_mismatch_tolerance = Decimal("5.999")
    assert [d.entity_id for d in detect_inventory_mismatch(session, AS_OF, settings)] == ["P1:W1"]


def test_supplier_delay_excludes_full_receipt_and_cancelled_orders() -> None:
    session = session_with_fixture()
    purchase_order = session.get(PurchaseOrder, 1)
    item = session.get(PurchaseOrderItem, 1)
    assert purchase_order is not None and item is not None

    purchase_order.status = PurchaseOrderStatus.RECEIVED
    item.received_quantity = item.ordered_quantity
    session.flush()
    assert detect_supplier_delay(session, AS_OF) == ()

    purchase_order.status = PurchaseOrderStatus.CANCELLED
    item.received_quantity = Decimal("0")
    session.flush()
    assert detect_supplier_delay(session, AS_OF) == ()

    purchase_order.status = PurchaseOrderStatus.PARTIALLY_RECEIVED
    session.flush()
    assert [d.entity_id for d in detect_supplier_delay(session, AS_OF)] == ["PO1"]


def test_supplier_delay_requires_past_due_date_and_populates_overdue_severity_metric() -> None:
    session = session_with_fixture()
    purchase_order = session.get(PurchaseOrder, 1)
    assert purchase_order is not None

    purchase_order.expected_delivery_at = AS_OF
    session.flush()
    assert detect_supplier_delay(session, AS_OF) == ()

    purchase_order.expected_delivery_at = AS_OF - timedelta(hours=24)
    session.flush()
    detections = detect_supplier_delay(session, AS_OF)

    assert len(detections) == 1
    assert detections[0].overdue_hours == Decimal("24.0000")
    assert severity_for_detection(detections[0], Settings()) == ExceptionSeverity.HIGH


def test_shipment_at_eta_and_delivered_shipment_do_not_trigger() -> None:
    session = session_with_fixture()
    shipment = session.get(Shipment, 1)
    assert shipment is not None
    shipment.eta = AS_OF
    session.flush()
    assert detect_shipment_delay(session, AS_OF) == ()
    shipment.status = "DELIVERED"
    shipment.eta = AS_OF - timedelta(hours=1)
    session.flush()
    assert detect_shipment_delay(session, AS_OF) == ()
