"""Contracts for local-only external dataset validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

MappingClass = Literal["DIRECT", "DERIVED", "APPROXIMATE", "UNAVAILABLE"]


@dataclass(frozen=True)
class FieldMapping:
    dataset: str
    artifact: str
    output_field: str
    source_fields: tuple[str, ...]
    classification: MappingClass
    transformation: str
    caveat: str = ""


@dataclass(frozen=True)
class Provenance:
    source: str
    transformation: str
    output: str


@dataclass(frozen=True)
class AdapterResult:
    dataset: str
    role: str
    rows_by_artifact: dict[str, list[dict[str, Any]]]
    provenance: tuple[Provenance, ...]
    mappings: tuple[FieldMapping, ...]
    unavailable: tuple[FieldMapping, ...]
    rows_read: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class CoherenceCheck:
    field: str
    expected: Any
    observed: Any
    status: str
    reason: str


def mapping_dict(mapping: FieldMapping) -> dict[str, Any]:
    return {
        "dataset": mapping.dataset,
        "artifact": mapping.artifact,
        "output_field": mapping.output_field,
        "source_fields": list(mapping.source_fields),
        "classification": mapping.classification,
        "transformation": mapping.transformation,
        "caveat": mapping.caveat,
    }


__all__ = [
    "AdapterResult",
    "CoherenceCheck",
    "FieldMapping",
    "MappingClass",
    "Provenance",
    "mapping_dict",
]
