"""Explicit, repeatable deterministic demo bootstrap for local deployment."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from control_tower.config import Settings
from control_tower.db import create_db_engine, create_session_factory
from control_tower.exceptions.service import ExceptionService
from control_tower.ingestion.loader import ingest
from control_tower.synthetic.generator import generate


def _as_of(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("as_of must include a timezone")
    return parsed.astimezone(timezone.utc)


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
