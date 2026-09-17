"""Explicit operator-only dataset replacement command flow."""

from __future__ import annotations

import argparse
import json
from datetime import datetime

from control_tower.config import Settings
from control_tower.db import create_db_engine, create_session_factory
from control_tower.replacement.service import (
    activate_dataset,
    ensure_dataset_version,
    rollback_dataset,
    stage_dataset_version,
    validate_dataset,
)


def _as_of(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("--as-of must include a timezone")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Operator-only dataset replacement controls")
    parser.add_argument("action", choices=("stage", "validate", "activate", "rollback"))
    parser.add_argument("--dataset-id", type=int)
    parser.add_argument("--dataset-key")
    parser.add_argument("--manifest-identity")
    parser.add_argument("--content-hash")
    parser.add_argument("--seed", type=int, default=20250301)
    parser.add_argument("--as-of", type=_as_of)
    parser.add_argument("--generator-revision", default="operator")
    args = parser.parse_args(argv)
    if args.action == "rollback" and args.dataset_id is None:
        target = None
    elif args.dataset_id is not None:
        target = args.dataset_id
    else:
        required = (args.dataset_key, args.manifest_identity, args.content_hash, args.as_of)
        if any(value is None for value in required):
            parser.error(
                "stage/validate/activate require --dataset-id or complete dataset identity"
            )
        target = None

    settings = Settings()
    engine = create_db_engine(settings)
    try:
        with create_session_factory(engine=engine)() as session:
            if target is None:
                dataset = ensure_dataset_version(
                    session,
                    dataset_key=args.dataset_key,
                    manifest_identity=args.manifest_identity,
                    content_hash=args.content_hash,
                    seed=args.seed,
                    as_of=args.as_of,
                    generator_revision=args.generator_revision,
                )
                target = dataset.id
            if args.action == "stage":
                result = stage_dataset_version(session, target)
            elif args.action == "validate":
                result = validate_dataset(session, target)
            elif args.action == "activate":
                result = activate_dataset(session, target)
            else:
                result = rollback_dataset(session, target)
            session.commit()
            print(
                json.dumps(
                    {"action": args.action, "dataset_id": result.id, "status": result.status}
                )
            )
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
