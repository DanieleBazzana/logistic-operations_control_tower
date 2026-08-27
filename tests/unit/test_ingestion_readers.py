import json
from pathlib import Path

import pytest

from control_tower.ingestion.loader import ingest
from control_tower.ingestion.readers import read_bundle
from control_tower.synthetic.artifacts import manifest_identity


def _write_manifest(root: Path, artifacts: dict[str, dict[str, int]]) -> None:
    manifest = {
        "schema_version": "m02.v1",
        "seed": 1,
        "as_of": "2025-01-15T12:00:00+00:00",
        "scenarios": [],
        "artifacts": artifacts,
    }
    manifest["manifest_identity"] = manifest_identity(manifest)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize("artifact_name", ["/tmp/outside.csv", "../outside.csv"])
def test_read_bundle_rejects_manifest_artifact_path_escape(
    tmp_path: Path, artifact_name: str
) -> None:
    _write_manifest(tmp_path, {artifact_name: {"row_count": 0}})

    with pytest.raises(ValueError, match="unsupported artifact"):
        read_bundle(tmp_path)


def test_read_bundle_rejects_supported_artifact_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.csv"
    outside.write_text(
        "source_product_id,sku,name,description,unit_price,active\n", encoding="utf-8"
    )
    artifact = tmp_path / "oms" / "products.csv"
    artifact.parent.mkdir()
    artifact.symlink_to(outside)
    _write_manifest(tmp_path, {"oms/products.csv": {"row_count": 0}})

    with pytest.raises(ValueError, match="outside bundle root"):
        read_bundle(tmp_path)


def test_ingest_reports_unknown_manifest_artifact_without_loader_key_error(tmp_path: Path) -> None:
    _write_manifest(tmp_path, {"unknown.csv": {"row_count": 0}})

    result = ingest(tmp_path)

    assert not result.committed
    assert result.summaries[0].source == "unknown.csv"
    assert result.summaries[0].rejection_details[0].error_code == "UNKNOWN_ARTIFACT"