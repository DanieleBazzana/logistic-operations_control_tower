import csv
import hashlib
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from control_tower.external_validation import runner as runner_module
from control_tower.external_validation.acquisition import (
    load_manifest,
    read_local_tables,
    verify_declared_files,
)
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
    assert dataco["source_version"] == "5"
    assert dataco["acquisition"]["encoding"] == "latin-1"
    assert dataco["verification"]["sha256"] == (
        "fa6d022ed437155e1a2f0378710602848703c8a7f203f7ff5d77805bf8480aa6"
    )
    assert any("synthetic" in caveat for caveat in dataco["caveats"])
    assert olist["raw_data_policy"] == "never_commit"
    assert olist["source_version"] == "2"
    assert olist["acquisition"]["download_status"] == "verified_local_file_outside_repository"
    assert olist["verification"]["encoding"] == "utf-8"
    assert set(olist["verification"]["files"]) == {
        "olist_customers_dataset.csv",
        "olist_geolocation_dataset.csv",
        "olist_order_items_dataset.csv",
        "olist_order_payments_dataset.csv",
        "olist_order_reviews_dataset.csv",
        "olist_orders_dataset.csv",
        "olist_products_dataset.csv",
        "olist_sellers_dataset.csv",
        "product_category_name_translation.csv",
    }


