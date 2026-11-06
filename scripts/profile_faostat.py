#!/usr/bin/env python3
"""Download and profile Kenya's official FAOSTAT Producer Prices data."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kenya_food_intelligence.profiling.faostat import (
    FaostatProfileError,
    profile_dataset,
)
from kenya_food_intelligence.sources.faostat import (
    DATASET_URL,
    DOWNLOAD_URL,
    FAOSTATClient,
    FaostatError,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--backoff-factor", type=float, default=0.5)
    parser.add_argument(
        "--download-dir",
        type=Path,
        default=Path("data/landing/faostat"),
        help="Directory for the official domain ZIP (ignored by Git)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports/source_profiling"),
        help="Directory for the sanitized JSON profile",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report: dict[str, Any] = {
        "report_name": "FAOSTAT Producer Prices Kenya source profile",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "dataset_url": DATASET_URL,
        "download_url": DOWNLOAD_URL,
        "country": "Kenya",
        "profiling_outcome": "failed",
    }

    try:
        client = FAOSTATClient(
            timeout=args.timeout,
            max_retries=args.max_retries,
            backoff_factor=args.backoff_factor,
        )
        downloaded = client.download_dataset(args.download_dir)
        report.update(profile_dataset(downloaded))
    except ValueError as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        _write_report(args.output_dir, report)
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    except (FaostatError, FaostatProfileError) as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        _write_report(args.output_dir, report)
        print(f"FAOSTAT profiling failed: {error}", file=sys.stderr)
        return 1

    report["profiling_outcome"] = "success"
    report["generated_at_utc"] = datetime.now(UTC).isoformat()
    path = _write_report(args.output_dir, report)
    print(f"Profile report written to {path}")
    return 0


def _write_report(output_dir: Path, report: dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_path = output_dir / f"faostat_source_profile_{timestamp}.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return report_path


if __name__ == "__main__":
    raise SystemExit(main())
