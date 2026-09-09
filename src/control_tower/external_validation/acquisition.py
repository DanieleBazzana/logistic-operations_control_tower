"""Metadata and safe local acquisition helpers; never downloads raw data."""

from __future__ import annotations

import csv
import hashlib
import json
from itertools import islice
from pathlib import Path
from typing import Any, Iterable, Mapping

MANIFEST_DIR = Path(__file__).resolve().parents[3] / "docs" / "external-validation" / "manifests"


def load_manifest(dataset: str) -> dict[str, Any]:
    """Load a committed metadata manifest, not an external dataset."""

    path = MANIFEST_DIR / f"{dataset}.json"
    if dataset not in {"olist", "dataco"}:
        raise ValueError(f"unsupported dataset: {dataset}")
    return json.loads(path.read_text(encoding="utf-8"))


def _local_path(root: str | Path, relative_path: str) -> Path:
    base = Path(root).resolve()
    path = (base / relative_path).resolve()
    path.relative_to(base)
    return path


def verify_declared_files(
    root: str | Path, files: Mapping[str, str], verification: Mapping[str, Any]
) -> None:
    """Verify declared bytes before any CSV parser consumes them."""

    if verification.get(
        "status"
    ) != "verified_local_file_outside_repository" or not verification.get("sha256"):
        return
    expected = str(verification["sha256"]).lower()
    for relative_path in files.values():
        path = _local_path(root, relative_path)
        if not path.is_file():
            raise FileNotFoundError(f"declared file is missing: {path}")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
        if actual != expected:
            raise ValueError(
                f"SHA-256 mismatch for declared file {relative_path}: "
                f"expected {expected}, got {actual}"
            )


def read_local_tables(
    root: str | Path,
    files: dict[str, str],
    *,
    sample_size: int | None = None,
    encoding: str = "utf-8",
) -> dict[str, list[dict[str, str]]]:
    """Read user-provided local CSVs with an optional bound and explicit encoding."""

    if sample_size is not None and sample_size < 0:
        raise ValueError("sample_size must be non-negative")
    tables: dict[str, list[dict[str, str]]] = {}
    for table, relative_path in files.items():
        path = _local_path(root, relative_path)
        with path.open(encoding=encoding, newline="") as handle:
            reader = csv.DictReader(handle)
            iterator: Iterable[dict[str, str]] = reader
            if sample_size is not None:
                iterator = islice(iterator, sample_size)
            tables[table] = list(iterator)
    return tables


__all__ = ["MANIFEST_DIR", "load_manifest", "read_local_tables", "verify_declared_files"]
