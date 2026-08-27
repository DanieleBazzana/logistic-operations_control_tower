"""M03 deterministic exception intelligence package."""

from control_tower.exceptions.contracts import Detection, DetectionRunResult, ExceptionDetection
from control_tower.exceptions.lifecycle import (
    ExceptionLifecycle,
    InvalidTransition,
    transition_exception,
)
from control_tower.exceptions.rules import (
    detect_all,
    detect_inventory_mismatch,
    detect_inventory_shortage,
    detect_shipment_delay,
    detect_sla_breach_risk,
    detect_stockout_risk,
    detect_supplier_delay,
)
from control_tower.exceptions.service import (
    ExceptionService,
    detect_and_persist,
    make_deduplication_key,
    persist_detections,
)
from control_tower.exceptions.severity import severity_for_detection, severity_for_metrics

__all__ = [
    "Detection",
    "DetectionRunResult",
    "ExceptionDetection",
    "ExceptionLifecycle",
    "ExceptionService",
    "InvalidTransition",
    "detect_all",
    "detect_and_persist",
    "detect_inventory_mismatch",
    "detect_inventory_shortage",
    "detect_shipment_delay",
    "detect_sla_breach_risk",
    "detect_stockout_risk",
    "detect_supplier_delay",
    "make_deduplication_key",
    "persist_detections",
    "severity_for_detection",
    "severity_for_metrics",
    "transition_exception",
]
