from datetime import datetime, timezone
from decimal import Decimal

from control_tower.external_validation.acquisition import load_manifest
from control_tower.external_validation.adapters import (
    DataCoAdapter,
    OlistAdapter,
    bounded_rows,
    normalize_external_row,
)
from control_tower.external_validation.evidence import build_validation_evidence
from control_tower.external_validation.independent_kpi import calculate_independent_kpis
from control_tower.external_validation.runner import run_validation, write_evidence
from control_tower.external_validation.validation import validate_adapted
from control_tower.ingestion.validation import validate_rows


def test_manifests_are_metadata_only_and_preserve_license_caveats() -> None:
    olist = load_manifest("olist")
    dataco = load_manifest("dataco")

    assert olist["role"] == "primary"
    assert olist["canonical_url"].startswith("https://www.kaggle.com/")
    assert olist["license"] == "CC BY-NC-SA 4.0"
    assert dataco["role"] == "secondary"
    assert dataco["canonical_url"].startswith("https://data.mendeley.com/")
    assert dataco["license"] == "CC BY 4.0"
    assert any("synthetic" in caveat for caveat in dataco["caveats"])
    assert olist["raw_data_policy"] == "never_commit"


def test_normalization_is_deterministic_and_sampling_is_bounded() -> None:
    row = {" Order ID ": " O-1 ", "Order-Date": "2025-01-01"}

    assert normalize_external_row(row) == {
        "order_id": "O-1",
        "order_date": "2025-01-01",
    }
    rows = ({"id": str(index)} for index in range(5))
    assert bounded_rows(rows, 2) == [{"id": "0"}, {"id": "1"}]


def test_olist_adapter_maps_orders_and_marks_unavailable_operational_domains() -> None:
    result = OlistAdapter().adapt(
        {
            "orders": [
                {
                    "order_id": "o-1",
                    "customer_id": "c-1",
                    "order_status": "delivered",
                    "order_purchase_timestamp": "2018-01-01 10:00:00",
                    "order_estimated_delivery_date": "2018-01-10",
                    "order_delivered_customer_date": "2018-01-08 11:00:00",
                }
            ],
            "payments": [{"order_id": "o-1", "payment_value": "42.50"}],
            "customers": [{"customer_id": "c-1", "customer_state": "SP"}],
        }
    )

    order = result.rows_by_artifact["oms/orders.csv"][0]
    assert order["source_order_id"] == "o-1"
    assert order["status"] == "FULFILLED"
    assert order["total_amount"] == "42.50"
    assert order["currency"] is None
    assert order["source_warehouse_id"] is None
    assert any(item.output == "oms/orders.csv.currency" for item in result.provenance)
    assert {item.output_field for item in result.unavailable} >= {
        "source_warehouse_id",
        "currency",
    }
    assert {item.classification for item in result.mappings} == {
        "DIRECT",
        "DERIVED",
        "APPROXIMATE",
        "UNAVAILABLE",
    }


def test_dataco_adapter_keeps_secondary_provenance_and_does_not_fabricate_delivery() -> None:
    result = DataCoAdapter().adapt(
        {
            "orders": [
                {
                    "Order Id": "100",
                    "Order Status": "COMPLETE",
                    "Order Date (DateOrders)": "1/1/2018",
                    "Sales": "99.00",
                    "Delivery Status": "Advance shipping",
                }
            ]
        }
    )

    order = result.rows_by_artifact["oms/orders.csv"][0]
    assert result.role == "secondary"
    assert order["source_order_id"] == "100"
    assert order["status"] == "FULFILLED"
    assert order["promised_at"] is None
    assert order["fulfilled_at"] is None
    assert any("DataCo" in item.source for item in result.provenance)
    assert any(item.output_field == "promised_at" for item in result.unavailable)