def _write_verified_olist_fixture(tmp_path):
    files = {
        "olist_customers_dataset.csv": "id,name\n1,Ana\n",
        "olist_geolocation_dataset.csv": "zip,lat\n1,0\n",
        "olist_order_items_dataset.csv": "id,item\n1,book\n",
        "olist_order_payments_dataset.csv": "id,value\n1,10\n",
        "olist_order_reviews_dataset.csv": 'id,comment\n1,"line one\nline two"\n',
        "olist_orders_dataset.csv": "id,status\n1,delivered\n",
        "olist_products_dataset.csv": "id,title\n1,Book\n",
        "olist_sellers_dataset.csv": "id,state\n1,SP\n",
        "product_category_name_translation.csv": "name,en\nlivros,books\n",
    }
    metadata = {}
    for filename, content in files.items():
        path = tmp_path / filename
        path.write_text(content, encoding="utf-8", newline="")
        metadata[filename] = {
            "filename": filename,
            "size_bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "row_count": sum(1 for _ in csv.reader(content.splitlines(keepends=True))) - 1,
        }
    archive = tmp_path / "olist.zip"
    archive.write_bytes(b"archive bytes")
    archive_metadata = {
        "filename": archive.name,
        "size_bytes": archive.stat().st_size,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    verification = {
        "status": "verified_local_file_outside_repository",
        "encoding": "utf-8",
        "archive": archive_metadata,
        "files": metadata,
    }
    return files, verification


def test_olist_per_file_verification_checks_archive_and_logical_csv_rows(tmp_path) -> None:
    files, verification = _write_verified_olist_fixture(tmp_path)

    computed = verify_declared_files(tmp_path, files, verification)

    assert computed["archive"] == verification["archive"]
    assert computed["files"]["olist_order_reviews_dataset.csv"]["row_count"] == 1
    assert computed["files"] == verification["files"]


@pytest.mark.parametrize("change", ["size_bytes", "sha256", "row_count"])
def test_olist_per_file_verification_rejects_declared_mismatch(tmp_path, change) -> None:
    files, verification = _write_verified_olist_fixture(tmp_path)
    verification["files"]["olist_orders_dataset.csv"][change] = (
        verification["files"]["olist_orders_dataset.csv"][change] + 1
        if change != "sha256"
        else "0" * 64
    )

    with pytest.raises(ValueError, match="olist_orders_dataset.csv"):
        verify_declared_files(tmp_path, files, verification)


def test_olist_per_file_verification_rejects_missing_or_invalid_utf8(tmp_path) -> None:
    files, verification = _write_verified_olist_fixture(tmp_path)
    (tmp_path / "olist_products_dataset.csv").unlink()
    with pytest.raises(FileNotFoundError, match="olist_products_dataset.csv"):
        verify_declared_files(tmp_path, files, verification)

    files, verification = _write_verified_olist_fixture(tmp_path)
    invalid = tmp_path / "olist_products_dataset.csv"
    invalid.write_bytes(b"id,title\n1,\xff\n")
    verification["files"][invalid.name]["size_bytes"] = invalid.stat().st_size
    verification["files"][invalid.name]["sha256"] = hashlib.sha256(invalid.read_bytes()).hexdigest()
    with pytest.raises(UnicodeDecodeError):
        verify_declared_files(tmp_path, files, verification)


def test_olist_per_file_verification_rejects_traversal_and_nonregular_archive(tmp_path) -> None:
    files, verification = _write_verified_olist_fixture(tmp_path)
    verification["archive"]["filename"] = "../olist.zip"
    with pytest.raises(ValueError):
        verify_declared_files(tmp_path, files, verification)

    files, verification = _write_verified_olist_fixture(tmp_path)
    archive = tmp_path / "olist.zip"
    archive.unlink()
    archive.mkdir()
    with pytest.raises(ValueError, match="regular file"):
        verify_declared_files(tmp_path, files, verification)


def test_runner_evidence_contains_verified_olist_manifest_provenance(tmp_path, monkeypatch) -> None:
    files, verification = _write_verified_olist_fixture(tmp_path)
    manifest = load_manifest("olist")
    manifest["verification"] = verification
    monkeypatch.setattr(runner_module, "load_manifest", lambda dataset: manifest)

    evidence = run_validation(
        "olist",
        tmp_path,
        as_of=datetime(2018, 1, 2, tzinfo=timezone.utc),
        sample_size=1,
    )

    provenance = evidence["manifest_provenance"]
    assert provenance["source_version"] == "2"
    assert provenance["license"] == "CC BY-NC-SA 4.0"
    assert provenance["status"] == "verified_local_file_outside_repository"
    assert provenance["archive"]["sha256"] == verification["archive"]["sha256"]
    assert provenance["files"]["olist_orders_dataset.csv"]["row_count"] == 1


def test_dataco_scalar_verification_still_returns_no_computed_provenance(tmp_path) -> None:
    path = tmp_path / "DataCoSupplyChainDataset.csv"
    path.write_bytes(b"orders\n1\n")
    verification = {
        "status": "verified_local_file_outside_repository",
        "filename": path.name,
        "encoding": "latin-1",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }

    assert verify_declared_files(tmp_path, {"orders": path.name}, verification) is None


def test_normalization_is_deterministic_and_sampling_is_bounded() -> None:
    row = {" Order ID ": " O-1 ", "Order-Date": "2025-01-01"}

    assert normalize_external_row(row) == {
        "order_id": "O-1",
        "order_date": "2025-01-01",
    }
    rows = ({"id": str(index)} for index in range(5))
    assert bounded_rows(rows, 2) == [{"id": "0"}, {"id": "1"}]


def test_bounded_rows_does_not_consume_after_requested_prefix() -> None:
    def rows():
        yield {"id": "0"}
        raise AssertionError("bounded sampling consumed a row beyond the prefix")

    assert bounded_rows(rows(), 1) == [{"id": "0"}]


def test_local_table_reader_defaults_to_utf8_and_accepts_manifest_encoding(tmp_path) -> None:
    utf8_path = tmp_path / "utf8.csv"
    utf8_path.write_text("name\nMünchen\n", encoding="utf-8")
    assert read_local_tables(tmp_path, {"rows": "utf8.csv"})["rows"][0]["name"] == "München"

    latin1_path = tmp_path / "latin1.csv"
    latin1_path.write_bytes("name\nSão Paulo\n".encode("latin-1"))
    assert (
        read_local_tables(tmp_path, {"rows": "latin1.csv"}, encoding="latin-1")["rows"][0]["name"]
        == "São Paulo"
    )


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


def test_dataco_required_unavailable_fields_remain_rejected() -> None:
    adapted = DataCoAdapter().adapt(
        {
            "orders": [
                {
                    "Order Id": "100",
                    "Order Status": "COMPLETE",
                    "Order Date (DateOrders)": "01/01/2018 10:30",
                    "Sales": "99.00",
                }
            ]
        }
    )
    result = validate_adapted(adapted)["oms/orders.csv"]
    required_fields = {
        rejection.field for rejection in result.rejections if rejection.error_code == "REQUIRED"
    }
    assert {"source_warehouse_id", "promised_at", "currency"} <= required_fields


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


def test_independent_kpi_parses_dataco_datetime_and_uses_order_granularity() -> None:
    kpis = calculate_independent_kpis(
        "dataco",
        {
            "orders": [
                {
                    "Order Id": "100",
                    "Order Status": "COMPLETE",
                    "Order Date (DateOrders)": "01/01/2018 10:30",
                    "Delivery Status": "Advance shipping",
                    "Sales": "50.00",
                },
                {
                    "Order Id": "100",
                    "Order Status": "COMPLETE",
                    "Order Date (DateOrders)": "01/01/2018 10:30",
                    "Delivery Status": "Advance shipping",
                    "Sales": "49.00",
                },
            ]
        },
        as_of=datetime(2018, 1, 1, 10, 0, tzinfo=timezone.utc),
    )

    assert kpis["orders_processed"] == 0
    assert kpis["fulfilled_orders"] == 0


def test_independent_kpi_sla_is_unavailable_without_promised_and_fulfilled_dates() -> None:
    kpis = calculate_independent_kpis(
        "dataco",
        {
            "orders": [
                {
                    "Order Id": "100",
                    "Order Status": "COMPLETE",
                    "Order Date (DateOrders)": "01/01/2018 10:30",
                    "Delivery Status": "Advance shipping",
                }
            ]
        },
        as_of=datetime(2018, 1, 2, tzinfo=timezone.utc),
    )

    assert kpis["fulfilled_orders"] == 0
    assert kpis["sla_performance_pct"] is None


def test_independent_kpi_accepts_observed_lowercase_dataco_date_header() -> None:
    kpis = calculate_independent_kpis(
        "dataco",
        {
            "orders": [
                {
                    "Order Id": "100",
                    "Order Status": "COMPLETE",
                    "order date (DateOrders)": "01/01/2018 10:30",
                    "order_delivered_customer_date": "01/01/2018 12:00",
                }
            ]
        },
        as_of=datetime(2018, 1, 2, tzinfo=timezone.utc),
    )

    assert kpis["orders_processed"] == 1
    assert kpis["fulfilled_orders"] == 1


def test_independent_kpi_excludes_missing_or_bad_order_dates() -> None:
    kpis = calculate_independent_kpis(
        "dataco",
        {
            "orders": [
                {
                    "Order Id": "missing",
                    "Order Status": "COMPLETE",
                    "Order Date (DateOrders)": "",
                },
                {
                    "Order Id": "bad",
                    "Order Status": "COMPLETE",
                    "Order Date (DateOrders)": "not-a-date",
                },
                {
                    "Order Id": "valid",
                    "Order Status": "COMPLETE",
                    "Order Date (DateOrders)": "01/01/2018",
                    "order_delivered_customer_date": "01/01/2018",
                },
            ]
        },
        as_of=datetime(2018, 1, 2, tzinfo=timezone.utc),
    )

    assert kpis["orders_processed"] == 1
    assert kpis["fulfilled_orders"] == 1


def test_independent_kpi_excludes_fulfillment_after_as_of() -> None:
    kpis = calculate_independent_kpis(
        "dataco",
        {
            "orders": [
                {
                    "Order Id": "100",
                    "Order Status": "COMPLETE",
                    "Order Date (DateOrders)": "01/01/2018",
                    "order_delivered_customer_date": "01/03/2018",
                }
            ]
        },
        as_of=datetime(2018, 1, 2, tzinfo=timezone.utc),
    )

    assert kpis["orders_processed"] == 1
    assert kpis["fulfilled_orders"] == 0
    assert kpis["sla_performance_pct"] is None


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
    assert evidence["counts"]["rejection_errors"] == 2
    assert evidence["counts"]["source_line_rows"] == 2
    assert evidence["artifacts"]["oms/orders.csv"]["source_line_rows"] == 2
    assert evidence["coherence"]["status"] == "NOT_COMPARABLE"
    assert "supplier" in evidence["unavailable_domains"]


def test_validation_evidence_distinguishes_order_counts_from_rejection_errors() -> None:
    adapted = DataCoAdapter().adapt(
        {
            "orders": [
                {
                    "Order Id": "100",
                    "Order Status": "COMPLETE",
                    "Order Date (DateOrders)": "01/01/2018",
                    "Sales": "99.00",
                }
            ]
        }
    )
    validated = validate_adapted(adapted)

    evidence = build_validation_evidence(
        dataset="dataco",
        role="secondary",
        sample_size=None,
        rows_read=adapted.rows_read,
        source_line_rows={"oms/orders.csv": 1},
        adapted_orders={"oms/orders.csv": 1},
        validated=validated,
        unavailable=[],
        kpis={"open_exceptions": None},
        queue_counts={},
    )

    assert evidence["counts"] == {
        "source_line_rows": 1,
        "adapted_orders": 1,
        "accepted_orders": 0,
        "rejected_orders": 1,
        "rejection_errors": 4,
        "duplicate_identical": 0,
    }
    assert evidence["artifacts"]["oms/orders.csv"] == {
        "source_line_rows": 1,
        "adapted_orders": 1,
        "accepted_orders": 0,
        "rejected_orders": 1,
        "rejection_errors": 4,
        "duplicate_identical": 0,
    }


def test_runner_writes_local_evidence_without_api_or_raw_data(tmp_path, monkeypatch) -> None:
    csv_path = tmp_path / "DataCoSupplyChainDataset.csv"
    csv_path.write_bytes(
        (
            "Order Id,Order Status,Order Date (DateOrders),Sales,Delivery Status,Order Region\n"
            "100,COMPLETE,1/1/2018,99.00,Advance shipping,São Paulo\n"
        ).encode("latin-1")
    )
    output_path = tmp_path / "evidence.json"
    manifest = load_manifest("dataco")
    manifest["verification"] = {
        "status": "not_verified",
        "filename": "DataCoSupplyChainDataset.csv",
        "encoding": "latin-1",
        "sha256": None,
    }
    monkeypatch.setattr(runner_module, "load_manifest", lambda dataset: manifest)

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
    assert evidence["manifest_provenance"] == {
        "source_version": "5",
        "filename": "DataCoSupplyChainDataset.csv",
        "encoding": "latin-1",
        "sha256": None,
        "status": "not_verified",
    }
    assert output_path.read_text(encoding="utf-8").endswith("\n")


def test_runner_rejects_declared_checksum_mismatch_before_parsing(tmp_path) -> None:
    (tmp_path / "DataCoSupplyChainDataset.csv").write_bytes(b"not-the-committed-dataset")

    try:
        run_validation(
            "dataco",
            tmp_path,
            as_of=datetime(2018, 1, 2, tzinfo=timezone.utc),
        )
    except ValueError as error:
        assert "SHA-256 mismatch" in str(error)
    else:
        raise AssertionError("declared checksum mismatch was not rejected")


def test_runner_rejects_missing_declared_checksum_file_before_parsing(tmp_path) -> None:
    try:
        run_validation(
            "dataco",
            tmp_path,
            as_of=datetime(2018, 1, 2, tzinfo=timezone.utc),
        )
    except FileNotFoundError as error:
        assert "declared file is missing" in str(error)
    else:
        raise AssertionError("missing declared file was not rejected")
