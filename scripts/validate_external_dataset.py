#!/usr/bin/env python3
"""Run M07.5 validation against user-provided local CSVs; never downloads data."""

from __future__ import annotations

import argparse
from datetime import datetime

from control_tower.external_validation.runner import run_validation, write_evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", choices=("olist", "dataco"))
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--as-of", required=True, help="timezone-aware ISO-8601 instant")
    parser.add_argument("--sample-size", type=int, default=None)
    parser.add_argument("--output", required=True, help="JSON evidence path outside the repository")
    args = parser.parse_args()
    as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        parser.error("--as-of must include a timezone")
    evidence = run_validation(
        args.dataset,
        args.input_dir,
        as_of=as_of,
        sample_size=args.sample_size,
    )
    write_evidence(evidence, args.output)
    print(f"validation evidence written: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