def test_adapted_rows_use_existing_validator_for_nulls_and_conflicts() -> None:
    adapted = OlistAdapter().adapt(
        {
            "orders": [
                {
                    "order_id": "o-1",
                    "order_status": "delivered",
                    "order_purchase_timestamp": "2018-01-01T10:00:00+00:00",
                    "order_estimated_delivery_date": "2018-01-10T00:00:00+00:00",
                },
                {
                    "order_id": "o-1",
                    "order_status": "canceled",
                    "order_purchase_timestamp": "2018-01-01T10:00:00+00:00",
                    "order_estimated_delivery_date": "2018-01-10T00:00:00+00:00",
                },
            ]
        }
    )
    validated = validate_adapted(adapted)

    result = validated["oms/orders.csv"]
    assert result.accepted == 0
    assert any(rejection.error_code == "REQUIRED" for rejection in result.rejections)
    assert all(rejection.source_id == "o-1" for rejection in result.rejections)


def test_contract_validation_distinguishes_identical_and_conflicting_duplicates() -> None:
    row = {
        "source_order_id": "o-1",
        "order_number": "ord-1",
        "status": "OPEN",
        "region": "EU",
        "source_warehouse_id": "w-1",
        "ordered_at": "2018-01-01T00:00:00+00:00",
        "promised_at": "2018-01-10T00:00:00+00:00",
        "fulfilled_at": "",
        "total_amount": "10.00",
        "currency": "EUR",
    }
    conflict = {**row, "total_amount": "11.00"}

    result = validate_rows(
        "oms/orders.csv",
        [row, row, conflict],
        known_ids={"warehouses": {"w-1"}},
    )

    assert result.accepted == 1
    assert result.duplicate_identical == 1
    assert [rejection.error_code for rejection in result.rejections] == ["DUPLICATE_CONFLICT"]


def test_independent_kpi_calculation_is_deterministic_and_not_api_based() -> None:
    as_of = datetime(2018, 1, 31, tzinfo=timezone.utc)
    kpis = calculate_independent_kpis(
        "olist",
        {
            "orders": [
                {
                    "order_id": "o-1",
                    "order_status": "delivered",
                    "order_purchase_timestamp": "2018-01-01T10:00:00+00:00",
                    "order_estimated_delivery_date": "2018-01-10T00:00:00+00:00",
                    "order_delivered_customer_date": "2018-01-08T11:00:00+00:00",
                },
                {
                    "order_id": "o-2",
                    "order_status": "canceled",
                    "order_purchase_timestamp": "2018-01-02T10:00:00+00:00",
                },
            ]
        },
        as_of=as_of,
    )

    assert kpis["orders_processed"] == 2
    assert kpis["fulfilled_orders"] == 1
    assert kpis["cancelled_orders"] == 1
    assert kpis["sla_performance_pct"] == Decimal("100.00")
    assert kpis["open_exceptions"] is None
    assert kpis["formula_source"] == "external_validation.independent_kpi"


def test_validation_evidence_is_structured_and_explicit_about_non_comparability() -> None:
    evidence = build_validation_evidence(
        dataset="olist",
        role="primary",
        sample_size=10,
        rows_read={"orders": 2},
        validated={"oms/orders.csv": {"accepted": 0, "rejected": 2, "duplicate_identical": 0}},
        unavailable=["supplier", "inventory", "carrier", "revenue_at_risk"],
        kpis={"open_exceptions": None},
        queue_counts={},
    )

    assert evidence["dataset"] == "olist"
    assert evidence["sampling"]["max_rows"] == 10
    assert evidence["counts"]["rejected"] == 2
    assert evidence["coherence"]["status"] == "NOT_COMPARABLE"
    assert "supplier" in evidence["unavailable_domains"]


def test_runner_writes_local_evidence_without_api_or_raw_data(tmp_path) -> None:
    csv_path = tmp_path / "DataCoSupplyChainDataset.csv"
    csv_path.write_text(
        "Order Id,Order Status,Order Date (DateOrders),Sales,Delivery Status\n"
        "100,COMPLETE,1/1/2018,99.00,Advance shipping\n",
        encoding="utf-8",
    )
    output_path = tmp_path / "evidence.json"

    evidence = run_validation(
        "dataco",
        tmp_path,
        as_of=datetime(2018, 1, 2, tzinfo=timezone.utc),
        sample_size=1,
    )
    write_evidence(evidence, output_path)

    assert evidence["role"] == "secondary"
    assert evidence["api_called"] is False
    assert evidence["raw_data_committed"] is False
    assert evidence["sampling"]["max_rows"] == 1
    assert output_path.read_text(encoding="utf-8").endswith("\n")
