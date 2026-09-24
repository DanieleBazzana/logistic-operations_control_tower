"""Regression coverage for the populated M07.6 dataset-version backfill."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Iterator
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from control_tower.config import Settings, set_alembic_database_url
from control_tower.db import create_db_engine

_SCOPED_TABLES = (
    "products",
    "warehouses",
    "inventory",
    "inventory_movements",
    "source_order_identities",
    "orders",
    "order_observations",
    "order_observation_receipts",
    "order_items",
    "suppliers",
    "purchase_orders",
    "purchase_order_items",
    "shipments",
    "exceptions",
    "exception_history",
)
_LEGACY_IDENTITY = "1463efc2d5062bc493ff278f9e6ad43d207f065ec33cabadc2287911752ebb7e"
_LEGACY_REVISION = "20260911_01"
_DATASET_VERSION_REVISION = "20260917_01"
_SECOND_DATASET_IDENTITY = "2" * 64
_DATASET_UNIQUE_CONSTRAINTS = (
    "uq_products_source_product_id",
    "uq_products_sku",
    "uq_warehouses_source_warehouse_id",
    "uq_warehouses_code",
    "uq_suppliers_source_supplier_id",
    "uq_suppliers_code",
    "uq_inventory_product_warehouse",
    "uq_inventory_movements_source_movement_id",
    "uq_source_order_identity_namespace_order",
    "uq_orders_source_namespace_order",
    "uq_orders_source_namespace_number",
    "uq_order_items_source_order_item_id",
    "uq_purchase_orders_source_purchase_order_id",
    "uq_purchase_orders_po_number",
    "uq_purchase_order_items_source_purchase_order_item_id",
    "uq_shipments_source_shipment_id",
    "uq_shipments_tracking_id",
    "uq_exceptions_deduplication_key",
    "uq_order_observation_receipt_observation_batch",
    "uq_order_observations_replay_identity_digest",
)
_LEGACY_UNIQUE_CONSTRAINTS = (
    ("products", "uq_products_source_product_id", ("source_product_id",)),
    ("products", "uq_products_sku", ("sku",)),
    ("warehouses", "uq_warehouses_source_warehouse_id", ("source_warehouse_id",)),
    ("warehouses", "uq_warehouses_code", ("code",)),
    ("suppliers", "uq_suppliers_source_supplier_id", ("source_supplier_id",)),
    ("suppliers", "uq_suppliers_code", ("code",)),
    ("inventory", "uq_inventory_product_warehouse", ("product_id", "warehouse_id")),
    ("inventory_movements", "uq_inventory_movements_source_movement_id", ("source_movement_id",)),
    (
        "source_order_identities",
        "uq_source_order_identity_namespace_order",
        ("source_namespace", "source_order_id"),
    ),
    ("orders", "uq_orders_source_namespace_order", ("source_namespace", "source_order_id")),
    ("orders", "uq_orders_source_namespace_number", ("source_namespace", "order_number")),
    ("order_items", "uq_order_items_source_order_item_id", ("source_order_item_id",)),
    (
        "purchase_orders",
        "uq_purchase_orders_source_purchase_order_id",
        ("source_purchase_order_id",),
    ),
    ("purchase_orders", "uq_purchase_orders_po_number", ("po_number",)),
    (
        "purchase_order_items",
        "uq_purchase_order_items_source_purchase_order_item_id",
        ("source_purchase_order_item_id",),
    ),
    ("shipments", "uq_shipments_source_shipment_id", ("source_shipment_id",)),
    ("shipments", "uq_shipments_tracking_id", ("tracking_id",)),
    ("exceptions", "uq_exceptions_deduplication_key", ("deduplication_key",)),
    (
        "order_observation_receipts",
        "uq_order_observation_receipt_observation_batch",
        ("observation_id", "batch_id"),
    ),
    (
        "order_observations",
        "uq_order_observations_replay_identity_digest",
        ("replay_identity_digest",),
    ),
)


@pytest.fixture
def disposable_database_url() -> Iterator[str]:
    raw_database_url = os.getenv("TEST_DATABASE_URL")
    if not raw_database_url:
        pytest.skip("TEST_DATABASE_URL is not set; start disposable PostgreSQL 16 for this gate")
    try:
        parsed_url = make_url(raw_database_url)
    except (ArgumentError, TypeError, ValueError) as error:
        pytest.fail(f"TEST_DATABASE_URL must be a valid SQLAlchemy URL: {error}")

    if parsed_url.get_backend_name() != "postgresql":
        pytest.fail("TEST_DATABASE_URL must be a PostgreSQL URL")
    if parsed_url.query:
        pytest.fail("TEST_DATABASE_URL must not include query parameters")
    if (parsed_url.host or "").lower() not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("integration test requires a local PostgreSQL database")
    if parsed_url.database != "control_tower_m04" and not (
        parsed_url.database and parsed_url.database.endswith("_test")
    ):
        pytest.fail("integration test requires control_tower_m04 or a *_test database")

    database_name = f"control_tower_legacy_{uuid4().hex}"
    admin_url = parsed_url.set(database="postgres")
    target_url = parsed_url.set(database=database_name)
    admin_engine = create_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
        future=True,
    )
    try:
        with admin_engine.connect() as connection:
            database_identifier = connection.dialect.identifier_preparer.quote(database_name)
            connection.execute(text(f"CREATE DATABASE {database_identifier}"))
    finally:
        admin_engine.dispose()

    try:
        yield target_url.render_as_string(hide_password=False)
    finally:
        # Each test disposes its target engine in its own finally block before
        # this fixture teardown attempts to drop the disposable database.
        cleanup_engine = create_engine(
            admin_url,
            isolation_level="AUTOCOMMIT",
            pool_pre_ping=True,
            future=True,
        )
        try:
            with cleanup_engine.connect() as connection:
                database_identifier = connection.dialect.identifier_preparer.quote(database_name)
                connection.execute(text(f"DROP DATABASE {database_identifier}"))
                remaining = connection.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :database_name"),
                    {"database_name": database_name},
                ).scalar()
                if remaining is not None:
                    raise AssertionError(
                        f"disposable database {database_name!r} still exists after teardown"
                    )
        finally:
            cleanup_engine.dispose()


def _migration_state(connection: Connection) -> tuple[object, ...]:
    scoped_columns = tuple(
        connection.execute(
            text(
                "SELECT table_name, column_name, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND column_name = 'dataset_version_id' "
                "ORDER BY table_name"
            )
        ).tuples()
    )
    dataset_constraints = tuple(
        connection.execute(
            text(
                "SELECT conname, pg_get_constraintdef(oid) "
                "FROM pg_constraint "
                "WHERE connamespace = 'public'::regnamespace "
                "AND (conname LIKE 'fk_dv_%' OR conname LIKE 'pk_dataset_%' "
                "OR conname LIKE 'uq_dataset_%' OR conname LIKE 'ck_dataset_%' "
                "OR conname = 'fk_dataset_activation_active_dataset_version_id') "
                "ORDER BY conname"
            )
        ).tuples()
    )
    dataset_indexes = tuple(
        connection.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' "
                "AND (indexname LIKE 'ix_%_dataset_version_id' "
                "OR indexname = 'uq_exceptions_active_type_issue_key') "
                "ORDER BY indexname"
            )
        ).tuples()
    )
    dataset_versions = tuple(
        connection.execute(
            text(
                "SELECT id, dataset_key, identity_hash, manifest_identity, content_hash, seed, "
                "as_of, generator_revision, status FROM dataset_versions ORDER BY id"
            )
        ).tuples()
    )
    activation = tuple(
        connection.execute(
            text("SELECT id, active_dataset_version_id FROM dataset_activation ORDER BY id")
        ).tuples()
    )
    revisions = tuple(sorted(MigrationContext.configure(connection).get_current_heads()))
    return (
        revisions,
        scoped_columns,
        dataset_constraints,
        dataset_indexes,
        dataset_versions,
        activation,
    )


def _seed_populated_legacy_m076(connection: Connection) -> dict[str, object]:
    product_id = connection.execute(
        text(
            "INSERT INTO products "
            "(source_product_id, sku, name, unit_price) "
            "VALUES ('legacy-product-1', 'LEGACY-SKU-1', 'Legacy Product', 12.50) "
            "RETURNING id"
        )
    ).scalar_one()
    supplier_id = connection.execute(
        text(
            "INSERT INTO suppliers (source_supplier_id, code, name, region) "
            "VALUES ('legacy-supplier-1', 'LEGACY-SUP-1', 'Legacy Supplier', 'EU') RETURNING id"
        )
    ).scalar_one()
    warehouse_id = connection.execute(
        text(
            "INSERT INTO warehouses (source_warehouse_id, code, name, region, timezone) "
            "VALUES ('legacy-warehouse-1', 'LEGACY-WH-1', 'Legacy Warehouse', 'EU', 'UTC') "
            "RETURNING id"
        )
    ).scalar_one()
    connection.execute(
        text(
            "INSERT INTO inventory (product_id, warehouse_id, on_hand, reserved, observed_at) "
            "VALUES (:product_id, :warehouse_id, 10, 2, :observed_at)"
        ),
        {
            "product_id": product_id,
            "warehouse_id": warehouse_id,
            "observed_at": datetime(2025, 3, 7, 18, tzinfo=timezone.utc),
        },
    )
    connection.execute(
        text(
            "INSERT INTO inventory_movements "
            "(source_movement_id, product_id, warehouse_id, movement_type, quantity, occurred_at) "
            "VALUES ('legacy-movement-1', :product_id, :warehouse_id, 'RECEIPT', 10, :occurred_at)"
        ),
        {
            "product_id": product_id,
            "warehouse_id": warehouse_id,
            "occurred_at": datetime(2025, 3, 7, 17, tzinfo=timezone.utc),
        },
    )
    order_id = connection.execute(
        text(
            "INSERT INTO orders "
            "(source_order_id, order_number, status, region, warehouse_id, ordered_at, "
            "promised_at, "
            "total_amount, currency) "
            "VALUES ('legacy-order-1', 'LEGACY-ORDER-1', 'OPEN', 'EU', :warehouse_id, "
            ":ordered_at, :promised_at, 25.00, 'EUR') RETURNING id"
        ),
        {
            "warehouse_id": warehouse_id,
            "ordered_at": datetime(2025, 3, 1, 10, tzinfo=timezone.utc),
            "promised_at": datetime(2025, 3, 10, 10, tzinfo=timezone.utc),
        },
    ).scalar_one()
    identity_id = connection.execute(
        text(
            "INSERT INTO source_order_identities (source_namespace, source_order_id) "
            "VALUES ('legacy-m076', 'legacy-order-1') RETURNING id"
        )
    ).scalar_one()
    observation_id = connection.execute(
        text(
            "INSERT INTO order_observations "
            "(source_order_identity_id, source_namespace, source_order_id, source_version, "
            "source_row_hash, replay_identity, replay_identity_digest, source_facts, order_number, "
            "order_status, region, source_warehouse_id, warehouse_id, ordered_at, promised_at, "
            "total_amount, currency, status, source_status, capabilities, non_promotion_reasons) "
            "VALUES (:identity_id, 'legacy-m076', 'legacy-order-1', 'v1', 'legacy-row-hash', "
            "CAST(:replay_identity AS jsonb), 'legacy-replay-digest', "
            "CAST(:source_facts AS jsonb), "
            "'LEGACY-ORDER-1', 'OPEN', 'EU', 'legacy-warehouse-1', :warehouse_id, :ordered_at, "
            ":promised_at, 25.00, 'EUR', 'PROMOTED', 'PROMOTED', CAST(:capabilities AS jsonb), "
            "CAST(:non_promotion_reasons AS jsonb)) RETURNING id"
        ),
        {
            "identity_id": identity_id,
            "warehouse_id": warehouse_id,
            "ordered_at": datetime(2025, 3, 1, 10, tzinfo=timezone.utc),
            "promised_at": datetime(2025, 3, 10, 10, tzinfo=timezone.utc),
            "replay_identity": '{"source": "legacy-m076", "order": "legacy-order-1"}',
            "source_facts": '{"source_order_id": "legacy-order-1", "total_amount": "25.00"}',
            "capabilities": '["PROMOTE"]',
            "non_promotion_reasons": "[]",
        },
    ).scalar_one()
    connection.execute(
        text(
            "INSERT INTO order_observation_receipts (observation_id, batch_id) "
            "VALUES (:observation_id, 'legacy-m076-batch')"
        ),
        {"observation_id": observation_id},
    )
    connection.execute(
        text(
            "INSERT INTO order_items "
            "(source_order_item_id, order_id, product_id, line_number, ordered_quantity, "
            "fulfilled_quantity, unit_price) "
            "VALUES ('legacy-order-item-1', :order_id, :product_id, 1, 2, 0, 12.50)"
        ),
        {"order_id": order_id, "product_id": product_id},
    )
    purchase_order_id = connection.execute(
        text(
            "INSERT INTO purchase_orders "
            "(source_purchase_order_id, po_number, supplier_id, warehouse_id, status, ordered_at, "
            "expected_delivery_at) "
            "VALUES ('legacy-po-1', 'LEGACY-PO-1', :supplier_id, :warehouse_id, 'OPEN', "
            ":ordered_at, :expected_delivery_at) RETURNING id"
        ),
        {
            "supplier_id": supplier_id,
            "warehouse_id": warehouse_id,
            "ordered_at": datetime(2025, 3, 1, 9, tzinfo=timezone.utc),
            "expected_delivery_at": datetime(2025, 3, 12, 9, tzinfo=timezone.utc),
        },
    ).scalar_one()
    connection.execute(
        text(
            "INSERT INTO purchase_order_items "
            "(source_purchase_order_item_id, purchase_order_id, product_id, ordered_quantity, "
            "received_quantity, unit_cost) VALUES ('legacy-po-item-1', :purchase_order_id, "
            ":product_id, 5, 0, 10.00)"
        ),
        {"purchase_order_id": purchase_order_id, "product_id": product_id},
    )
    connection.execute(
        text(
            "INSERT INTO shipments "
            "(source_shipment_id, order_id, carrier, tracking_id, status, eta) "
            "VALUES ('legacy-shipment-1', :order_id, 'Legacy Carrier', 'LEGACY-TRACK-1', "
            "'CREATED', :eta)"
        ),
        {"order_id": order_id, "eta": datetime(2025, 3, 11, 10, tzinfo=timezone.utc)},
    )
    exception_id = connection.execute(
        text(
            "INSERT INTO exceptions "
            "(deduplication_key, exception_type, issue_key, entity_type, entity_id, severity, "
            "status, "
            "detected_at, business_impact, revenue_at_risk, orders_affected, root_cause, "
            "recommended_action, confidence, warehouse_id, product_id) "
            "VALUES ('legacy-dedupe-1', 'INVENTORY_SHORTAGE', 'legacy-shortage-1', 'PRODUCT', "
            "'legacy-product-1', 'HIGH', 'OPEN', :detected_at, 'Legacy shortage', 10.00, 1, "
            "'Legacy fixture', 'Replenish', 0.9000, :warehouse_id, :product_id) RETURNING id"
        ),
        {
            "detected_at": datetime(2025, 3, 7, 18, tzinfo=timezone.utc),
            "warehouse_id": warehouse_id,
            "product_id": product_id,
        },
    ).scalar_one()
    history_id = connection.execute(
        text(
            "INSERT INTO exception_history "
            "(exception_id, from_status, to_status, changed_at, actor, transition_reason) "
            "VALUES (:exception_id, NULL, 'OPEN', :changed_at, 'legacy-fixture', "
            "'initial detection') "
            "RETURNING id"
        ),
        {
            "exception_id": exception_id,
            "changed_at": datetime(2025, 3, 7, 18, tzinfo=timezone.utc),
        },
    ).scalar_one()
    return {"exception_id": exception_id, "history_id": history_id}


def _counts(connection: Connection) -> dict[str, int]:
    return {
        table: connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        for table in _SCOPED_TABLES
    }


def _payloads(connection: Connection, *, after_backfill: bool = False) -> dict[str, list[object]]:
    expression = "to_jsonb(t) - 'dataset_version_id'" if after_backfill else "to_jsonb(t)"
    return {
        table: list(
            connection.execute(text(f"SELECT {expression} FROM {table} AS t ORDER BY t.id"))
            .scalars()
            .all()
        )
        for table in _SCOPED_TABLES
    }


@pytest.mark.integration
def test_populated_m076_legacy_backfill_preserves_append_only_history(
    disposable_database_url: str,
) -> None:
    database_url = disposable_database_url
    engine = create_db_engine(Settings.model_validate({"database_url": database_url}))
    config = Config("alembic.ini")
    set_alembic_database_url(config, database_url)
    try:
        command.upgrade(config, _LEGACY_REVISION)
        with engine.begin() as connection:
            seeded = _seed_populated_legacy_m076(connection)
            before_counts = _counts(connection)
            before_history = (
                connection.execute(
                    text(
                        "SELECT exception_id, from_status::text, to_status::text, changed_at, "
                        "actor, transition_reason FROM exception_history WHERE id = :history_id"
                    ),
                    {"history_id": seeded["history_id"]},
                )
                .mappings()
                .one()
            )
            before_payloads = _payloads(connection)

        with pytest.raises(SQLAlchemyError, match="exception_history is append-only"):
            with engine.begin() as connection:
                connection.execute(
                    text("UPDATE exception_history SET actor = 'tampered' WHERE id = :history_id"),
                    {"history_id": seeded["history_id"]},
                )
        with pytest.raises(SQLAlchemyError, match="exception_history is append-only"):
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM exception_history WHERE id = :history_id"),
                    {"history_id": seeded["history_id"]},
                )

        command.upgrade(config, "20260917_01")

        with engine.connect() as connection:
            assert _counts(connection) == before_counts
            assert _payloads(connection, after_backfill=True) == before_payloads
            assert (
                connection.execute(text("SELECT count(*) FROM dataset_versions")).scalar_one() == 1
            )
            dataset = (
                connection.execute(
                    text(
                        "SELECT id, dataset_key, identity_hash, status FROM dataset_versions "
                        "WHERE identity_hash = :identity_hash"
                    ),
                    {"identity_hash": _LEGACY_IDENTITY},
                )
                .mappings()
                .one()
            )
            assert dict(dataset) == {
                "id": dataset["id"],
                "dataset_key": "M07.6",
                "identity_hash": _LEGACY_IDENTITY,
                "status": "ACTIVE",
            }
            assert (
                connection.execute(
                    text("SELECT active_dataset_version_id FROM dataset_activation WHERE id = 1")
                ).scalar_one()
                == dataset["id"]
            )
            assert (
                connection.execute(
                    text(
                        "SELECT exception_id, from_status::text, to_status::text, changed_at, "
                        "actor, transition_reason FROM exception_history WHERE id = :history_id"
                    ),
                    {"history_id": seeded["history_id"]},
                )
                .mappings()
                .one()
                == before_history
            )
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM exception_history "
                        "WHERE dataset_version_id = :dataset_id"
                    ),
                    {"dataset_id": dataset["id"]},
                ).scalar_one()
                == 1
            )

            for table in _SCOPED_TABLES:
                assert (
                    connection.execute(
                        text(
                            "SELECT count(*) FROM "
                            "information_schema.columns WHERE table_schema = 'public' "
                            "AND table_name = :table_name AND column_name = 'dataset_version_id' "
                            "AND is_nullable = 'NO'"
                        ),
                        {"table_name": table},
                    ).scalar_one()
                    == 1
                )
                assert (
                    connection.execute(
                        text(
                            "SELECT count(*) FROM pg_indexes WHERE schemaname = 'public' "
                            "AND indexname = :index_name"
                        ),
                        {"index_name": f"ix_{table}_dataset_version_id"},
                    ).scalar_one()
                    == 1
                )
                assert (
                    connection.execute(
                        text(
                            "SELECT count(*) FROM pg_constraint "
                            "WHERE connamespace = 'public'::regnamespace "
                            "AND conname = :constraint_name"
                        ),
                        {"constraint_name": f"fk_dv_{table}"},
                    ).scalar_one()
                    == 1
                )

            for constraint_name in _DATASET_UNIQUE_CONSTRAINTS:
                assert (
                    connection.execute(
                        text(
                            "SELECT count(*) FROM pg_constraint "
                            "WHERE connamespace = 'public'::regnamespace "
                            "AND conname = :constraint_name"
                        ),
                        {"constraint_name": constraint_name},
                    ).scalar_one()
                    == 1
                )
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM pg_indexes WHERE schemaname = 'public' "
                        "AND indexname = 'uq_exceptions_active_type_issue_key'"
                    )
                ).scalar_one()
                == 1
            )

            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM pg_trigger "
                        "WHERE tgrelid = 'exception_history'::regclass "
                        "AND tgname IN ('trg_exception_history_append_only', "
                        "'trg_exception_history_append_only_truncate') AND tgenabled = 'O'"
                    )
                ).scalar_one()
                == 2
            )
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM pg_constraint WHERE conname IN "
                        "('pk_dataset_versions', 'uq_dataset_versions_identity_hash', "
                        "'uq_dataset_versions_logical_identity', "
                        "'ck_dataset_versions_ck_dataset_versions_status', "
                        "'pk_dataset_activation', "
                        "'ck_dataset_activation_ck_dataset_activation_singleton', "
                        "'fk_dataset_activation_active_dataset_version_id')"
                    )
                ).scalar_one()
                == 7
            )

        with pytest.raises(SQLAlchemyError, match="exception_history is append-only"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE exception_history SET actor = 'tampered-after' "
                        "WHERE id = :history_id"
                    ),
                    {"history_id": seeded["history_id"]},
                )
        with pytest.raises(SQLAlchemyError, match="exception_history is append-only"):
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM exception_history WHERE id = :history_id"),
                    {"history_id": seeded["history_id"]},
                )
    finally:
        engine.dispose()


@pytest.mark.integration
def test_single_dataset_downgrade_restores_legacy_schema(disposable_database_url: str) -> None:
    database_url = disposable_database_url
    engine = create_db_engine(Settings.model_validate({"database_url": database_url}))
    config = Config("alembic.ini")
    set_alembic_database_url(config, database_url)
    try:
        command.upgrade(config, _LEGACY_REVISION)
        command.upgrade(config, _DATASET_VERSION_REVISION)
        with engine.connect() as connection:
            assert set(MigrationContext.configure(connection).get_current_heads()) == {
                _DATASET_VERSION_REVISION
            }
            assert (
                connection.execute(text("SELECT count(*) FROM dataset_versions")).scalar_one() == 1
            )

        command.downgrade(config, _LEGACY_REVISION)

        with engine.connect() as connection:
            assert set(MigrationContext.configure(connection).get_current_heads()) == {
                _LEGACY_REVISION
            }
            table_names = set(inspect(connection).get_table_names())
            assert {"dataset_versions", "dataset_activation"}.isdisjoint(table_names)
            for table in _SCOPED_TABLES:
                assert "dataset_version_id" not in {
                    column["name"] for column in inspect(connection).get_columns(table)
                }
            for table, constraint_name, columns in _LEGACY_UNIQUE_CONSTRAINTS:
                constraints = {
                    constraint["name"]: tuple(constraint["column_names"])
                    for constraint in inspect(connection).get_unique_constraints(table)
                }
                assert constraints[constraint_name] == columns
            legacy_index = connection.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname = 'public' "
                    "AND indexname = 'uq_exceptions_active_type_issue_key'"
                )
            ).scalar_one()
            assert "(exception_type, issue_key)" in legacy_index
    finally:
        engine.dispose()


@pytest.mark.integration
def test_multi_dataset_downgrade_fails_closed_without_changing_state(
    disposable_database_url: str,
) -> None:
    database_url = disposable_database_url
    engine = create_db_engine(Settings.model_validate({"database_url": database_url}))
    config = Config("alembic.ini")
    set_alembic_database_url(config, database_url)
    try:
        command.upgrade(config, _LEGACY_REVISION)
        command.upgrade(config, _DATASET_VERSION_REVISION)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO dataset_versions "
                    "(dataset_key, identity_hash, manifest_identity, content_hash, seed, as_of, "
                    "generator_revision, status) VALUES "
                    "('M07.6-secondary', :identity_hash, 'm07.6-secondary', :content_hash, "
                    "20250302, TIMESTAMP WITH TIME ZONE '2025-03-08 18:00:00+00', 'm07.6', 'READY')"
                ),
                {"identity_hash": _SECOND_DATASET_IDENTITY, "content_hash": "3" * 64},
            )
        with engine.connect() as connection:
            before_state = _migration_state(connection)

        with pytest.raises(
            SQLAlchemyError,
            match="dataset-version downgrade requires a disposable single-version database",
        ):
            command.downgrade(config, _LEGACY_REVISION)

        with engine.connect() as connection:
            assert _migration_state(connection) == before_state
    finally:
        engine.dispose()
