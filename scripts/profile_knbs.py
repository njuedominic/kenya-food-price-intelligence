#!/usr/bin/env python3
"""Discover and profile official KNBS CPI publication PDFs."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from kenya_food_intelligence.profiling.knbs import (
    PDFProfileError,
    compare_layouts,
    profile_report,
)
from kenya_food_intelligence.sources.knbs import (
    LISTING_URL,
    KNBSClient,
    KNBSError,
    ReportLink,
)

DEFAULT_MONTHS = ("2026-08", "2026-09", "2026-10")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--months",
        nargs="+",
        default=list(DEFAULT_MONTHS),
        help="Reference months in YYYY-MM format (defaults to August-October 2026)",
    )
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--backoff-factor", type=float, default=0.5)
    parser.add_argument(
        "--download-dir",
        type=Path,
        default=Path("data/landing/knbs"),
        help="Local directory for downloaded source PDFs (ignored by Git)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports/source_profiling"),
        help="Directory for aggregate JSON profiling reports",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        client = KNBSClient(
            timeout=args.timeout,
            max_retries=args.max_retries,
            backoff_factor=args.backoff_factor,
        )
    except ValueError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2

    discovery_error: KNBSError | None = None
    try:
        discovered = client.discover_reports(args.months)
    except ValueError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    except KNBSError as error:
        discovered = {}
        discovery_error = error

    reports = []
    successful_profiles = []
    failed_count = 0
    for reference_month in args.months:
        report_link = discovered.get(reference_month)
        if discovery_error is not None:
            reports.append(_failed_report(reference_month, discovery_error))
            failed_count += 1
            continue
        if report_link is None:
            error = KNBSError(
                f"No official PDF link for {reference_month} was found at {LISTING_URL}"
            )
            reports.append(_failed_report(reference_month, error))
            print(f"{reference_month}: discovery failed: {error}", file=sys.stderr)
            failed_count += 1
            continue
        try:
            downloaded = client.download(report_link, args.download_dir)
            profile = profile_report(downloaded)
        except (KNBSError, PDFProfileError) as error:
            reports.append(_failed_report(reference_month, error, report_link))
            print(f"{reference_month}: profiling failed: {error}", file=sys.stderr)
            failed_count += 1
            continue
        reports.append(profile)
        successful_profiles.append(profile)

    explorer = client.probe_cpi_explorer()

    report = {
        "report_name": "KNBS monthly CPI source profile",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "profiling_outcome": ("success" if failed_count == 0 else "partial_failure"),
        "source_discovery": {
            "listing_url": LISTING_URL,
            "requested_months": args.months,
            "discovered_months": sorted(discovered),
            "discovery_error": (
                {
                    "type": type(discovery_error).__name__,
                    "message": str(discovery_error),
                }
                if discovery_error
                else None
            ),
        },
        "alternative_source_check": {
            "cpi_explorer_homepage": explorer,
            "scope": "Public homepage only; no undocumented endpoints inspected.",
        },
        "layout_comparison": compare_layouts(successful_profiles),
        "reports": reports,
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_path = args.output_dir / f"knbs_source_profile_{timestamp}.json"
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Profile report written to {report_path}")
    return 1 if failed_count else 0


def _failed_report(
    reference_month: str,
    error: Exception,
    report_link: ReportLink | None = None,
) -> dict[str, object]:
    report = {
        "source_id": "KNBS_CPI_MONTHLY",
        "reference_month": reference_month,
        "retrieved_at_utc": datetime.now(UTC).isoformat(),
        "profiling_outcome": "failed",
        "extraction_status": "failed",
        "error": {"type": type(error).__name__, "message": str(error)},
    }
    if report_link is not None:
        report.update(
            {
                "document_title": report_link.title,
                "document_url": report_link.document_url,
                "publication_page_url": report_link.publication_page_url,
                "publication_date": report_link.publication_date,
                "publication_date_basis": report_link.publication_date_basis,
            }
        )
    return report


if __name__ == "__main__":
    raise SystemExit(main())
