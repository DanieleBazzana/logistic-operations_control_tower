from control_tower.ingestion.validation import validate_rows


def _product(source_id: str = "P0001", name: str = "Product") -> dict[str, str]:
    return {
        "source_product_id": source_id,
        "sku": f"SKU-{source_id}",
        "name": name,
        "description": "",
        "unit_price": "1.00",
        "active": "true",
    }


def test_identical_source_duplicates_are_skipped_but_conflicts_are_rejected() -> None:
    identical = validate_rows("oms/products.csv", [_product(), _product()])
    conflicting = validate_rows("oms/products.csv", [_product(), _product(name="Different")])

    assert identical.accepted == 1
    assert identical.duplicate_identical == 1
    assert not identical.rejections
    assert conflicting.rejections[0].error_code == "DUPLICATE_CONFLICT"
