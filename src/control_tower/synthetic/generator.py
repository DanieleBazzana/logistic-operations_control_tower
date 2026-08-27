"""Deterministic OMS/WMS/ERP/carrier fixture generation."""

from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from control_tower.config import Settings
from control_tower.synthetic.anomalies import scenario_manifest
from control_tower.synthetic.artifacts import ARTIFACT_COLUMNS, write_artifact_rows, write_manifest

DEFAULT_OUTPUT_DIR = Path("data/generated")
SCHEMA_VERSION = "m02.v1"


def _utc(value: datetime | str | None, default: datetime) -> datetime:
    if value is None:
        value = default
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("as_of must include a timezone")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):.2f}"


def _quantity(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.001')):.3f}"


def _row_count(manifest: dict[str, Any], name: str) -> int:
    return manifest["artifacts"][name]["row_count"]


def generate(
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    *,
    seed: int | None = None,
    as_of: datetime | str | None = None,
    settings: Settings | None = None,
    product_count: int = 200,
    warehouse_count: int = 3,
    supplier_count: int = 10,
    order_count: int = 1200,
) -> dict[str, Any]:
    """Generate a deterministic fixture and return its manifest.

    The generator is intentionally local-only: all randomness uses one local
    ``Random`` instance, and no domain value depends on the wall clock.
    """

    configured = settings or Settings()
    actual_seed = configured.deterministic_seed if seed is None else seed
    actual_as_of = _utc(as_of, configured.as_of)
    minimums = {
        "product_count": 3,
        "warehouse_count": 1,
        "supplier_count": 1,
        "order_count": 4,
    }
    dimensions = {
        "product_count": product_count,
        "warehouse_count": warehouse_count,
        "supplier_count": supplier_count,
        "order_count": order_count,
    }
    if any(dimensions[name] < minimum for name, minimum in minimums.items()):
        requirements = ", ".join(f"{name}>={minimum}" for name, minimum in minimums.items())
        raise ValueError(f"fixture dimensions must satisfy scenario references: {requirements}")
    rng = random.Random(actual_seed)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    products: list[dict[str, Any]] = []
    for number in range(1, product_count + 1):
        price = Decimal(rng.randint(500, 25000)) / Decimal(100)
        products.append(
            {
                "source_product_id": f"P{number:04d}",
                "sku": f"SKU-{number:04d}",
                "name": f"Product {number:04d}",
                "description": f"Synthetic product {number:04d}",
                "unit_price": _money(price),
                "active": "true",
            }
        )

    warehouses: list[dict[str, Any]] = []
    for number in range(1, warehouse_count + 1):
        warehouses.append(
            {
                "source_warehouse_id": f"W{number:03d}",
                "code": f"WH-{number:03d}",
                "name": f"Warehouse {number:03d}",
                "region": ["NORTH", "SOUTH", "WEST"][number - 1]
                if number <= 3
                else f"REGION-{number:03d}",
                "timezone": "UTC",
            }
        )

    suppliers: list[dict[str, Any]] = []
    for number in range(1, supplier_count + 1):
        suppliers.append(
            {
                "source_supplier_id": f"SUP{number:03d}",
                "code": f"SUP-{number:03d}",
                "name": f"Supplier {number:03d}",
                "region": "GLOBAL",
                "active": "true",
            }
        )

    inventory: list[dict[str, Any]] = []
    movements: list[dict[str, Any]] = []
    for warehouse_number in range(1, warehouse_count + 1):
        for product_number in range(1, product_count + 1):
            if warehouse_number == 1 and product_number == 1:
                on_hand, reserved = Decimal("5"), Decimal("0")
            elif warehouse_number == 1 and product_number == 2:
                on_hand, reserved = Decimal("100"), Decimal("0")
            elif warehouse_number == 1 and product_number == 3:
                on_hand, reserved = Decimal("150"), Decimal("0")
            else:
                on_hand = Decimal(rng.randint(80, 320))
                reserved = Decimal(rng.randint(0, min(20, int(on_hand))))
            product_id = f"P{product_number:04d}"
            warehouse_id = f"W{warehouse_number:03d}"
            inventory.append(
                {
                    "source_product_id": product_id,
                    "source_warehouse_id": warehouse_id,
                    "on_hand": _quantity(on_hand),
                    "reserved": _quantity(reserved),
                    "observed_at": _timestamp(actual_as_of - timedelta(hours=2)),
                }
            )
            movement_number = len(movements) + 1
            movements.append(
                {
                    "source_movement_id": f"MOV{movement_number:06d}",
                    "source_product_id": product_id,
                    "source_warehouse_id": warehouse_id,
                    "movement_type": "RECEIPT",
                    "quantity": _quantity(on_hand),
                    "occurred_at": _timestamp(actual_as_of - timedelta(days=30, hours=1)),
                    "reference_type": "SNAPSHOT",
                    "reference_id": f"{product_id}-{warehouse_id}",
                }
            )
            movement_number += 1
            if reserved > 0:
                movement_type, movement_quantity = "RESERVATION", reserved
            else:
                movement_type, movement_quantity = "ADJUSTMENT_IN", Decimal("0.001")
            movements.append(
                {
                    "source_movement_id": f"MOV{movement_number:06d}",
                    "source_product_id": product_id,
                    "source_warehouse_id": warehouse_id,
                    "movement_type": movement_type,
                    "quantity": _quantity(movement_quantity),
                    "occurred_at": _timestamp(actual_as_of - timedelta(days=10)),
                    "reference_type": "SNAPSHOT",
                    "reference_id": f"{product_id}-{warehouse_id}",
                }
            )
    # This one movement makes the snapshot/movement reconstruction intentionally differ.
    movements.append(
        {
            "source_movement_id": f"MOV{len(movements) + 1:06d}",
            "source_product_id": "P0003",
            "source_warehouse_id": "W001",
            "movement_type": "ADJUSTMENT_IN",
            "quantity": "25.000",
            "occurred_at": _timestamp(actual_as_of - timedelta(days=1)),
            "reference_type": "MISMATCH_FIXTURE",
            "reference_id": "P0003-W001",
        }
    )

    orders: list[dict[str, Any]] = []
    order_items: list[dict[str, Any]] = []
    for number in range(1, order_count + 1):
        source_order_id = f"O{number:06d}"
        warehouse_id = f"W{(number % warehouse_count) + 1:03d}"
        status = (
            "OPEN"
            if number <= max(10, order_count // 4)
            else ("FULFILLED" if number % 5 else "OPEN")
        )
        if number == 1:
            warehouse_id, ordered_at, promised_at = (
                "W001",
                actual_as_of - timedelta(days=4),
                actual_as_of - timedelta(days=2),
            )
        elif number == 2:
            warehouse_id, ordered_at, promised_at = (
                "W001",
                actual_as_of - timedelta(days=1),
                actual_as_of + timedelta(days=2),
            )
        elif number == 3:
            warehouse_id, ordered_at, promised_at = (
                "W001",
                actual_as_of - timedelta(days=1),
                actual_as_of + timedelta(days=2),
            )
        else:
            ordered_at = actual_as_of - timedelta(days=rng.randint(1, 45), hours=rng.randint(0, 20))
            promised_at = ordered_at + timedelta(days=rng.randint(2, 8))
        fulfilled_at = None
        if status == "FULFILLED":
            fulfilled_at = promised_at - timedelta(hours=rng.randint(0, 48))
            if fulfilled_at < ordered_at:
                fulfilled_at = ordered_at + timedelta(hours=2)
        item_count = 1 if number <= 3 else rng.randint(1, 4)
        item_values: list[tuple[int, Decimal, Decimal]] = []
        for line_number in range(1, item_count + 1):
            product_number = ((number * 7 + line_number * 11) % product_count) + 1
            quantity = Decimal(rng.randint(1, 12))
            if number == 1 and line_number == 1:
                product_number, quantity = 1, Decimal("50")
            elif number == 2 and line_number == 1:
                product_number, quantity = 1, Decimal("20")
            elif number == 3 and line_number == 1:
                product_number, quantity = 2, Decimal("90")
            price = Decimal(products[product_number - 1]["unit_price"])
            item_values.append((product_number, quantity, price))
            order_items.append(
                {
                    "source_order_item_id": f"{source_order_id}-{line_number:02d}",
                    "source_order_id": source_order_id,
                    "source_product_id": f"P{product_number:04d}",
                    "line_number": str(line_number),
                    "ordered_quantity": _quantity(quantity),
                    "fulfilled_quantity": _quantity(
                        quantity if status == "FULFILLED" else Decimal("0")
                    ),
                    "unit_price": _money(price),
                }
            )
        total = sum((quantity * price for _, quantity, price in item_values), Decimal("0"))
        orders.append(
            {
                "source_order_id": source_order_id,
                "order_number": f"ORD-{number:06d}",
                "status": status,
                "region": "NORTH" if number % 2 else "SOUTH",
                "source_warehouse_id": warehouse_id,
                "ordered_at": _timestamp(ordered_at),
                "promised_at": _timestamp(promised_at),
                "fulfilled_at": _timestamp(fulfilled_at) if fulfilled_at else None,
                "total_amount": _money(total),
                "currency": "USD",
            }
        )

    purchase_orders: list[dict[str, Any]] = []
    purchase_order_items: list[dict[str, Any]] = []
    po_count = max(30, supplier_count * 4)
    for number in range(1, po_count + 1):
        source_po_id = f"PO{number:06d}"
        ordered_at = actual_as_of - timedelta(days=rng.randint(5, 60))
        status = "RECEIVED" if number % 4 else "PARTIALLY_RECEIVED"
        expected = ordered_at + timedelta(days=rng.randint(3, 20))
        received = expected + timedelta(days=1) if status == "RECEIVED" else None
        if number == 1:
            status, expected, received = "OPEN", actual_as_of - timedelta(days=3), None
        purchase_orders.append(
            {
                "source_purchase_order_id": source_po_id,
                "po_number": f"PO-{number:06d}",
                "source_supplier_id": f"SUP{((number - 1) % supplier_count) + 1:03d}",
                "source_warehouse_id": f"W{((number - 1) % warehouse_count) + 1:03d}",
                "status": status,
                "ordered_at": _timestamp(ordered_at),
                "expected_delivery_at": _timestamp(expected),
                "received_at": _timestamp(received) if received else None,
            }
        )
        item_count = rng.randint(1, 3)
        for line_number in range(1, item_count + 1):
            product_number = ((number * 13 + line_number) % product_count) + 1
            quantity = Decimal(rng.randint(20, 150))
            received_quantity = (
                quantity if status == "RECEIVED" else (quantity / 2).quantize(Decimal("0.001"))
            )
            if number == 1:
                received_quantity = Decimal("0")
            purchase_order_items.append(
                {
                    "source_purchase_order_item_id": f"{source_po_id}-{line_number:02d}",
                    "source_purchase_order_id": source_po_id,
                    "source_product_id": f"P{product_number:04d}",
                    "ordered_quantity": _quantity(quantity),
                    "received_quantity": _quantity(received_quantity),
                    "unit_cost": _money(
                        Decimal(products[product_number - 1]["unit_price"]) * Decimal("0.65")
                    ),
                }
            )

    shipments: list[dict[str, Any]] = []
    carriers = ("DHL", "FEDEX", "UPS", "USPS")
    for number, order in enumerate(orders, start=1):
        if number % 7 == 0 or number <= 5:
            if number == 4:
                shipped_at, eta, status, delivered_at = (
                    actual_as_of - timedelta(days=6),
                    actual_as_of - timedelta(days=2),
                    "IN_TRANSIT",
                    None,
                )
            else:
                shipped_at = datetime.fromisoformat(order["ordered_at"]) + timedelta(days=1)
                eta = datetime.fromisoformat(order["promised_at"])
                status = "DELIVERED" if order["status"] == "FULFILLED" else "IN_TRANSIT"
                delivered_at = eta if status == "DELIVERED" else None
            shipments.append(
                {
                    "source_shipment_id": f"SHP{len(shipments) + 1:06d}",
                    "source_order_id": order["source_order_id"],
                    "carrier": carriers[number % len(carriers)],
                    "tracking_id": f"TRK{len(shipments) + 1:06d}",
                    "status": status,
                    "shipped_at": _timestamp(shipped_at),
                    "eta": _timestamp(eta),
                    "delivered_at": _timestamp(delivered_at) if delivered_at else None,
                }
            )

    source_ids = {
        "SLA_BREACH_RISK": "O000001",
        "INVENTORY_SHORTAGE": "O000002",
        "STOCKOUT_RISK": "O000003",
        "INVENTORY_MISMATCH": "P0003-W001",
        "SUPPLIER_DELAY": "PO000001",
        "SHIPMENT_DELAY": "SHP000004",
    }
    datasets = {
        "oms/products.csv": products,
        "oms/orders.csv": orders,
        "oms/order_items.csv": order_items,
        "wms/warehouses.csv": warehouses,
        "wms/inventory.csv": inventory,
        "wms/inventory_movements.csv": movements,
        "erp/suppliers.csv": suppliers,
        "erp/purchase_orders.csv": purchase_orders,
        "erp/purchase_order_items.csv": purchase_order_items,
        "carrier/shipments.csv": shipments,
    }
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "seed": actual_seed,
        "as_of": _timestamp(actual_as_of),
        "scenarios": scenario_manifest(source_ids),
        "artifacts": {},
    }
    for name in ARTIFACT_COLUMNS:
        manifest["artifacts"][name] = {
            "row_count": write_artifact_rows(output, name, datasets[name])
        }
    manifest["manifest_identity"] = __import__(
        "control_tower.synthetic.artifacts", fromlist=["manifest_identity"]
    ).manifest_identity(manifest)
    write_manifest(output, manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic M02 source artifacts")
    parser.add_argument("generate", nargs="?", help="compatibility subcommand")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--seed", type=int)
    parser.add_argument("--as-of")
    args = parser.parse_args(argv)
    manifest = generate(args.output_dir, seed=args.seed, as_of=args.as_of)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
