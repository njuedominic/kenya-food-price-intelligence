#!/usr/bin/env python3
"""Ingest one official KNBS monthly CPI PDF into local Bronze storage."""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from pathlib import Path

from kenya_food_intelligence.ingestion.knbs import KNBSIngestor
from kenya_food_intelligence.sources.knbs import MAX_PDF_BYTES, KNBSClient
from kenya_food_intelligence.storage.bronze import BronzeStorageError, BronzeStore


class _UTCFormatter(logging.Formatter):
    converter = time.gmtime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--month",
        required=True,
        help="Reference month to ingest in YYYY-MM format",
    )
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--backoff-factor", type=float, default=0.5)
    parser.add_argument(
        "--max-pdf-bytes",
        type=int,
        default=MAX_PDF_BYTES,
        help=f"Maximum streamed PDF bytes (default: {MAX_PDF_BYTES})",
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
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", args.month):
        print("Configuration error: --month must use YYYY-MM format", file=sys.stderr)
        return 2
    try:
        client = KNBSClient(
            timeout=args.timeout,
            max_retries=args.max_retries,
            backoff_factor=args.backoff_factor,
            max_pdf_bytes=args.max_pdf_bytes,
        )
        result = KNBSIngestor(client, BronzeStore(args.output_root)).ingest(args.month)
    except ValueError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    except BronzeStorageError as error:
        print(f"Bronze storage error: {error}", file=sys.stderr)
        return 1

    size_bytes = result.file_size_bytes if result.file_size_bytes is not None else "-"
    page_count = result.page_count if result.page_count is not None else "-"
    print(
        f"reference_month={result.reference_month} outcome={result.outcome} "
        f"snapshot_id={result.snapshot_id or '-'} "
        f"size_bytes={size_bytes} "
        f"sha256={result.checksum_sha256 or '-'} "
        f"pages={page_count} run_id={result.ingestion_run_id}"
    )
    if result.outcome == "failed":
        print(f"Ingestion failed: {result.error}", file=sys.stderr)
        print(f"run_record={result.run_record_path}")
        return 1
    print(f"snapshot={result.snapshot_path}")
    print(f"manifest={result.manifest_path} run_record={result.run_record_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
