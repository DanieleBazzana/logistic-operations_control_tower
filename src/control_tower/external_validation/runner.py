"""Orchestrate one reproducible local external validation run."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from control_tower.external_validation.acquisition import load_manifest, read_local_tables
from control_tower.external_validation.adapters import DataCoAdapter, OlistAdapter
from control_tower.external_validation.evidence import build_validation_evidence
from control_tower.external_validation.independent_kpi import calculate_independent_kpis
from control_tower.external_validation.validation import validate_adapted


def run_validation(
    dataset: str,
    input_dir: str | Path,
    *,
    as_of: datetime,
    sample_size: int | None = None,
) -> dict[str, Any]:
    """Read only user-supplied CSVs and return JSON-compatible evidence."""

    manifest = load_manifest(dataset)
    tables = read_local_tables(
        input_dir,
        manifest["acquisition"]["files"],
        sample_size=sample_size,
        encoding=manifest["acquisition"].get("encoding", "utf-8"),
    )
    adapter = OlistAdapter() if dataset == "olist" else DataCoAdapter()
    adapted = adapter.adapt(tables)
    validated = validate_adapted(adapted)
    kpis = calculate_independent_kpis(dataset, tables, as_of=as_of)
    return build_validation_evidence(
        dataset=dataset,
        role=manifest["role"],
        sample_size=sample_size,
        rows_read=adapted.rows_read,
        validated=validated,
        unavailable=kpis["unavailable"] + [mapping.output_field for mapping in adapted.unavailable],
        kpis=kpis,
        queue_counts={},
        provenance=[
            {
                "source": item.source,
                "transformation": item.transformation,
                "output": item.output,
            }
            for item in adapted.provenance
        ],
        mappings=adapted.mappings,
    )


def write_evidence(evidence: dict[str, Any], output: str | Path) -> None:
    Path(output).write_text(
        json.dumps(evidence, default=str, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = ["run_validation", "write_evidence"]
