"""Explicit, repeatable deterministic demo bootstrap for local deployment."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from control_tower.config import Settings
from control_tower.db import create_db_engine, create_session_factory
from control_tower.enums import ExceptionStatus, ExceptionType
from control_tower.exceptions.lifecycle import transition_exception
from control_tower.exceptions.service import ExceptionService
from control_tower.ingestion.loader import ingest
from control_tower.models import ExceptionRecord
from control_tower.synthetic.generator import generate


def _as_of(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("as_of must include a timezone")
    return parsed.astimezone(timezone.utc)


def _seed_lifecycle_sample(session, *, changed_at: datetime) -> None:
    """Apply the fixed six-record lifecycle sample without duplicating history."""

    lifecycle_paths = {
        ExceptionType.SLA_BREACH_RISK: (),
        ExceptionType.INVENTORY_SHORTAGE: (),
        ExceptionType.STOCKOUT_RISK: (ExceptionStatus.ACKNOWLEDGED,),
        ExceptionType.INVENTORY_MISMATCH: (
            ExceptionStatus.ACKNOWLEDGED,
            ExceptionStatus.IN_PROGRESS,
        ),
        ExceptionType.SUPPLIER_DELAY: (
            ExceptionStatus.ACKNOWLEDGED,
            ExceptionStatus.IN_PROGRESS,
            ExceptionStatus.RESOLVED,
        ),
        ExceptionType.SHIPMENT_DELAY: (ExceptionStatus.DISMISSED,),
    }
    records = list(
        session.scalars(
            select(ExceptionRecord).where(
                ExceptionRecord.exception_type.in_(lifecycle_paths)
            )
        )
    )
    records_by_type = {record.exception_type: record for record in records}
    if len(records) != len(lifecycle_paths) or set(records_by_type) != set(lifecycle_paths):
        return
    if any(record.status != ExceptionStatus.OPEN for record in records_by_type.values()):
        return

    for exception_type, path in lifecycle_paths.items():
        record = records_by_type[exception_type]
        for target in path:
            transition_exception(
                session,
                record.id,
                target,
                actor="bootstrap",
                reason="deterministic lifecycle sample"
                if target in (ExceptionStatus.RESOLVED, ExceptionStatus.DISMISSED)
                else None,
                changed_at=changed_at,
            )


def bootstrap(
    output_dir: str | Path,
    *,
    seed: int,
    as_of: datetime,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Generate, ingest, and detect once; rerunning is safe and idempotent."""

    configured = settings or Settings(as_of=as_of, deterministic_seed=seed)
    generate(output_dir, seed=seed, as_of=as_of, settings=configured)
    ingestion = ingest(output_dir, settings=configured)
    engine = create_db_engine(configured)
    try:
        with create_session_factory(engine=engine)() as session:
            detection = ExceptionService(session, configured).detect(as_of)
            _seed_lifecycle_sample(session, changed_at=as_of)
            session.commit()
    finally:
        engine.dispose()
    return {
        "as_of": as_of.isoformat().replace("+00:00", "Z"),
        "committed": ingestion.committed,
        "detections": detection.count,
        "created": detection.created,
        "updated": detection.updated,
        "skipped": detection.skipped,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the explicit deterministic demo bootstrap")
    parser.add_argument("--output-dir", default="/tmp/control_tower_demo_bundle")
    parser.add_argument("--seed", type=int, default=20250301)
    parser.add_argument("--as-of", type=_as_of, default=None)
    args = parser.parse_args(argv)
    settings = Settings()
    as_of = args.as_of or settings.as_of
    result = bootstrap(args.output_dir, seed=args.seed, as_of=as_of, settings=settings)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["committed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
