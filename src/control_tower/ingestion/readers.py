"""UTF-8 CSV and JSON readers for an ingestion bundle."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from control_tower.ingestion.contracts import ArtifactBundle
from control_tower.synthetic.artifacts import ARTIFACT_COLUMNS, manifest_identity


def read_csv(path: str | Path) -> list[dict[str, str]]:
    """Read a UTF-8 CSV while preserving row order and blank values."""

    rows, _ = _read_csv_with_header(path)
    return rows


def _read_csv_with_header(path: str | Path) -> tuple[list[dict[str, str]], tuple[str, ...] | None]:
    with Path(path).open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        header = tuple(reader.fieldnames) if reader.fieldnames is not None else None
        return list(reader), header


def read_json(path: str | Path) -> dict[str, Any]:
    """Read a UTF-8 JSON object."""

    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("manifest must contain a JSON object")
    return value


def read_bundle(root: str | Path) -> ArtifactBundle:
    """Read and identity-check a generated artifact directory."""

    directory = Path(root).resolve()
    manifest = read_json(directory / "manifest.json")
    expected = manifest.get("manifest_identity")
    identity_payload = {key: value for key, value in manifest.items() if key != "manifest_identity"}
    if expected != manifest_identity(identity_payload):
        raise ValueError("manifest identity does not match manifest contents")
    rows: dict[str, list[dict[str, str]]] = {}
    headers: dict[str, tuple[str, ...] | None] = {}
    artifacts = manifest.get("artifacts", {})
    if not isinstance(artifacts, dict):
        return ArtifactBundle(root=directory, manifest=manifest, rows=rows, headers=headers)
    for artifact_name in artifacts:
        if not isinstance(artifact_name, str):
            continue
        artifact_path = Path(artifact_name)
        if artifact_path.is_absolute() or ".." in artifact_path.parts:
            raise ValueError(f"unsupported artifact path: {artifact_name}")
        if artifact_name not in ARTIFACT_COLUMNS:
            continue
        path = directory / artifact_path
        resolved_path = path.resolve(strict=False)
        try:
            resolved_path.relative_to(directory)
        except ValueError as error:
            raise ValueError(f"artifact path is outside bundle root: {artifact_name}") from error
        if not path.is_file():
            continue
        rows[artifact_name], headers[artifact_name] = _read_csv_with_header(path)
    return ArtifactBundle(root=directory, manifest=manifest, rows=rows, headers=headers)


__all__ = ["read_bundle", "read_csv", "read_json"]
