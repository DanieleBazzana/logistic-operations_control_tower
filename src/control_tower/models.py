"""SQLAlchemy 2.x relational model for the operations control tower."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship
from sqlalchemy.sql.dml import Delete, Update

from control_tower.db import Base, utc_now
from control_tower.enums import (
    ExceptionSeverity,
    ExceptionStatus,
    ExceptionType,
    InventoryMovementType,
    OrderStatus,
    PurchaseOrderStatus,
    ShipmentStatus,
)

QUANTITY = Numeric(18, 3)
MONEY = Numeric(14, 2)


def domain_enum(enum_class: type, name: str) -> SAEnum:
    return SAEnum(enum_class, name=name, native_enum=True, validate_strings=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )


class Product(TimestampMixin, Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_product_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    sku: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    unit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    inventory: Mapped[list["Inventory"]] = relationship(back_populates="product")
    movements: Mapped[list["InventoryMovement"]] = relationship(back_populates="product")
    order_items: Mapped[list["OrderItem"]] = relationship(back_populates="product")
    purchase_order_items: Mapped[list["PurchaseOrderItem"]] = relationship(back_populates="product")

    __table_args__ = (
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        Index("ix_products_sku", "sku"),
    )


class Warehouse(TimestampMixin, Base):
    __tablename__ = "warehouses"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_warehouse_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[str] = mapped_column(String(100), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'UTC'"))

    inventory: Mapped[list["Inventory"]] = relationship(back_populates="warehouse")
    movements: Mapped[list["InventoryMovement"]] = relationship(back_populates="warehouse")
    orders: Mapped[list["Order"]] = relationship(back_populates="warehouse")
    purchase_orders: Mapped[list["PurchaseOrder"]] = relationship(back_populates="warehouse")
    exceptions: Mapped[list["ExceptionRecord"]] = relationship(back_populates="warehouse")


class Inventory(TimestampMixin, Base):
    __tablename__ = "inventory"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    on_hand: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    reserved: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    available: Mapped[Decimal] = mapped_column(
        QUANTITY, Computed("on_hand - reserved", persisted=True), nullable=False
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    product: Mapped[Product] = relationship(back_populates="inventory")
    warehouse: Mapped[Warehouse] = relationship(back_populates="inventory")

    __table_args__ = (
        UniqueConstraint("product_id", "warehouse_id", name="uq_inventory_product_warehouse"),
        CheckConstraint("on_hand >= 0", name="on_hand_non_negative"),
        CheckConstraint("reserved >= 0", name="reserved_non_negative"),
        Index("ix_inventory_warehouse_product", "warehouse_id", "product_id"),
    )


class InventoryMovement(TimestampMixin, Base):
    __tablename__ = "inventory_movements"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_movement_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    movement_type: Mapped[InventoryMovementType] = mapped_column(
        domain_enum(InventoryMovementType, "inventory_movement_type"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reference_type: Mapped[str | None] = mapped_column(String(50))
    reference_id: Mapped[str | None] = mapped_column(String(100))

    product: Mapped[Product] = relationship(back_populates="movements")
    warehouse: Mapped[Warehouse] = relationship(back_populates="movements")

    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        Index(
            "ix_inventory_movements_product_warehouse_occurred",
            "product_id",
            "warehouse_id",
            "occurred_at",
        ),
    )


class Order(TimestampMixin, Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_order_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    order_number: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    status: Mapped[OrderStatus] = mapped_column(
        domain_enum(OrderStatus, "order_status"), nullable=False
    )
    region: Mapped[str] = mapped_column(String(100), nullable=False)
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    ordered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    promised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default=text("'USD'"))

    warehouse: Mapped[Warehouse] = relationship(back_populates="orders")
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    shipments: Mapped[list["Shipment"]] = relationship(back_populates="order")

    __table_args__ = (
        CheckConstraint("total_amount >= 0", name="total_amount_non_negative"),
        CheckConstraint(
            "fulfilled_at IS NULL OR fulfilled_at >= ordered_at",
            name="fulfilled_after_ordered",
        ),
        Index("ix_orders_status_promised_at", "status", "promised_at"),
        Index("ix_orders_warehouse_status", "warehouse_id", "status"),
    )


class OrderItem(TimestampMixin, Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_order_item_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    ordered_quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    fulfilled_quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")
    product: Mapped[Product] = relationship(back_populates="order_items")

    __table_args__ = (
        UniqueConstraint("order_id", "line_number", name="uq_order_items_order_line"),
        CheckConstraint("ordered_quantity > 0", name="ordered_quantity_positive"),
        CheckConstraint("fulfilled_quantity >= 0", name="fulfilled_quantity_non_negative"),
        CheckConstraint(
            "fulfilled_quantity <= ordered_quantity",
            name="fulfilled_quantity_lte_ordered_quantity",
        ),
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
    )


class Supplier(TimestampMixin, Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_supplier_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[str] = mapped_column(String(100), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    purchase_orders: Mapped[list["PurchaseOrder"]] = relationship(back_populates="supplier")


class PurchaseOrder(TimestampMixin, Base):
    __tablename__ = "purchase_orders"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_purchase_order_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    po_number: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False
    )
    warehouse_id: Mapped[int] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[PurchaseOrderStatus] = mapped_column(
        domain_enum(PurchaseOrderStatus, "purchase_order_status"), nullable=False
    )
    ordered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expected_delivery_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    supplier: Mapped[Supplier] = relationship(back_populates="purchase_orders")
    warehouse: Mapped[Warehouse] = relationship(back_populates="purchase_orders")
    items: Mapped[list["PurchaseOrderItem"]] = relationship(
        back_populates="purchase_order", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "received_at IS NULL OR received_at >= ordered_at",
            name="received_after_ordered",
        ),
        Index("ix_purchase_orders_status_expected_delivery", "status", "expected_delivery_at"),
    )


class PurchaseOrderItem(TimestampMixin, Base):
    __tablename__ = "purchase_order_items"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_purchase_order_item_id: Mapped[str] = mapped_column(
        String(120), nullable=False, unique=True
    )
    purchase_order_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    ordered_quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    received_quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    purchase_order: Mapped[PurchaseOrder] = relationship(back_populates="items")
    product: Mapped[Product] = relationship(back_populates="purchase_order_items")

    __table_args__ = (
        CheckConstraint("ordered_quantity > 0", name="ordered_quantity_positive"),
        CheckConstraint("received_quantity >= 0", name="received_quantity_non_negative"),
        CheckConstraint(
            "received_quantity <= ordered_quantity",
            name="received_quantity_lte_ordered_quantity",
        ),
        CheckConstraint("unit_cost >= 0", name="unit_cost_non_negative"),
    )


class Shipment(TimestampMixin, Base):
    __tablename__ = "shipments"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_shipment_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    carrier: Mapped[str] = mapped_column(String(100), nullable=False)
    tracking_id: Mapped[str] = mapped_column(String(150), nullable=False, unique=True)
    status: Mapped[ShipmentStatus] = mapped_column(
        domain_enum(ShipmentStatus, "shipment_status"), nullable=False
    )
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    eta: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    order: Mapped[Order] = relationship(back_populates="shipments")

    __table_args__ = (
        CheckConstraint(
            "delivered_at IS NULL OR shipped_at IS NULL OR delivered_at >= shipped_at",
            name="delivered_after_shipped",
        ),
        Index("ix_shipments_status_eta", "status", "eta"),
    )


class ExceptionRecord(TimestampMixin, Base):
    __tablename__ = "exceptions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    deduplication_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    exception_type: Mapped[ExceptionType] = mapped_column(
        domain_enum(ExceptionType, "exception_type"), nullable=False
    )
    issue_key: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[ExceptionSeverity] = mapped_column(
        domain_enum(ExceptionSeverity, "exception_severity"), nullable=False
    )
    status: Mapped[ExceptionStatus] = mapped_column(
        domain_enum(ExceptionStatus, "exception_status"), nullable=False
    )
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expected_resolution: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    business_impact: Mapped[str] = mapped_column(Text, nullable=False)
    revenue_at_risk: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, server_default=text("0")
    )
    orders_affected: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    root_cause: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_action: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouses.id", ondelete="SET NULL")
    )
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    warehouse: Mapped[Warehouse | None] = relationship(back_populates="exceptions")
    product: Mapped[Product | None] = relationship()
    history: Mapped[list["ExceptionHistory"]] = relationship(
        back_populates="exception",
        order_by="ExceptionHistory.changed_at",
    )

    __table_args__ = (
        CheckConstraint("revenue_at_risk >= 0", name="revenue_at_risk_non_negative"),
        CheckConstraint("orders_affected >= 0", name="orders_affected_non_negative"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_0_1"),
        Index(
            "uq_exceptions_active_type_issue_key",
            "exception_type",
            "issue_key",
            unique=True,
            postgresql_where=text("status IN ('OPEN', 'ACKNOWLEDGED', 'IN_PROGRESS')"),
        ),
        Index("ix_exceptions_status_detected_at", "status", "detected_at"),
        Index("ix_exceptions_warehouse_status", "warehouse_id", "status"),
    )


class ExceptionHistory(Base):
    __tablename__ = "exception_history"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    exception_id: Mapped[int] = mapped_column(
        ForeignKey("exceptions.id", ondelete="RESTRICT"), nullable=False
    )
    from_status: Mapped[ExceptionStatus | None] = mapped_column(
        domain_enum(ExceptionStatus, "exception_status"), nullable=True
    )
    to_status: Mapped[ExceptionStatus] = mapped_column(
        domain_enum(ExceptionStatus, "exception_status"), nullable=False
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    transition_reason: Mapped[str | None] = mapped_column(Text)

    exception: Mapped[ExceptionRecord] = relationship(back_populates="history")

    __table_args__ = (
        Index("ix_exception_history_exception_changed", "exception_id", "changed_at"),
    )


@event.listens_for(ExceptionHistory, "before_update")
@event.listens_for(ExceptionHistory, "before_delete")
def _prevent_exception_history_mutation(mapper, connection, target) -> None:
    """Reject ORM updates/deletes before they can violate append-only history."""

    raise ValueError("exception_history is append-only")


@event.listens_for(Session, "do_orm_execute")
def _prevent_exception_history_bulk_mutation(orm_execute_state) -> None:
    """Reject ORM UPDATE/DELETE statements against append-only history."""

    if not (orm_execute_state.is_update or orm_execute_state.is_delete):
        return
    target = getattr(orm_execute_state.statement, "table", None)
    if target is ExceptionHistory.__table__ or (
        getattr(target, "name", None) == ExceptionHistory.__tablename__
    ):
        raise ValueError("exception_history is append-only")


@event.listens_for(Engine, "before_execute")
def _prevent_exception_history_legacy_bulk_mutation(
    connection, clauseelement, multiparams, params, execution_options
) -> None:
    """Reject legacy ``Session.bulk_update_mappings`` history mutations.

    Legacy bulk methods execute Core DML directly and therefore bypass
    ``Session.do_orm_execute``.  The engine boundary closes that gap for all
    SQLAlchemy UPDATE/DELETE statements; direct SQL remains protected by the
    PostgreSQL append-only trigger.
    """

    if not isinstance(clauseelement, (Update, Delete)):
        return
    target = getattr(clauseelement, "table", None)
    if target is ExceptionHistory.__table__ or (
        getattr(target, "name", None) == ExceptionHistory.__tablename__
    ):
        raise ValueError("exception_history is append-only")


# Useful descriptive aliases for service/API code without changing the table name.
SupplyChainException = ExceptionRecord
Exception = ExceptionRecord

__all__ = [
    "Exception",
    "ExceptionHistory",
    "ExceptionRecord",
    "Inventory",
    "InventoryMovement",
    "Order",
    "OrderItem",
    "Product",
    "PurchaseOrder",
    "PurchaseOrderItem",
    "Shipment",
    "Supplier",
    "SupplyChainException",
    "Warehouse",
]
