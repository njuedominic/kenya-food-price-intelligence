#!/usr/bin/env python3
"""Ingest a World Bank Kenya CPI indicator into local immutable Bronze storage."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from kenya_food_intelligence.ingestion.world_bank import (
    INDICATORS,
    WorldBankIngestor,
)
from kenya_food_intelligence.sources.world_bank import (
    DEFAULT_MAX_PAGE_BYTES,
    DEFAULT_MAX_PAGES,
    DEFAULT_MAX_TOTAL_BYTES,
    WorldBankClient,
)
from kenya_food_intelligence.storage.bronze import BronzeStorageError, BronzeStore


class _UTCFormatter(logging.Formatter):
    converter = time.gmtime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--indicator",
        required=True,
        choices=INDICATORS,
        help="Approved World Bank CPI indicator code",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--backoff-factor", type=float, default=0.5)
    parser.add_argument("--per-page", type=int, default=100)
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help=f"Maximum pages to retrieve (default: {DEFAULT_MAX_PAGES})",
    )
    parser.add_argument(
        "--max-page-bytes",
        type=int,
        default=DEFAULT_MAX_PAGE_BYTES,
        help=f"Maximum raw response bytes per page (default: {DEFAULT_MAX_PAGE_BYTES})",
    )
    parser.add_argument(
        "--max-total-bytes",
        type=int,
        default=DEFAULT_MAX_TOTAL_BYTES,
        help=f"Maximum raw response bytes across all pages "
        f"(default: {DEFAULT_MAX_TOTAL_BYTES})",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("data"),
        help="Root for ignored Bronze snapshots and manifest/run records",
    )
    return parser.parse_args()


def _configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        _UTCFormatter(
            "%(asctime)sZ %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def main() -> int:
    args = parse_args()
    _configure_logging()
    try:
        client = WorldBankClient(
            timeout=args.timeout,
            max_retries=args.max_retries,
            backoff_factor=args.backoff_factor,
            max_pages=args.max_pages,
            max_page_bytes=args.max_page_bytes,
            max_total_bytes=args.max_total_bytes,
        )
        ingestor = WorldBankIngestor(client, BronzeStore(args.output_root))
        result = ingestor.ingest(args.indicator, per_page=args.per_page)
    except ValueError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    except BronzeStorageError as error:
        print(f"Bronze storage error: {error}", file=sys.stderr)
        return 1

    observation_count = (
        str(result.observation_count) if result.observation_count is not None else "-"
    )
    page_count = str(result.page_count) if result.page_count is not None else "-"
    print(
        f"indicator={result.indicator_code} outcome={result.outcome} "
        f"snapshot_id={result.snapshot_id or '-'} "
        f"observations={observation_count} pages={page_count} "
        f"storage={result.snapshot_path or '-'} run_id={result.ingestion_run_id}"
    )
    if result.outcome == "failed":
        print(f"Ingestion failed: {result.error}", file=sys.stderr)
        return 1
    print(f"manifest={result.manifest_path} run_record={result.run_record_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
