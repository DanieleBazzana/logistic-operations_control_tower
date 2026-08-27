from decimal import Decimal

from control_tower.ingestion.loader import _equal, _row_to_model
from control_tower.models import Product

_PRODUCT_ROW = {
    "source_product_id": "P0001",
    "sku": "SKU-P0001",
    "name": "Product 0001",
    "description": "Synthetic product",
    "unit_price": Decimal("12.50"),
    "active": True,
}


def test_product_source_id_stays_scalar_during_model_transformation() -> None:
    product = _row_to_model("oms/products.csv", _PRODUCT_ROW, {"products": {}})

    assert isinstance(product, Product)
    assert product.source_product_id == "P0001"
    assert product.sku == "SKU-P0001"


def test_product_source_id_compares_as_a_scalar_for_idempotency() -> None:
    product = Product(**_PRODUCT_ROW)

    assert _equal("oms/products.csv", _PRODUCT_ROW, product)
