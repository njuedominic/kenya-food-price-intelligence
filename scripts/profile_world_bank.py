#!/usr/bin/env python3
"""Profile Kenya CPI indicators from the World Bank Indicators API."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from kenya_food_intelligence.profiling.world_bank import (
    INDICATORS,
    failed_profile,
    profile_indicator,
)
from kenya_food_intelligence.sources.world_bank import WorldBankClient, WorldBankError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout", type=float, default=20.0, help="HTTP timeout in seconds"
    )
    parser.add_argument(
        "--max-retries", type=int, default=3, help="Retries after the initial request"
    )
    parser.add_argument(
        "--backoff-factor",
        type=float,
        default=0.5,
        help="Initial exponential retry delay in seconds",
    )
    parser.add_argument(
        "--per-page", type=int, default=100, help="Observations per API page (1-1000)"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports/source_profiling"),
        help="Directory for machine-readable aggregate reports",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        client = WorldBankClient(
            timeout=args.timeout,
            max_retries=args.max_retries,
            backoff_factor=args.backoff_factor,
        )
        if not 1 <= args.per_page <= 1000:
            raise ValueError("per_page must be between 1 and 1000")
    except ValueError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2

    profiles = []
    failures = 0
    for indicator_code in INDICATORS:
        try:
            profiles.append(
                profile_indicator(client, indicator_code, per_page=args.per_page)
            )
        except WorldBankError as error:
            failures += 1
            profiles.append(
                failed_profile(indicator_code, error, per_page=args.per_page)
            )
            print(f"{indicator_code}: profiling failed: {error}", file=sys.stderr)

    report = {
        "report_name": "World Bank Kenya CPI source profile",
        "generated_at": datetime.now(UTC).isoformat(),
        "profiling_outcome": "success" if failures == 0 else "partial_failure",
        "indicators": profiles,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_path = args.output_dir / f"world_bank_profile_{timestamp}.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Profile report written to {report_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
