"""Add dataset-version boundaries and the singleton activation pointer.

Revision ID: 20260917_01
Revises: 20260911_01
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260917_01"
down_revision: str | None = "20260911_01"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_SCOPED_TABLES = (
    "products", "warehouses", "inventory", "inventory_movements",
    "source_order_identities", "orders", "order_observations",
    "order_observation_receipts", "order_items", "suppliers", "purchase_orders",
    "purchase_order_items", "shipments", "exceptions", "exception_history",
)
# Keep names deterministic and below PostgreSQL's 63-byte identifier limit.  The
# same map is deliberately used by upgrade and downgrade.
_FK_NAMES = {table: f"fk_dv_{table}" for table in _SCOPED_TABLES}
_LEGACY_IDENTITY = "1463efc2d5062bc493ff278f9e6ad43d207f065ec33cabadc2287911752ebb7e"


def upgrade() -> None:
    op.create_table(
        "dataset_versions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("dataset_key", sa.String(32), nullable=False),
        sa.Column("identity_hash", sa.String(64), nullable=False),
        sa.Column("manifest_identity", sa.String(128), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generator_revision", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('STAGED', 'READY', 'ACTIVE', 'RETIRED')", name="ck_dataset_versions_status"),
        sa.PrimaryKeyConstraint("id", name="pk_dataset_versions"),
        sa.UniqueConstraint("identity_hash", name="uq_dataset_versions_identity_hash"),
        sa.UniqueConstraint(
            "dataset_key", "manifest_identity", "seed", "as_of", "generator_revision",
            name="uq_dataset_versions_logical_identity",
        ),
    )
    op.create_index("ix_dataset_versions_status", "dataset_versions", ["status"])
    op.create_table(
        "dataset_activation",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("active_dataset_version_id", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["active_dataset_version_id"], ["dataset_versions.id"],
            name="fk_dataset_activation_active_dataset_version_id", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_dataset_activation"),
        sa.CheckConstraint("id = 1", name="ck_dataset_activation_singleton"),
    )

    for table in _SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN dataset_version_id BIGINT")
        op.create_foreign_key(
            _FK_NAMES[table], table, "dataset_versions", ["dataset_version_id"], ["id"],
            ondelete="RESTRICT",
        )
        op.create_index(f"ix_{table}_dataset_version_id", table, ["dataset_version_id"])

    op.execute(
        f"""
        INSERT INTO dataset_versions
            (dataset_key, identity_hash, manifest_identity, content_hash, seed, as_of,
             generator_revision, status)
        VALUES
            ('M07.6', '{_LEGACY_IDENTITY}', 'm07.6-legacy-backfill',
             '0fa4fe517eb2a40d4031faff8a02b9e169121fec16137c07f284e8b54dc46ef8',
             20250301, TIMESTAMP WITH TIME ZONE '2025-03-07 18:00:00+00', 'm07.6', 'ACTIVE')
        ON CONFLICT (identity_hash) DO NOTHING
        """
    )
    for table in _SCOPED_TABLES:
        if table == "exception_history":
            continue
        op.execute(
            f"UPDATE {table} SET dataset_version_id = "
            f"(SELECT id FROM dataset_versions WHERE identity_hash = '{_LEGACY_IDENTITY}') "
            "WHERE dataset_version_id IS NULL"
        )

    # The M07.6 exception-history rows predate this metadata.  Bypass the
    # append-only trigger only for this metadata-only UPDATE, in this
    # transaction, and restore it before normal traffic can resume.
    op.execute("ALTER TABLE exception_history DISABLE TRIGGER trg_exception_history_append_only")
    op.execute(
        "ALTER TABLE exception_history DISABLE TRIGGER trg_exception_history_append_only_truncate"
    )
    op.execute(
        f"UPDATE exception_history SET dataset_version_id = "
        f"(SELECT id FROM dataset_versions WHERE identity_hash = '{_LEGACY_IDENTITY}') "
        "WHERE dataset_version_id IS NULL"
    )
    op.execute("ALTER TABLE exception_history ENABLE TRIGGER trg_exception_history_append_only")
    op.execute(
        "ALTER TABLE exception_history ENABLE TRIGGER trg_exception_history_append_only_truncate"
    )

    op.execute("ALTER TABLE exception_history ALTER COLUMN dataset_version_id SET NOT NULL")
    for table in _SCOPED_TABLES:
        if table != "exception_history":
            op.execute(f"ALTER TABLE {table} ALTER COLUMN dataset_version_id SET NOT NULL")

    op.execute(
        """
        INSERT INTO dataset_activation (id, active_dataset_version_id)
        SELECT 1, id FROM dataset_versions
        WHERE identity_hash = '1463efc2d5062bc493ff278f9e6ad43d207f065ec33cabadc2287911752ebb7e'
        ON CONFLICT (id) DO NOTHING
        """
    )

    # Source identifiers are unique inside a dataset, not across replacements.
    for table, name in (
        ("products", "uq_products_source_product_id"), ("products", "uq_products_sku"),
        ("suppliers", "uq_suppliers_source_supplier_id"), ("suppliers", "uq_suppliers_code"),
        ("warehouses", "uq_warehouses_source_warehouse_id"), ("warehouses", "uq_warehouses_code"),
        ("inventory", "uq_inventory_product_warehouse"),
        ("inventory_movements", "uq_inventory_movements_source_movement_id"),
        ("orders", "uq_orders_source_namespace_order"), ("orders", "uq_orders_source_namespace_number"),
        ("purchase_orders", "uq_purchase_orders_po_number"),
        ("purchase_orders", "uq_purchase_orders_source_purchase_order_id"),
        ("order_items", "uq_order_items_source_order_item_id"),
        ("purchase_order_items", "uq_purchase_order_items_source_purchase_order_item_id"),
        ("shipments", "uq_shipments_tracking_id"), ("shipments", "uq_shipments_source_shipment_id"),
        ("exceptions", "uq_exceptions_deduplication_key"),
        ("source_order_identities", "uq_source_order_identity_namespace_order"),
        ("order_observations", "uq_order_observations_replay_identity_digest"),
        ("order_observation_receipts", "uq_order_observation_receipt_observation_batch"),
    ):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")

    op.execute("DROP INDEX IF EXISTS uq_exceptions_active_type_issue_key")
    op.create_index(
        "uq_exceptions_active_type_issue_key", "exceptions",
        ["dataset_version_id", "exception_type", "issue_key"], unique=True,
        postgresql_where=sa.text("status IN ('OPEN', 'ACKNOWLEDGED', 'IN_PROGRESS')"),
    )
    unique_specs = (
        ("products", "uq_products_source_product_id", ["dataset_version_id", "source_product_id"]),
        ("products", "uq_products_sku", ["dataset_version_id", "sku"]),
        ("warehouses", "uq_warehouses_source_warehouse_id", ["dataset_version_id", "source_warehouse_id"]),
        ("warehouses", "uq_warehouses_code", ["dataset_version_id", "code"]),
        ("suppliers", "uq_suppliers_source_supplier_id", ["dataset_version_id", "source_supplier_id"]),
        ("suppliers", "uq_suppliers_code", ["dataset_version_id", "code"]),
        ("inventory", "uq_inventory_product_warehouse", ["dataset_version_id", "product_id", "warehouse_id"]),
        ("inventory_movements", "uq_inventory_movements_source_movement_id", ["dataset_version_id", "source_movement_id"]),
        ("source_order_identities", "uq_source_order_identity_namespace_order", ["dataset_version_id", "source_namespace", "source_order_id"]),
        ("orders", "uq_orders_source_namespace_order", ["dataset_version_id", "source_namespace", "source_order_id"]),
        ("orders", "uq_orders_source_namespace_number", ["dataset_version_id", "source_namespace", "order_number"]),
        ("order_items", "uq_order_items_source_order_item_id", ["dataset_version_id", "source_order_item_id"]),
        ("purchase_orders", "uq_purchase_orders_source_purchase_order_id", ["dataset_version_id", "source_purchase_order_id"]),
        ("purchase_orders", "uq_purchase_orders_po_number", ["dataset_version_id", "po_number"]),
        ("purchase_order_items", "uq_purchase_order_items_source_purchase_order_item_id", ["dataset_version_id", "source_purchase_order_item_id"]),
        ("shipments", "uq_shipments_source_shipment_id", ["dataset_version_id", "source_shipment_id"]),
        ("shipments", "uq_shipments_tracking_id", ["dataset_version_id", "tracking_id"]),
        ("exceptions", "uq_exceptions_deduplication_key", ["dataset_version_id", "deduplication_key"]),
        ("order_observation_receipts", "uq_order_observation_receipt_observation_batch", ["dataset_version_id", "observation_id", "batch_id"]),
        ("order_observations", "uq_order_observations_replay_identity_digest", ["dataset_version_id", "replay_identity_digest"]),
    )
    for table, name, columns in unique_specs:
        op.create_unique_constraint(name, table, columns)



def downgrade() -> None:
    # Recreating global uniqueness would make downgrade destructive/ambiguous once
    # two versions coexist.  Only a disposable single-version database may do it.
    op.execute(
        """
        DO $$
        BEGIN
            IF (SELECT count(*) FROM dataset_versions) > 1 THEN
                RAISE EXCEPTION 'dataset-version downgrade requires a disposable single-version database';
            END IF;
        END $$
        """
    )
    for table, name in (
        ("order_observations", "uq_order_observations_replay_identity_digest"),
        ("order_observation_receipts", "uq_order_observation_receipt_observation_batch"),
        ("products", "uq_products_source_product_id"), ("products", "uq_products_sku"),
        ("warehouses", "uq_warehouses_source_warehouse_id"), ("warehouses", "uq_warehouses_code"),
        ("suppliers", "uq_suppliers_source_supplier_id"), ("suppliers", "uq_suppliers_code"),
        ("inventory", "uq_inventory_product_warehouse"),
        ("inventory_movements", "uq_inventory_movements_source_movement_id"),
        ("source_order_identities", "uq_source_order_identity_namespace_order"),
        ("orders", "uq_orders_source_namespace_order"), ("orders", "uq_orders_source_namespace_number"),
        ("order_items", "uq_order_items_source_order_item_id"), ("purchase_orders", "uq_purchase_orders_source_purchase_order_id"),
        ("purchase_orders", "uq_purchase_orders_po_number"),
        ("purchase_order_items", "uq_purchase_order_items_source_purchase_order_item_id"),
        ("shipments", "uq_shipments_source_shipment_id"), ("shipments", "uq_shipments_tracking_id"),
        ("exceptions", "uq_exceptions_deduplication_key"),
    ):
        op.drop_constraint(name, table, type_="unique")
    op.drop_index("uq_exceptions_active_type_issue_key", table_name="exceptions")
    op.create_index(
        "uq_exceptions_active_type_issue_key", "exceptions", ["exception_type", "issue_key"], unique=True,
        postgresql_where=sa.text("status IN ('OPEN', 'ACKNOWLEDGED', 'IN_PROGRESS')"),
    )
    for table, name, columns in (
        ("products", "uq_products_source_product_id", ["source_product_id"]),
        ("products", "uq_products_sku", ["sku"]), ("warehouses", "uq_warehouses_source_warehouse_id", ["source_warehouse_id"]),
        ("warehouses", "uq_warehouses_code", ["code"]), ("suppliers", "uq_suppliers_source_supplier_id", ["source_supplier_id"]),
        ("suppliers", "uq_suppliers_code", ["code"]), ("inventory", "uq_inventory_product_warehouse", ["product_id", "warehouse_id"]),
        ("inventory_movements", "uq_inventory_movements_source_movement_id", ["source_movement_id"]),
        ("orders", "uq_orders_source_namespace_order", ["source_namespace", "source_order_id"]),
        ("orders", "uq_orders_source_namespace_number", ["source_namespace", "order_number"]),
        ("purchase_orders", "uq_purchase_orders_source_purchase_order_id", ["source_purchase_order_id"]),
        ("purchase_orders", "uq_purchase_orders_po_number", ["po_number"]), ("order_items", "uq_order_items_source_order_item_id", ["source_order_item_id"]),
        ("purchase_order_items", "uq_purchase_order_items_source_purchase_order_item_id", ["source_purchase_order_item_id"]),
        ("shipments", "uq_shipments_source_shipment_id", ["source_shipment_id"]), ("shipments", "uq_shipments_tracking_id", ["tracking_id"]),
        ("exceptions", "uq_exceptions_deduplication_key", ["deduplication_key"]),
        ("source_order_identities", "uq_source_order_identity_namespace_order", ["source_namespace", "source_order_id"]),
        ("order_observation_receipts", "uq_order_observation_receipt_observation_batch", ["observation_id", "batch_id"]),
        ("order_observations", "uq_order_observations_replay_identity_digest", ["replay_identity_digest"]),
    ):
        op.create_unique_constraint(name, table, columns)

    for table in reversed(_SCOPED_TABLES):
        op.drop_index(f"ix_{table}_dataset_version_id", table_name=table)
        op.drop_constraint(_FK_NAMES[table], table, type_="foreignkey")
        op.drop_column(table, "dataset_version_id")
    op.drop_table("dataset_activation")
    op.drop_index("ix_dataset_versions_status", table_name="dataset_versions")
    op.drop_table("dataset_versions")
