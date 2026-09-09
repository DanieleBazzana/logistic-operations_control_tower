"""Metadata and safe local acquisition helpers; never downloads raw data."""

from __future__ import annotations

import csv
import json
from itertools import islice
from pathlib import Path
from typing import Any, Iterable

MANIFEST_DIR = Path(__file__).resolve().parents[3] / "docs" / "external-validation" / "manifests"


def load_manifest(dataset: str) -> dict[str, Any]:
    """Load a committed metadata manifest, not an external dataset."""

    path = MANIFEST_DIR / f"{dataset}.json"
    if dataset not in {"olist", "dataco"}:
        raise ValueError(f"unsupported dataset: {dataset}")
    return json.loads(path.read_text(encoding="utf-8"))


def read_local_tables(
    root: str | Path, files: dict[str, str], *, sample_size: int | None = None
) -> dict[str, list[dict[str, str]]]:
    """Read user-provided local CSVs with an optional deterministic row bound."""

    if sample_size is not None and sample_size < 0:
        raise ValueError("sample_size must be non-negative")
    base = Path(root).resolve()
    tables: dict[str, list[dict[str, str]]] = {}
    for table, relative_path in files.items():
        path = (base / relative_path).resolve()
        path.relative_to(base)
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            iterator: Iterable[dict[str, str]] = reader
            if sample_size is not None:
                iterator = islice(iterator, sample_size)
            tables[table] = list(iterator)
    return tables


__all__ = ["MANIFEST_DIR", "load_manifest", "read_local_tables"]
