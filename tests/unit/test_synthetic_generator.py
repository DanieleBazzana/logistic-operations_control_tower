from pathlib import Path

import pytest

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


def test_generation_manifest_has_target_volumes_and_six_source_scenarios(tmp_path: Path) -> None:
    manifest = generate(tmp_path, seed=78, as_of="2025-01-15T12:00:00+00:00")

    assert manifest["artifacts"]["oms/products.csv"]["row_count"] == 200
    assert manifest["artifacts"]["wms/warehouses.csv"]["row_count"] == 3
    assert manifest["artifacts"]["erp/suppliers.csv"]["row_count"] == 10
    assert manifest["artifacts"]["oms/orders.csv"]["row_count"] == 1200
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
