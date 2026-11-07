#!/usr/bin/env python3
"""Ingest the official FAOSTAT normalized Producer Prices ZIP into Bronze."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from kenya_food_intelligence.ingestion.faostat import FAOSTATIngestor
from kenya_food_intelligence.sources.faostat import (
    MAX_ARCHIVE_BYTES,
    MAX_ARCHIVE_MEMBERS,
    MAX_MEMBER_BYTES,
    MAX_TOTAL_UNCOMPRESSED_BYTES,
    FAOSTATClient,
)
from kenya_food_intelligence.storage.bronze import BronzeStorageError, BronzeStore


class _UTCFormatter(logging.Formatter):
    converter = time.gmtime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--backoff-factor", type=float, default=0.5)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("data"),
        help="Root for ignored Bronze snapshots and manifest/run records",
    )
    parser.add_argument(
        "--max-download-bytes",
        type=int,
        default=MAX_ARCHIVE_BYTES,
        help=f"Maximum streamed ZIP bytes (default: {MAX_ARCHIVE_BYTES})",
    )
    parser.add_argument(
        "--max-members",
        type=int,
        default=MAX_ARCHIVE_MEMBERS,
        help=f"Maximum ZIP member count (default: {MAX_ARCHIVE_MEMBERS})",
    )
    parser.add_argument(
        "--max-member-uncompressed-bytes",
        type=int,
        default=MAX_MEMBER_BYTES,
        help=f"Maximum uncompressed bytes per member (default: {MAX_MEMBER_BYTES})",
    )
    parser.add_argument(
        "--max-total-uncompressed-bytes",
        type=int,
        default=MAX_TOTAL_UNCOMPRESSED_BYTES,
        help=(
            "Maximum aggregate uncompressed bytes validated "
            f"(default: {MAX_TOTAL_UNCOMPRESSED_BYTES})"
        ),
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
        client = FAOSTATClient(
            timeout=args.timeout,
            max_retries=args.max_retries,
            backoff_factor=args.backoff_factor,
            max_download_bytes=args.max_download_bytes,
            max_members=args.max_members,
            max_member_uncompressed_bytes=args.max_member_uncompressed_bytes,
            max_total_uncompressed_bytes=args.max_total_uncompressed_bytes,
        )
        result = FAOSTATIngestor(client, BronzeStore(args.output_root)).ingest()
    except ValueError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    except BronzeStorageError as error:
        print(f"Bronze storage error: {error}", file=sys.stderr)
        return 1

    size_bytes = (
        result.archive_size_bytes if result.archive_size_bytes is not None else "-"
    )
    members = result.member_count if result.member_count is not None else "-"
    print(
        f"source=FAOSTAT outcome={result.outcome} "
        f"snapshot_id={result.snapshot_id or '-'} "
        f"archive_size_bytes={size_bytes} members={members} "
        f"run_id={result.ingestion_run_id}"
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
