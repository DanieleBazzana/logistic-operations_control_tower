import csv
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from control_tower.ingestion.contracts import ArtifactBundle, ValidationResult
from control_tower.ingestion.readers import read_bundle
from control_tower.ingestion.validation import validate_bundle_rows
from control_tower.synthetic.generator import generate


def _files(root: Path) -> list[Path]:
    return sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file())


def test_generation_is_byte_identical_for_same_seed_and_as_of(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"

    generate(first, seed=77, as_of="2025-01-15T12:00:00+00:00")
    generate(second, seed=77, as_of="2025-01-15T12:00:00+00:00")

    first_files = _files(first)
    second_files = _files(second)
    assert first_files == second_files
    assert [p.read_bytes() for p in (first / f for f in first_files)] == [
        p.read_bytes() for p in (second / f for f in second_files)
    ]


def test_default_profile_has_four_warehouses_and_operational_scale(tmp_path: Path) -> None:
    manifest = generate(tmp_path, seed=78, as_of="2025-03-07T18:00:00Z")

    assert manifest["artifacts"]["oms/products.csv"]["row_count"] >= 220
    assert manifest["artifacts"]["wms/warehouses.csv"]["row_count"] >= 4
    assert manifest["artifacts"]["erp/suppliers.csv"]["row_count"] >= 11
    assert manifest["artifacts"]["oms/orders.csv"]["row_count"] >= 1400


def test_default_profile_has_warehouse_and_fulfillment_variation(tmp_path: Path) -> None:
    generate(tmp_path, seed=79, as_of="2025-03-07T18:00:00Z")
    orders = list(csv.DictReader((tmp_path / "oms/orders.csv").open()))

    by_warehouse = Counter(row["source_warehouse_id"] for row in orders)
    fulfilled = [row for row in orders if row["status"] == "FULFILLED"]
    late = [row for row in fulfilled if row["fulfilled_at"] > row["promised_at"]]

    assert by_warehouse["W001"] > by_warehouse["W002"] > by_warehouse["W003"] > by_warehouse["W004"]
    assert late
    assert all(row["fulfilled_at"] <= "2025-03-07T18:00:00+00:00" for row in fulfilled)


def test_default_profile_has_varied_supplier_receipt_facts(tmp_path: Path) -> None:
    generate(tmp_path, seed=80, as_of="2025-03-07T18:00:00Z")
    purchase_orders = list(csv.DictReader((tmp_path / "erp/purchase_orders.csv").open()))
    suppliers = Counter(row["source_supplier_id"] for row in purchase_orders)
    statuses = Counter(row["status"] for row in purchase_orders)
    pressure_warehouse = Counter(
        row["source_warehouse_id"]
        for row in purchase_orders
        if row["source_supplier_id"] in {"SUP001", "SUP002", "SUP003"}
    )

    assert len(suppliers) >= 3
    assert len(statuses) >= 3
    assert pressure_warehouse["W003"] > pressure_warehouse["W001"]


def test_default_profile_has_broad_shipment_coverage(tmp_path: Path) -> None:
    generate(tmp_path, seed=81, as_of="2025-03-07T18:00:00Z")
    shipments = list(csv.DictReader((tmp_path / "carrier/shipments.csv").open()))

    assert len(shipments) > 500
    assert {row["status"] for row in shipments} == {"DELIVERED", "IN_TRANSIT"}
    assert len({row["carrier"] for row in shipments}) >= 3


def test_default_profile_spreads_inventory_risk_and_reconciliation_facts(tmp_path: Path) -> None:
    generate(tmp_path, seed=82, as_of="2025-03-07T18:00:00Z")
    inventory = list(csv.DictReader((tmp_path / "wms/inventory.csv").open()))
    movements = list(csv.DictReader((tmp_path / "wms/inventory_movements.csv").open()))

    low_stock_warehouses = {
        row["source_warehouse_id"] for row in inventory if Decimal(row["on_hand"]) <= Decimal("8")
    }
    mismatch_warehouses = {
        row["source_warehouse_id"]
        for row in movements
        if row["reference_type"] == "MISMATCH_FIXTURE"
    }

    assert low_stock_warehouses == {"W001", "W002", "W003", "W004"}
    assert mismatch_warehouses == {"W001", "W002", "W003", "W004"}


def test_generation_manifest_has_target_volumes_and_six_source_scenarios(tmp_path: Path) -> None:
    manifest = generate(tmp_path, seed=78, as_of="2025-01-15T12:00:00+00:00")

    assert manifest["artifacts"]["oms/products.csv"]["row_count"] == 240
    assert manifest["artifacts"]["wms/warehouses.csv"]["row_count"] == 4
    assert manifest["artifacts"]["erp/suppliers.csv"]["row_count"] == 12
    assert manifest["artifacts"]["oms/orders.csv"]["row_count"] == 1500
    assert {scenario["scenario_id"] for scenario in manifest["scenarios"]} == {
        "SLA_BREACH_RISK",
        "INVENTORY_SHORTAGE",
        "STOCKOUT_RISK",
        "INVENTORY_MISMATCH",
        "SUPPLIER_DELAY",
        "SHIPMENT_DELAY",
    }
    assert not (tmp_path / "exceptions.csv").exists()


def test_generation_overrides_seed_and_as_of(tmp_path: Path) -> None:
    manifest = generate(
        tmp_path,
        seed=91,
        as_of="2026-04-05T08:00:00+02:00",
        product_count=4,
        warehouse_count=1,
        supplier_count=2,
        order_count=6,
    )

    assert manifest["seed"] == 91
    assert manifest["as_of"] == "2026-04-05T06:00:00+00:00"
    assert manifest["artifacts"]["oms/products.csv"]["row_count"] == 4
    assert manifest["artifacts"]["wms/inventory.csv"]["row_count"] == 4


@pytest.mark.parametrize(
    ("dimension", "value"),
    [
        ("product_count", 2),
        ("warehouse_count", 0),
        ("supplier_count", 0),
        ("order_count", 3),
    ],
)
def test_generation_rejects_dimensions_below_scenario_references(
    tmp_path: Path, dimension: str, value: int
) -> None:
    dimensions = {
        "product_count": 200,
        "warehouse_count": 3,
        "supplier_count": 10,
        "order_count": 1200,
    }
    dimensions[dimension] = value
    with pytest.raises(ValueError, match="scenario references"):
        generate(
            tmp_path,
            product_count=dimensions["product_count"],
            warehouse_count=dimensions["warehouse_count"],
            supplier_count=dimensions["supplier_count"],
            order_count=dimensions["order_count"],
        )


def _validated_bundle(
    root: Path, **dimensions: Any
) -> tuple[dict[str, Any], ArtifactBundle, dict[str, ValidationResult]]:
    manifest = generate(root, seed=20250301, as_of="2025-03-07T18:00:00Z", **dimensions)
    bundle = read_bundle(root)
    results = validate_bundle_rows(
        bundle.rows,
        headers=bundle.headers,
        manifest_artifacts=set(manifest["artifacts"]),
    )
    return manifest, bundle, results


def test_default_bundle_has_no_validation_rejections(tmp_path: Path) -> None:
    _, _, results = _validated_bundle(tmp_path)

    assert not [rejection for result in results.values() for rejection in result.rejections]


def test_default_bundle_observed_facts_are_before_as_of_and_po_000026_is_ordered(
    tmp_path: Path,
) -> None:
    as_of = datetime.fromisoformat("2025-03-07T18:00:00+00:00")
    _, bundle, _ = _validated_bundle(tmp_path)
    orders = {row["source_order_id"]: row for row in bundle.rows["oms/orders.csv"]}
    purchase_orders = {
        row["source_purchase_order_id"]: row for row in bundle.rows["erp/purchase_orders.csv"]
    }
    shipments = bundle.rows["carrier/shipments.csv"]

    for row in orders.values():
        ordered_at = datetime.fromisoformat(row["ordered_at"])
        fulfilled_at = row["fulfilled_at"]
        if fulfilled_at:
            fulfilled_at = datetime.fromisoformat(fulfilled_at)
            assert ordered_at <= fulfilled_at <= as_of
    for row in purchase_orders.values():
        ordered_at = datetime.fromisoformat(row["ordered_at"])
        expected = datetime.fromisoformat(row["expected_delivery_at"])
        assert ordered_at <= expected <= as_of
        received_at = row["received_at"]
        if received_at:
            received_at = datetime.fromisoformat(received_at)
            assert expected <= received_at <= as_of
    for row in shipments:
        shipped_at = datetime.fromisoformat(row["shipped_at"]) if row["shipped_at"] else None
        delivered_at = datetime.fromisoformat(row["delivered_at"]) if row["delivered_at"] else None
        if delivered_at:
            assert shipped_at is not None
            assert shipped_at <= delivered_at <= as_of
            fulfilled_at = orders[row["source_order_id"]]["fulfilled_at"]
            assert fulfilled_at
            assert delivered_at >= datetime.fromisoformat(fulfilled_at)

    po = purchase_orders["PO000026"]
    assert datetime.fromisoformat(po["ordered_at"]) <= datetime.fromisoformat(
        po["expected_delivery_at"]
    )
    assert datetime.fromisoformat(po["expected_delivery_at"]) <= datetime.fromisoformat(
        po["received_at"]
    )


def test_small_override_bundle_has_no_rejections_or_orphan_references(tmp_path: Path) -> None:
    _, bundle, results = _validated_bundle(
        tmp_path,
        product_count=4,
        warehouse_count=1,
        supplier_count=2,
        order_count=6,
    )

    rejections = [rejection for result in results.values() for rejection in result.rejections]
    assert not rejections
    assert all(rejection.error_code != "PARENT_SOURCE_ID" for rejection in rejections)
    assert {row["source_product_id"] for row in bundle.rows["wms/inventory_movements.csv"]} <= {
        f"P{number:04d}" for number in range(1, 5)
    }


def test_supplier_delay_and_shipment_facts_remain_coherent(tmp_path: Path) -> None:
    as_of = datetime.fromisoformat("2025-03-07T18:00:00+00:00")
    _, bundle, results = _validated_bundle(tmp_path)
    assert not [rejection for result in results.values() for rejection in result.rejections]

    po = next(
        row
        for row in bundle.rows["erp/purchase_orders.csv"]
        if row["source_purchase_order_id"] == "PO000001"
    )
    assert po["status"] == "OPEN"
    assert datetime.fromisoformat(po["expected_delivery_at"]) < as_of
    assert not po["received_at"]

    orders = {row["source_order_id"]: row for row in bundle.rows["oms/orders.csv"]}
    for shipment in bundle.rows["carrier/shipments.csv"]:
        if shipment["delivered_at"]:
            delivered_at = datetime.fromisoformat(shipment["delivered_at"])
            fulfilled_at = datetime.fromisoformat(
                orders[shipment["source_order_id"]]["fulfilled_at"]
            )
            assert fulfilled_at <= delivered_at <= as_of


def test_default_bundle_has_bounded_open_cohort_and_commitment_mix(tmp_path: Path) -> None:
    as_of = datetime.fromisoformat("2025-03-07T18:00:00+00:00")
    _, bundle, _ = _validated_bundle(tmp_path)
    orders = bundle.rows["oms/orders.csv"]
    open_orders = [row for row in orders if row["status"] == "OPEN"]
    generic_open_orders = [
        row
        for row in open_orders
        if row["source_order_id"] not in {"O000001", "O000002", "O000003"}
    ]

    assert len(open_orders) / len(orders) <= 0.20
    assert {row["source_order_id"] for row in open_orders} >= {"O000001", "O000002", "O000003"}
    assert sum(datetime.fromisoformat(row["promised_at"]) >= as_of for row in open_orders) >= 20
    assert sum(datetime.fromisoformat(row["promised_at"]) < as_of for row in open_orders) >= 20
    assert (
        sum(
            as_of <= datetime.fromisoformat(row["promised_at"]) <= as_of + timedelta(hours=48)
            for row in open_orders
        )
        >= 10
    )
    assert min(
        as_of - datetime.fromisoformat(row["ordered_at"]) for row in generic_open_orders
    ) <= timedelta(days=3)


def test_default_bundle_has_broad_shipments_but_few_overdue_in_transit(tmp_path: Path) -> None:
    as_of = datetime.fromisoformat("2025-03-07T18:00:00+00:00")
    _, bundle, _ = _validated_bundle(tmp_path)
    shipments = bundle.rows["carrier/shipments.csv"]
    in_transit = [row for row in shipments if row["status"] == "IN_TRANSIT"]
    overdue = [row for row in in_transit if datetime.fromisoformat(row["eta"]) < as_of]

    assert len(shipments) > 500
    assert {row["source_shipment_id"] for row in overdue} >= {"SHP000004"}
    assert len(overdue) >= 2
    assert len(overdue) <= max(3, len(in_transit) // 10)
    assert all(
        row["source_order_id"] != "O000004" or row["source_shipment_id"] == "SHP000004"
        for row in overdue
    )


def test_default_bundle_retains_all_six_scenario_source_anchors(tmp_path: Path) -> None:
    manifest, bundle, _ = _validated_bundle(tmp_path)
    scenario_sources = {
        scenario["scenario_id"]: scenario["source_id"] for scenario in manifest["scenarios"]
    }
    orders = {row["source_order_id"]: row for row in bundle.rows["oms/orders.csv"]}
    inventory = {
        (row["source_product_id"], row["source_warehouse_id"]): row
        for row in bundle.rows["wms/inventory.csv"]
    }
    movements = bundle.rows["wms/inventory_movements.csv"]
    purchase_orders = {
        row["source_purchase_order_id"]: row for row in bundle.rows["erp/purchase_orders.csv"]
    }
    shipments = {row["source_shipment_id"]: row for row in bundle.rows["carrier/shipments.csv"]}

    assert scenario_sources == {
        "SLA_BREACH_RISK": "O000001",
        "INVENTORY_SHORTAGE": "O000002",
        "STOCKOUT_RISK": "O000003",
        "INVENTORY_MISMATCH": "P0003-W001",
        "SUPPLIER_DELAY": "PO000001",
        "SHIPMENT_DELAY": "SHP000004",
    }
    assert orders["O000001"]["status"] == "OPEN"
    assert orders["O000001"]["promised_at"] < manifest["as_of"]
    assert orders["O000002"]["status"] == "OPEN"
    assert orders["O000003"]["status"] == "OPEN"
    assert inventory[("P0001", "W001")]["on_hand"] == "5.000"
    assert inventory[("P0002", "W001")]["on_hand"] == "100.000"
    assert any(
        row["source_product_id"] == "P0003"
        and row["source_warehouse_id"] == "W001"
        and row["reference_type"] == "MISMATCH_FIXTURE"
        for row in movements
    )
    assert purchase_orders["PO000001"]["status"] == "OPEN"
    assert shipments["SHP000004"]["status"] == "IN_TRANSIT"
    assert shipments["SHP000004"]["eta"] < manifest["as_of"]
