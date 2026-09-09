"""Metadata and safe local acquisition helpers; never downloads raw data."""

from __future__ import annotations

import csv
import hashlib
import json
import stat
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
) -> dict[str, Any] | None:
    """Verify declared bytes before any CSV parser consumes them."""

    if "files" in verification or "archive" in verification:
        if not verification.get("files") or not verification.get("archive"):
            raise ValueError("per-file verification requires archive and files metadata")
        encoding = str(verification.get("encoding", "utf-8"))
        if encoding.lower().replace("_", "-") != "utf-8":
            raise ValueError(f"per-file verification requires UTF-8 encoding, got {encoding}")
        archive = verification["archive"]
        archive_path = _local_path(root, str(archive["filename"]))
        _verify_bytes(archive_path, archive, str(archive["filename"]))

        computed_files: dict[str, dict[str, Any]] = {}
        for metadata in verification["files"].values():
            filename = str(metadata["filename"])
            path = _local_path(root, filename)
            _verify_bytes(path, metadata, filename)
            row_count = _csv_row_count(path, encoding)
            if row_count != int(metadata["row_count"]):
                raise ValueError(
                    f"CSV row-count mismatch for declared file {filename}: "
                    f"expected {metadata['row_count']}, got {row_count}"
                )
            computed_files[filename] = {
                "filename": filename,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
                "row_count": row_count,
            }
        return {
            "archive": {
                "filename": str(archive["filename"]),
                "size_bytes": archive_path.stat().st_size,
                "sha256": _sha256(archive_path),
            },
            "files": computed_files,
        }

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


def _verify_bytes(path: Path, metadata: Mapping[str, Any], label: str) -> None:
    try:
        mode = path.stat().st_mode
    except FileNotFoundError as error:
        raise FileNotFoundError(f"declared file is missing: {path}") from error
    if not stat.S_ISREG(mode):
        raise ValueError(f"declared file is not a regular file: {label}")
    expected_size = int(metadata["size_bytes"])
    actual_size = path.stat().st_size
    if actual_size != expected_size:
        raise ValueError(
            f"size mismatch for declared file {label}: expected {expected_size}, got {actual_size}"
        )
    expected = str(metadata["sha256"]).lower()
    actual = _sha256(path)
    if actual != expected:
        raise ValueError(
            f"SHA-256 mismatch for declared file {label}: expected {expected}, got {actual}"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _csv_row_count(path: Path, encoding: str) -> int:
    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        return sum(1 for _ in reader)


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
