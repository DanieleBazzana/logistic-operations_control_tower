from datetime import datetime
from decimal import Decimal

import pytest

from control_tower.ingestion.validation import validate_bundle_rows, validate_rows
from control_tower.synthetic.artifacts import ARTIFACT_COLUMNS


def test_validation_normalizes_strings_enums_decimals_and_utc() -> None:
    rows = [
        {
            "source_movement_id": " MOV000001 ",
            "source_product_id": " P0001 ",
            "source_warehouse_id": " W001 ",
            "movement_type": " receipt ",
            "quantity": "2.500",
            "occurred_at": "2025-01-15T12:00:00+01:00",
            "reference_type": "",
            "reference_id": "",
        }
    ]

    result = validate_rows("wms/inventory_movements.csv", rows, known_ids={"P0001", "W001"})

    assert not result.rejections
    assert result.rows[0]["source_product_id"] == "P0001"
    assert result.rows[0]["movement_type"] == "RECEIPT"
    assert result.rows[0]["quantity"] == Decimal("2.500")
    assert result.rows[0]["reference_type"] is None
    assert result.rows[0]["occurred_at"] == datetime.fromisoformat("2025-01-15T11:00:00+00:00")


@pytest.mark.parametrize("special_value", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_validation_rejects_non_finite_decimal_values_as_structured_type_errors(
    special_value: str,
) -> None:
    result = validate_rows(
        "wms/inventory_movements.csv",
        [
            {
                "source_movement_id": "MOV000001",
                "source_product_id": "P0001",
                "source_warehouse_id": "W001",
                "movement_type": "RECEIPT",
                "quantity": special_value,
                "occurred_at": "2025-01-15T12:00:00+00:00",
                "reference_type": "",
                "reference_id": "",
            }
        ],
        known_ids={"P0001", "W001"},
    )

    assert result.accepted == 0
    assert len(result.rejections) == 1
    rejection = result.rejections[0]
    assert rejection.error_code == "TYPE"
    assert rejection.field == "quantity"
    assert rejection.message == "must be a finite decimal"


def test_validation_rejects_blank_required_and_naive_timestamp() -> None:
    rows = [
        {
            "source_product_id": "",
            "source_warehouse_id": "W001",
            "movement_type": "RECEIPT",
            "quantity": "1",
            "occurred_at": "2025-01-15T12:00:00",
            "reference_type": "",
            "reference_id": "",
        }
    ]

    result = validate_rows("wms/inventory_movements.csv", rows, known_ids={"W001"})

    assert {rejection.error_code for rejection in result.rejections} >= {
        "REQUIRED",
        "NAIVE_TIMESTAMP",
    }


def test_bundle_validation_rejects_unknown_and_missing_manifest_artifacts() -> None:
    results = validate_bundle_rows(
        {"oms/products.csv": []},
        manifest_artifacts={"oms/products.csv", "wms/warehouses.csv", "unknown.csv"},
    )

    assert results["unknown.csv"].rejections[0].error_code == "UNKNOWN_ARTIFACT"
    assert results["wms/warehouses.csv"].rejections[0].error_code == "MISSING_ARTIFACT"


@pytest.mark.parametrize(
    ("header", "error_code"),
    [
        (None, "MALFORMED_HEADER"),
        (ARTIFACT_COLUMNS["oms/products.csv"][:-1], "MISSING_COLUMN"),
        (ARTIFACT_COLUMNS["oms/products.csv"] + ("unexpected",), "UNKNOWN_COLUMN"),
    ],
)
def test_bundle_validation_rejects_non_contract_csv_headers(
    header: tuple[str, ...] | None, error_code: str
) -> None:
    results = validate_bundle_rows(
        {"oms/products.csv": []},
        headers={"oms/products.csv": header},
    )

    assert any(
        rejection.error_code == error_code
        for rejection in results["oms/products.csv"].rejections
    )


def test_inventory_validation_rejects_multiple_snapshots_for_one_natural_key() -> None:
    rows = [
        {
            "source_product_id": "P0001",
            "source_warehouse_id": "W001",
            "on_hand": "10",
            "reserved": "1",
            "observed_at": "2025-01-15T10:00:00+00:00",
        },
        {
            "source_product_id": "P0001",
            "source_warehouse_id": "W001",
            "on_hand": "12",
            "reserved": "1",
            "observed_at": "2025-01-15T11:00:00+00:00",
        },
    ]

    result = validate_rows(
        "wms/inventory.csv",
        rows,
        known_ids={"products": {"P0001"}, "warehouses": {"W001"}},
    )

    assert result.accepted == 1
    assert [rejection.error_code for rejection in result.rejections] == [
        "INVENTORY_MULTIPLE_SNAPSHOTS"
    ]


def test_validation_reports_required_for_blank_inventory_on_hand_without_comparing() -> None:
    result = validate_rows(
        "wms/inventory.csv",
        [
            {
                "source_product_id": "P0001",
                "source_warehouse_id": "W001",
                "on_hand": "",
                "reserved": "3",
                "observed_at": "2025-01-15T10:00:00+00:00",
            }
        ],
        known_ids={"products": {"P0001"}, "warehouses": {"W001"}},
    )

    assert result.accepted == 0
    assert [(rejection.error_code, rejection.field) for rejection in result.rejections] == [
        ("REQUIRED", "on_hand")
    ]


@pytest.mark.parametrize(
    ("artifact", "row", "known_ids"),
    [
        (
            "oms/order_items.csv",
            {
                "source_order_item_id": "OI0001",
                "source_order_id": "O0001",
                "source_product_id": "P0001",
                "line_number": "1",
                "ordered_quantity": "",
                "fulfilled_quantity": "2",
                "unit_price": "10.00",
            },
            {"orders": {"O0001"}, "products": {"P0001"}},
        ),
        (
            "erp/purchase_order_items.csv",
            {
                "source_purchase_order_item_id": "POI0001",
                "source_purchase_order_id": "PO0001",
                "source_product_id": "P0001",
                "ordered_quantity": "",
                "received_quantity": "2",
                "unit_cost": "10.00",
            },
            {"purchase_orders": {"PO0001"}, "products": {"P0001"}},
        ),
    ],
)
def test_validation_reports_required_for_blank_ordered_quantity_without_comparing(
    artifact: str, row: dict[str, str], known_ids: dict[str, set[str]]
) -> None:
    result = validate_rows(artifact, [row], known_ids=known_ids)

    assert result.accepted == 0
    assert [(rejection.error_code, rejection.field) for rejection in result.rejections] == [
        ("REQUIRED", "ordered_quantity")
    ]


@pytest.mark.parametrize(
    ("artifact", "row", "known_ids"),
    [
        (
            "oms/orders.csv",
            {
                "source_order_id": "O0001",
                "order_number": "ORD-0001",
                "status": "OPEN",
                "region": "EU",
                "source_warehouse_id": "W001",
                "ordered_at": "",
                "promised_at": "2025-01-17T10:00:00+00:00",
                "fulfilled_at": "2025-01-16T10:00:00+00:00",
                "total_amount": "10.00",
                "currency": "EUR",
            },
            {"warehouses": {"W001"}},
        ),
        (
            "erp/purchase_orders.csv",
            {
                "source_purchase_order_id": "PO0001",
                "po_number": "PO-0001",
                "source_supplier_id": "S0001",
                "source_warehouse_id": "W001",
                "status": "OPEN",
                "ordered_at": "",
                "expected_delivery_at": "2025-01-17T10:00:00+00:00",
                "received_at": "2025-01-16T10:00:00+00:00",
            },
            {"suppliers": {"S0001"}, "warehouses": {"W001"}},
        ),
    ],
)
def test_validation_reports_required_for_blank_ordered_timestamp_without_comparing(
    artifact: str, row: dict[str, str], known_ids: dict[str, set[str]]
) -> None:
    result = validate_rows(artifact, [row], known_ids=known_ids)

    assert result.accepted == 0
    assert [(rejection.error_code, rejection.field) for rejection in result.rejections] == [
        ("REQUIRED", "ordered_at")
    ]


def test_validation_allows_blank_optional_shipped_at_with_delivered_at() -> None:
    result = validate_rows(
        "carrier/shipments.csv",
        [
            {
                "source_shipment_id": "SH0001",
                "source_order_id": "O0001",
                "carrier": "DHL",
                "tracking_id": "TRACK-0001",
                "status": "DELIVERED",
                "shipped_at": "",
                "eta": "2025-01-16T10:00:00+00:00",
                "delivered_at": "2025-01-17T10:00:00+00:00",
            }
        ],
        known_ids={"orders": {"O0001"}},
    )

    assert result.accepted == 1
    assert not result.rejections


@pytest.mark.parametrize(
    ("artifact", "row", "known_ids", "field"),
    [
        (
            "oms/order_items.csv",
            {
                "source_order_item_id": "OI0001",
                "source_order_id": "O0001",
                "source_product_id": "P0001",
                "line_number": "1",
                "ordered_quantity": "2",
                "fulfilled_quantity": "3",
                "unit_price": "10.00",
            },
            {"orders": {"O0001"}, "products": {"P0001"}},
            "fulfilled_quantity",
        ),
        (
            "erp/purchase_order_items.csv",
            {
                "source_purchase_order_item_id": "POI0001",
                "source_purchase_order_id": "PO0001",
                "source_product_id": "P0001",
                "ordered_quantity": "2",
                "received_quantity": "3",
                "unit_cost": "10.00",
            },
            {"purchase_orders": {"PO0001"}, "products": {"P0001"}},
            "received_quantity",
        ),
        (
            "wms/inventory.csv",
            {
                "source_product_id": "P0001",
                "source_warehouse_id": "W001",
                "on_hand": "2",
                "reserved": "3",
                "observed_at": "2025-01-15T10:00:00+00:00",
            },
            {"products": {"P0001"}, "warehouses": {"W001"}},
            "reserved",
        ),
    ],
)
def test_validation_preserves_nonblank_quantity_bounds_rejections(
    artifact: str,
    row: dict[str, str],
    known_ids: dict[str, set[str]],
    field: str,
) -> None:
    result = validate_rows(artifact, [row], known_ids=known_ids)

    assert any(
        rejection.error_code == "QUANTITY_BOUNDS" and rejection.field == field
        for rejection in result.rejections
    )


@pytest.mark.parametrize(
    ("artifact", "row", "known_ids", "field"),
    [
        (
            "oms/orders.csv",
            {
                "source_order_id": "O0001",
                "order_number": "ORD-0001",
                "status": "FULFILLED",
                "region": "EU",
                "source_warehouse_id": "W001",
                "ordered_at": "2025-01-17T10:00:00+00:00",
                "promised_at": "2025-01-18T10:00:00+00:00",
                "fulfilled_at": "2025-01-16T10:00:00+00:00",
                "total_amount": "10.00",
                "currency": "EUR",
            },
            {"warehouses": {"W001"}},
            "fulfilled_at",
        ),
        (
            "erp/purchase_orders.csv",
            {
                "source_purchase_order_id": "PO0001",
                "po_number": "PO-0001",
                "source_supplier_id": "S0001",
                "source_warehouse_id": "W001",
                "status": "RECEIVED",
                "ordered_at": "2025-01-17T10:00:00+00:00",
                "expected_delivery_at": "2025-01-18T10:00:00+00:00",
                "received_at": "2025-01-16T10:00:00+00:00",
            },
            {"suppliers": {"S0001"}, "warehouses": {"W001"}},
            "received_at",
        ),
        (
            "carrier/shipments.csv",
            {
                "source_shipment_id": "SH0001",
                "source_order_id": "O0001",
                "carrier": "DHL",
                "tracking_id": "TRACK-0001",
                "status": "DELIVERED",
                "shipped_at": "2025-01-17T10:00:00+00:00",
                "eta": "2025-01-18T10:00:00+00:00",
                "delivered_at": "2025-01-16T10:00:00+00:00",
            },
            {"orders": {"O0001"}},
            "delivered_at",
        ),
    ],
)
def test_validation_preserves_nonblank_timestamp_order_rejections(
    artifact: str,
    row: dict[str, str],
    known_ids: dict[str, set[str]],
    field: str,
) -> None:
    result = validate_rows(artifact, [row], known_ids=known_ids)

    assert any(
        rejection.error_code == "TIMESTAMP_ORDER" and rejection.field == field
        for rejection in result.rejections
    )
