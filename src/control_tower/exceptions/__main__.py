"""Command-line entry point for deterministic exception detection."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any

from control_tower.config import Settings
from control_tower.db import create_db_engine, create_session_factory
from control_tower.exceptions.service import ExceptionService


def _timezone_aware_datetime(value: str) -> datetime:
    """Parse an RFC3339-ish instant and normalize it to UTC."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an ISO-8601 datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("must include a timezone")
    return parsed.astimezone(timezone.utc)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run deterministic exception detection against PostgreSQL"
    )
    parser.add_argument(
        "--as-of",
        required=True,
        type=_timezone_aware_datetime,
        help="timezone-aware ISO-8601 evaluation instant (normalized to UTC)",
    )
    parser.add_argument(
        "--database-url",
        help="PostgreSQL URL; defaults to DATABASE_URL/application settings",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run detection once and print stable machine-readable counters."""

    args = _parser().parse_args(argv)
    settings_kwargs: dict[str, Any] = {"as_of": args.as_of}
    if args.database_url is not None:
        settings_kwargs["database_url"] = args.database_url
    settings = Settings(**settings_kwargs)
    engine = create_db_engine(settings)
    try:
        session_factory = create_session_factory(engine=engine)
        with session_factory() as session:
            result = ExceptionService(session, settings).detect(args.as_of)
            session.commit()
        print(
            json.dumps(
                {
                    "as_of": result.as_of.isoformat().replace("+00:00", "Z"),
                    "detections": result.count,
                    "created": result.created,
                    "updated": result.updated,
                    "skipped": result.skipped,
                },
                sort_keys=True,
            )
        )
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
