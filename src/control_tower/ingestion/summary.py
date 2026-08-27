"""Human- and machine-readable ingestion summaries."""

from __future__ import annotations

import json
from typing import Any

from control_tower.ingestion.contracts import IngestionResult


def summary_dict(result: IngestionResult) -> dict[str, Any]:
    """Convert an ingestion result into a stable JSON-compatible summary."""

    return {
        "manifest_identity": result.manifest_identity,
        "seed": result.seed,
        "as_of": result.as_of.isoformat(),
        "committed": result.committed,
        "sources": [summary.as_dict() for summary in result.summaries],
    }


def summary_json(result: IngestionResult) -> str:
    return json.dumps(summary_dict(result), default=str, indent=2, sort_keys=True)


__all__ = ["summary_dict", "summary_json"]
