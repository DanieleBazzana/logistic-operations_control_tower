"""SQLAlchemy 2.x relational model for the operations control tower."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
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
    select,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy import (
    inspect as orm_inspect,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship
from sqlalchemy.sql.dml import Delete, Update

from control_tower.db import Base, utc_now
from control_tower.enums import (
    ExceptionSeverity,
    ExceptionStatus,
    ExceptionType,
    InventoryMovementType,
    OrderObservationStatus,
    OrderStatus,
    PurchaseOrderStatus,
    ShipmentStatus,
)

QUANTITY = Numeric(18, 3)
MONEY = Numeric(14, 2)
OBSERVATION_JSON = JSONB().with_variant(JSON(), "sqlite")


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


class DatasetScopedMixin:
    """Additive dataset boundary shared by every operational row."""

    dataset_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="RESTRICT"), nullable=True, index=True
    )


DATASET_SCOPE_EXPLICIT_KEY = "dataset_scope_requires_explicit_id"


@contextmanager
def explicit_dataset_scope(session: Session) -> Iterator[None]:
    """Require dataset IDs for candidate/replacement ORM writes in ``session``."""

    previous = session.info.get(DATASET_SCOPE_EXPLICIT_KEY)
    session.info[DATASET_SCOPE_EXPLICIT_KEY] = True
    try:
        yield
    finally:
        if previous is None:
            session.info.pop(DATASET_SCOPE_EXPLICIT_KEY, None)
        else:
            session.info[DATASET_SCOPE_EXPLICIT_KEY] = previous


class DatasetVersion(TimestampMixin, Base):
    """Immutable identity and state for one complete operational dataset."""

    __tablename__ = "dataset_versions"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), Identity(), primary_key=True
    )
    dataset_key: Mapped[str] = mapped_column(String(32), nullable=False)
    identity_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    manifest_identity: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generator_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('STAGED', 'READY', 'ACTIVE', 'RETIRED')",
            name="ck_dataset_versions_status",
        ),
        UniqueConstraint(
            "dataset_key", "manifest_identity", "seed", "as_of", "generator_revision",
            name="uq_dataset_versions_logical_identity",
        ),
        Index("ix_dataset_versions_status", "status"),
    )


class DatasetActivation(Base):
    """Singleton authoritative read pointer; changing it is the activation."""

    __tablename__ = "dataset_activation"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), Identity(), primary_key=True
    )
    active_dataset_version_id: Mapped[int] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="RESTRICT"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )
    active_dataset: Mapped[DatasetVersion] = relationship()


class Product(DatasetScopedMixin, TimestampMixin, Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_product_id: Mapped[str] = mapped_column(String(100), nullable=False)
    sku: Mapped[str] = mapped_column(String(100), nullable=False)
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
        UniqueConstraint(
            "dataset_version_id", "source_product_id", name="uq_products_source_product_id"
        ),
        UniqueConstraint("dataset_version_id", "sku", name="uq_products_sku"),
    )


class Warehouse(DatasetScopedMixin, TimestampMixin, Base):
    __tablename__ = "warehouses"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_warehouse_id: Mapped[str] = mapped_column(String(100), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[str] = mapped_column(String(100), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'UTC'"))
    __table_args__ = (
        UniqueConstraint(
            "dataset_version_id", "source_warehouse_id", name="uq_warehouses_source_warehouse_id"
        ),
        UniqueConstraint("dataset_version_id", "code", name="uq_warehouses_code"),
    )

    inventory: Mapped[list["Inventory"]] = relationship(back_populates="warehouse")
    movements: Mapped[list["InventoryMovement"]] = relationship(back_populates="warehouse")
    orders: Mapped[list["Order"]] = relationship(back_populates="warehouse")
    purchase_orders: Mapped[list["PurchaseOrder"]] = relationship(back_populates="warehouse")
    exceptions: Mapped[list["ExceptionRecord"]] = relationship(back_populates="warehouse")


class Inventory(DatasetScopedMixin, TimestampMixin, Base):
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
        UniqueConstraint(
            "dataset_version_id", "product_id", "warehouse_id",
            name="uq_inventory_product_warehouse",
        ),
        CheckConstraint("on_hand >= 0", name="on_hand_non_negative"),
        CheckConstraint("reserved >= 0", name="reserved_non_negative"),
        Index("ix_inventory_warehouse_product", "warehouse_id", "product_id"),
    )


class InventoryMovement(DatasetScopedMixin, TimestampMixin, Base):
    __tablename__ = "inventory_movements"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_movement_id: Mapped[str] = mapped_column(String(100), nullable=False)
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
        UniqueConstraint(
            "dataset_version_id",
            "source_movement_id",
            name="uq_inventory_movements_source_movement_id",
        ),
    )


class SourceOrderIdentity(DatasetScopedMixin, TimestampMixin, Base):
    """Authoritative identity of an order in one source namespace."""

    __tablename__ = "source_order_identities"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), Identity(), primary_key=True
    )
    source_namespace: Mapped[str] = mapped_column(String(100), nullable=False)
    source_order_id: Mapped[str] = mapped_column(String(100), nullable=False)

    observations: Mapped[list["OrderObservation"]] = relationship(back_populates="source_identity")
    orders: Mapped[list["Order"]] = relationship(back_populates="source_identity")

    __table_args__ = (
        UniqueConstraint(
            "dataset_version_id", "source_namespace", "source_order_id",
            name="uq_source_order_identity_namespace_order"
        ),
    )


class Order(DatasetScopedMixin, TimestampMixin, Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), Identity(), primary_key=True
    )
    source_namespace: Mapped[str | None] = mapped_column(String(100))
    source_order_id: Mapped[str] = mapped_column(String(100), nullable=False)
    source_order_identity_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_order_identities.id", ondelete="RESTRICT")
    )
    projected_source_version: Mapped[str | None] = mapped_column(String(100))
    source_warehouse_id: Mapped[str | None] = mapped_column(String(100))
    current_observation_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_observations.id", ondelete="RESTRICT")
    )
    order_number: Mapped[str] = mapped_column(String(100), nullable=False)
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
    source_identity: Mapped[SourceOrderIdentity | None] = relationship(back_populates="orders")
    current_observation: Mapped["OrderObservation | None"] = relationship(
        foreign_keys=[current_observation_id], post_update=True
    )
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    shipments: Mapped[list["Shipment"]] = relationship(back_populates="order")

    __table_args__ = (
        UniqueConstraint(
            "dataset_version_id", "source_namespace", "source_order_id",
            name="uq_orders_source_namespace_order"
        ),
        UniqueConstraint(
            "dataset_version_id", "source_namespace", "order_number",
            name="uq_orders_source_namespace_number"
        ),
        CheckConstraint("total_amount >= 0", name="total_amount_non_negative"),
        CheckConstraint(
            "fulfilled_at IS NULL OR fulfilled_at >= ordered_at",
            name="fulfilled_after_ordered",
        ),
        Index("ix_orders_status_promised_at", "status", "promised_at"),
        Index("ix_orders_warehouse_status", "warehouse_id", "status"),
    )


class OrderObservation(DatasetScopedMixin, TimestampMixin, Base):
    """Internal source observation kept separate from the strict Order projection."""

    __tablename__ = "order_observations"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), Identity(), primary_key=True
    )
    source_order_identity_id: Mapped[int] = mapped_column(
        ForeignKey("source_order_identities.id", ondelete="RESTRICT"), nullable=False
    )
    source_namespace: Mapped[str] = mapped_column(String(100), nullable=False)
    source_order_id: Mapped[str] = mapped_column(String(100), nullable=False)
    source_version: Mapped[str | None] = mapped_column(String(100))
    source_row_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    replay_identity: Mapped[list[str | None]] = mapped_column(OBSERVATION_JSON, nullable=False)
    replay_identity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_facts: Mapped[dict[str, object]] = mapped_column(OBSERVATION_JSON, nullable=False)
    order_number: Mapped[str | None] = mapped_column(String(100))
    order_status: Mapped[OrderStatus | None] = mapped_column(
        domain_enum(OrderStatus, "order_status"), nullable=True
    )
    region: Mapped[str | None] = mapped_column(String(100))
    source_warehouse_id: Mapped[str | None] = mapped_column(String(100))
    warehouse_id: Mapped[int | None] = mapped_column(
        ForeignKey("warehouses.id", ondelete="RESTRICT")
    )
    ordered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    promised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    currency: Mapped[str | None] = mapped_column(String(3))
    status: Mapped[OrderObservationStatus] = mapped_column(
        domain_enum(OrderObservationStatus, "order_observation_status"), nullable=False
    )
    source_status: Mapped[OrderObservationStatus] = mapped_column(
        domain_enum(OrderObservationStatus, "order_observation_status"), nullable=False
    )
    capabilities: Mapped[dict[str, object]] = mapped_column(
        OBSERVATION_JSON, nullable=False, default=dict
    )
    non_promotion_reasons: Mapped[list[str]] = mapped_column(
        OBSERVATION_JSON, nullable=False, default=list
    )
    promoted_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT")
    )
    superseded_by_observation_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_observations.id", ondelete="RESTRICT")
    )
    source_identity: Mapped[SourceOrderIdentity] = relationship(back_populates="observations")
    warehouse: Mapped[Warehouse | None] = relationship()
    superseded_by: Mapped["OrderObservation | None"] = relationship(
        remote_side="OrderObservation.id", foreign_keys=[superseded_by_observation_id]
    )
    receipts: Mapped[list["OrderObservationReceipt"]] = relationship(back_populates="observation")

    __table_args__ = (
        CheckConstraint(
            "total_amount IS NULL OR total_amount >= 0",
            name="order_observations_total_amount_non_negative",
        ),
        CheckConstraint(
            "fulfilled_at IS NULL OR ordered_at IS NULL OR fulfilled_at >= ordered_at",
            name="order_observations_fulfilled_after_ordered",
        ),
        Index("ix_order_observations_status", "status"),
        Index("ix_order_observations_source_order", "source_namespace", "source_order_id"),
        Index("ix_order_observations_source_version", "source_order_identity_id", "source_version"),
        UniqueConstraint(
            "dataset_version_id", "replay_identity_digest",
            name="uq_order_observations_replay_identity_digest",
        ),
    )

    @property
    def observation_status(self) -> OrderObservationStatus:
        """Compatibility/readability alias for the lifecycle status."""

        return self.status

    @observation_status.setter
    def observation_status(self, value: OrderObservationStatus) -> None:
        self.status = value


_OBSERVATION_IMMUTABLE_FIELDS = (
    "source_order_identity_id",
    "source_namespace",
    "source_order_id",
    "source_version",
    "source_row_hash",
    "replay_identity",
    "replay_identity_digest",
    "source_facts",
    "order_number",
    "order_status",
    "region",
    "source_warehouse_id",
    "ordered_at",
    "promised_at",
    "fulfilled_at",
    "total_amount",
    "currency",
)


@event.listens_for(OrderObservation, "before_update")
def _prevent_order_observation_evidence_mutation(mapper, connection, target) -> None:
    state = orm_inspect(target)
    if any(state.attrs[field].history.has_changes() for field in _OBSERVATION_IMMUTABLE_FIELDS):
        raise ValueError("order_observation evidence is immutable")


@event.listens_for(OrderObservation, "before_delete")
def _prevent_order_observation_delete(mapper, connection, target) -> None:
    raise ValueError("order_observation evidence is append-only")


@event.listens_for(OrderObservation.__table__, "after_create")
def _create_sqlite_observation_guards(target, connection, **kwargs) -> None:
    if connection.dialect.name != "sqlite":
        return
    connection.exec_driver_sql(
        """
        CREATE TRIGGER order_observations_immutable_update
        BEFORE UPDATE ON order_observations
        WHEN OLD.source_order_identity_id IS NOT NEW.source_order_identity_id
          OR OLD.source_namespace IS NOT NEW.source_namespace
          OR OLD.source_order_id IS NOT NEW.source_order_id
          OR OLD.source_version IS NOT NEW.source_version
          OR OLD.source_row_hash IS NOT NEW.source_row_hash
          OR OLD.replay_identity IS NOT NEW.replay_identity
          OR OLD.replay_identity_digest IS NOT NEW.replay_identity_digest
          OR OLD.source_facts IS NOT NEW.source_facts
          OR OLD.order_number IS NOT NEW.order_number
          OR OLD.order_status IS NOT NEW.order_status
          OR OLD.region IS NOT NEW.region
          OR OLD.source_warehouse_id IS NOT NEW.source_warehouse_id
          OR OLD.ordered_at IS NOT NEW.ordered_at
          OR OLD.promised_at IS NOT NEW.promised_at
          OR OLD.fulfilled_at IS NOT NEW.fulfilled_at
          OR OLD.total_amount IS NOT NEW.total_amount
          OR OLD.currency IS NOT NEW.currency
        BEGIN
          SELECT RAISE(ABORT, 'order_observation evidence is immutable');
        END
        """
    )
    connection.exec_driver_sql(
        """
        CREATE TRIGGER order_observations_append_only_delete
        BEFORE DELETE ON order_observations
        BEGIN
          SELECT RAISE(ABORT, 'order_observation evidence is append-only');
        END
        """
    )


class OrderObservationReceipt(DatasetScopedMixin, TimestampMixin, Base):
    """Provenance receipt: one batch may receive one immutable observation."""

    __tablename__ = "order_observation_receipts"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), Identity(), primary_key=True
    )
    observation_id: Mapped[int] = mapped_column(
        ForeignKey("order_observations.id", ondelete="RESTRICT"), nullable=False
    )
    batch_id: Mapped[str] = mapped_column(String(128), nullable=False)

    observation: Mapped[OrderObservation] = relationship(back_populates="receipts")

    __table_args__ = (
        UniqueConstraint(
            "dataset_version_id", "observation_id", "batch_id",
            name="uq_order_observation_receipt_observation_batch",
        ),
        Index("ix_order_observation_receipts_batch", "batch_id"),
    )


class OrderItem(DatasetScopedMixin, TimestampMixin, Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_order_item_id: Mapped[str] = mapped_column(String(120), nullable=False)
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
        UniqueConstraint(
            "dataset_version_id", "source_order_item_id", name="uq_order_items_source_order_item_id"
        ),
        CheckConstraint("ordered_quantity > 0", name="ordered_quantity_positive"),
        CheckConstraint("fulfilled_quantity >= 0", name="fulfilled_quantity_non_negative"),
        CheckConstraint(
            "fulfilled_quantity <= ordered_quantity",
            name="fulfilled_quantity_lte_ordered_quantity",
        ),
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
    )


class Supplier(DatasetScopedMixin, TimestampMixin, Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_supplier_id: Mapped[str] = mapped_column(String(100), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[str] = mapped_column(String(100), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    __table_args__ = (
        UniqueConstraint(
            "dataset_version_id", "source_supplier_id", name="uq_suppliers_source_supplier_id"
        ),
        UniqueConstraint("dataset_version_id", "code", name="uq_suppliers_code"),
    )

    purchase_orders: Mapped[list["PurchaseOrder"]] = relationship(back_populates="supplier")


class PurchaseOrder(DatasetScopedMixin, TimestampMixin, Base):
    __tablename__ = "purchase_orders"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_purchase_order_id: Mapped[str] = mapped_column(String(100), nullable=False)
    po_number: Mapped[str] = mapped_column(String(100), nullable=False)
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

    @property
    def remaining_quantity(self) -> Decimal:
        """Return the quantity still outstanding across all PO lines."""

        return sum(
            (item.ordered_quantity - item.received_quantity for item in self.items),
            Decimal("0"),
        )

    __table_args__ = (
        CheckConstraint(
            "received_at IS NULL OR received_at >= ordered_at",
            name="received_after_ordered",
        ),
        Index("ix_purchase_orders_status_expected_delivery", "status", "expected_delivery_at"),
        UniqueConstraint(
            "dataset_version_id",
            "source_purchase_order_id",
            name="uq_purchase_orders_source_purchase_order_id",
        ),
        UniqueConstraint(
            "dataset_version_id", "po_number", name="uq_purchase_orders_po_number"
        ),
    )


class PurchaseOrderItem(DatasetScopedMixin, TimestampMixin, Base):
    __tablename__ = "purchase_order_items"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_purchase_order_item_id: Mapped[str] = mapped_column(String(120), nullable=False)
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
        UniqueConstraint(
            "dataset_version_id", "source_purchase_order_item_id",
            name="uq_purchase_order_items_source_purchase_order_item_id",
        ),
    )


class Shipment(DatasetScopedMixin, TimestampMixin, Base):
    __tablename__ = "shipments"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_shipment_id: Mapped[str] = mapped_column(String(100), nullable=False)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    carrier: Mapped[str] = mapped_column(String(100), nullable=False)
    tracking_id: Mapped[str] = mapped_column(String(150), nullable=False)
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
        UniqueConstraint(
            "dataset_version_id", "source_shipment_id", name="uq_shipments_source_shipment_id"
        ),
        UniqueConstraint("dataset_version_id", "tracking_id", name="uq_shipments_tracking_id"),
    )


class ExceptionRecord(DatasetScopedMixin, TimestampMixin, Base):
    __tablename__ = "exceptions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    deduplication_key: Mapped[str] = mapped_column(String(255), nullable=False)
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
        order_by=lambda: (ExceptionHistory.changed_at, ExceptionHistory.id),
    )

    __table_args__ = (
        CheckConstraint("revenue_at_risk >= 0", name="revenue_at_risk_non_negative"),
        CheckConstraint("orders_affected >= 0", name="orders_affected_non_negative"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_0_1"),
        Index(
            "uq_exceptions_active_type_issue_key",
            "dataset_version_id",
            "exception_type",
            "issue_key",
            unique=True,
            postgresql_where=text("status IN ('OPEN', 'ACKNOWLEDGED', 'IN_PROGRESS')"),
        ),
        Index("ix_exceptions_status_detected_at", "status", "detected_at"),
        Index("ix_exceptions_warehouse_status", "warehouse_id", "status"),
        UniqueConstraint(
            "dataset_version_id", "deduplication_key", name="uq_exceptions_deduplication_key"
        ),
    )


class ExceptionHistory(DatasetScopedMixin, Base):
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
        if orm_execute_state.execution_options.get("_m076_metadata_backfill"):
            return
        raise ValueError("exception_history is append-only")


def _active_dataset_id_for_flush(session: Session) -> int | None:
    """Read the active pointer without triggering a nested ORM flush."""

    pending_activation = next(
        (
            item
            for item in (*session.new, *session.dirty)
            if isinstance(item, DatasetActivation)
            and item.id == 1
            and item.active_dataset_version_id is not None
        ),
        None,
    )
    if pending_activation is not None:
        return pending_activation.active_dataset_version_id
    return session.connection().execute(
        select(DatasetActivation.active_dataset_version_id).where(DatasetActivation.id == 1)
    ).scalar_one_or_none()


@event.listens_for(Session, "before_flush")
def _inherit_active_dataset_for_legacy_writes(session: Session, flush_context, instances) -> None:
    """Scope legacy M07.6 ORM inserts before migrated NOT NULL columns are checked."""

    pending_scoped = [item for item in session.new if isinstance(item, DatasetScopedMixin)]
    if not pending_scoped:
        return
    active_id = _active_dataset_id_for_flush(session)
    requires_explicit_id = session.info.get(DATASET_SCOPE_EXPLICIT_KEY, False)
    for item in pending_scoped:
        if item.dataset_version_id is not None:
            continue
        if requires_explicit_id:
            raise ValueError("dataset_version_id is required for candidate/replacement writes")
        if active_id is not None:
            item.dataset_version_id = active_id


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
        if execution_options.get("_m076_metadata_backfill"):
            return
        raise ValueError("exception_history is append-only")


# Useful descriptive aliases for service/API code without changing the table name.
SupplyChainException = ExceptionRecord
Exception = ExceptionRecord

__all__ = [
    "DATASET_SCOPE_EXPLICIT_KEY",
    "DatasetActivation",
    "DatasetVersion",
    "Exception",
    "ExceptionHistory",
    "ExceptionRecord",
    "Inventory",
    "InventoryMovement",
    "Order",
    "OrderItem",
    "OrderObservation",
    "OrderObservationReceipt",
    "Product",
    "PurchaseOrder",
    "PurchaseOrderItem",
    "Shipment",
    "Supplier",
    "SourceOrderIdentity",
    "SupplyChainException",
    "Warehouse",
    "explicit_dataset_scope",
]
