"""Acquire and preserve the official FAOSTAT Producer Prices archive."""

from __future__ import annotations

import logging
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from kenya_food_intelligence.sources.faostat import (
    ARCHIVE_FILENAME,
    DATA_CSV_MEMBER,
    SOURCE_ID,
    ArchiveValidation,
    DownloadedDataset,
    FAOSTATClient,
    FaostatError,
    discover_dataset,
    inspect_archive,
)
from kenya_food_intelligence.storage.bronze import (
    BronzeStorageError,
    BronzeStore,
    JsonObject,
    SnapshotIntegrityError,
)

RUN_RECORD_SCHEMA_VERSION = 1
PUBLICATION_IDENTITY = "normalized"
DATASET_NAME = "FAOSTAT Agricultural Producer Prices (normalized)"
PUBLISHER = "Food and Agriculture Organization of the United Nations"
LICENSE_NAME = "Creative Commons Attribution 4.0 International (CC BY 4.0)"
ATTRIBUTION = (
    "FAO. FAOSTAT: Producer Prices. Licensed under CC BY 4.0, subject to "
    "applicable source conditions and third-party restrictions."
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FAOSTATIngestionResult:
    ingestion_run_id: str
    outcome: str
    snapshot_id: str | None
    snapshot_path: Path | None
    manifest_path: Path | None
    run_record_path: Path
    archive_size_bytes: int | None
    checksum_sha256: str | None
    member_count: int | None
    error: str | None = None


class FAOSTATIngestor:
    """Download, validate, and atomically publish one FAOSTAT ZIP archive."""

    def __init__(self, client: FAOSTATClient, store: BronzeStore) -> None:
        self.client = client
        self.store = store

    def ingest(self) -> FAOSTATIngestionResult:
        run_id = uuid.uuid4().hex
        started_at = datetime.now(UTC)
        source = discover_dataset()
        downloaded: DownloadedDataset | None = None
        archive_validation: ArchiveValidation | None = None
        validation: JsonObject | None = None
        snapshot_id: str | None = None
        snapshot_path: Path | None = None
        outcome = "failed"
        error: Exception | None = None
        logger.info("Starting FAOSTAT Bronze ingestion from %s", source.download_url)

        try:
            self.store.output_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix=".faostat-download-",
                dir=self.store.output_root,
            ) as temporary_dir:
                downloaded = self.client.download_to(
                    Path(temporary_dir) / ARCHIVE_FILENAME
                )
                archive_validation = downloaded.archive_validation or inspect_archive(
                    downloaded.path
                )
                validation = {
                    "status": "passed",
                    "http_status_200": downloaded.http_status == 200,
                    "raw_sha256_verified": True,
                    "zip_structure_valid": True,
                    "member_safety_checks_passed": True,
                    "crc_validation_passed": True,
                    "resource_limits_passed": True,
                    "expected_dataset_member": DATA_CSV_MEMBER,
                }
                source_metadata: JsonObject = {
                    "dataset_identity": DATASET_NAME,
                    "dataset_id": source.dataset_id,
                    "dataset_url": source.dataset_url,
                    "domain_url": source.domain_url,
                    "download_url": source.download_url,
                    "source_archive_filename": ARCHIVE_FILENAME,
                    "publisher": PUBLISHER,
                    "license": LICENSE_NAME,
                    "license_terms_url": (
                        "https://www.fao.org/contact-us/terms/db-terms-of-use/en/"
                    ),
                    "third_party_restrictions_caveat": (
                        "FAO terms warn that third-party data may have additional "
                        "restrictions; check dataset metadata before redistribution."
                    ),
                }
                archive_metadata = archive_validation.as_manifest()
                metadata: JsonObject = {
                    "source": source_metadata,
                    "content_type": downloaded.content_type,
                    "archive": archive_metadata,
                    "validation": validation,
                    "attribution": ATTRIBUTION,
                }
                outcome, snapshot_id, snapshot_path, manifest = (
                    self.store.publish_binary_snapshot(
                        source_id=SOURCE_ID,
                        publication_identity=PUBLICATION_IDENTITY,
                        payload_path=downloaded.path,
                        filename=ARCHIVE_FILENAME,
                        metadata=metadata,
                        expected_sha256=downloaded.checksum_sha256,
                        expected_size_bytes=downloaded.file_size_bytes,
                    )
                )
                _verify_published_metadata(
                    manifest,
                    metadata=metadata,
                    downloaded=downloaded,
                )
                logger.info(
                    "FAOSTAT Bronze snapshot %s: %s",
                    outcome,
                    snapshot_id,
                )
        except (FaostatError, BronzeStorageError, OSError, ValueError) as caught:
            outcome = "failed"
            error = caught
            if validation is None:
                validation = {
                    "status": "failed",
                    "stage": "acquisition_or_archive_validation",
                    "error_category": type(caught).__name__,
                }
            logger.error(
                "FAOSTAT Bronze ingestion failed: %s", _safe_error_message(caught)
            )

        completed_at = datetime.now(UTC)
        run_record: JsonObject = {
            "run_record_schema_version": RUN_RECORD_SCHEMA_VERSION,
            "source_id": SOURCE_ID,
            "ingestion_run_id": run_id,
            "requested_url": source.download_url,
            "started_at_utc": started_at.isoformat(),
            "completed_at_utc": completed_at.isoformat(),
            "retrieved_at_utc": (
                downloaded.retrieved_at_utc if downloaded is not None else None
            ),
            "http_status": downloaded.http_status if downloaded is not None else None,
            "source_last_modified": (
                downloaded.last_modified if downloaded is not None else None
            ),
            "source_etag": downloaded.etag if downloaded is not None else None,
            "outcome": outcome,
            "snapshot_id": snapshot_id,
            "archive_size_bytes": (
                downloaded.file_size_bytes if downloaded is not None else None
            ),
            "checksum_sha256": (
                downloaded.checksum_sha256 if downloaded is not None else None
            ),
            "archive_member_count": (
                archive_validation.member_count
                if archive_validation is not None
                else None
            ),
            "validation": validation,
            "error": (
                {
                    "category": type(error).__name__,
                    "message": _safe_error_message(error),
                }
                if error is not None
                else None
            ),
        }
        run_record_path = self.store.write_source_run_record(run_record)
        return FAOSTATIngestionResult(
            ingestion_run_id=run_id,
            outcome=outcome,
            snapshot_id=snapshot_id,
            snapshot_path=snapshot_path,
            manifest_path=(
                snapshot_path / "manifest.json" if snapshot_path is not None else None
            ),
            run_record_path=run_record_path,
            archive_size_bytes=(
                downloaded.file_size_bytes if downloaded is not None else None
            ),
            checksum_sha256=(
                downloaded.checksum_sha256 if downloaded is not None else None
            ),
            member_count=(
                archive_validation.member_count
                if archive_validation is not None
                else None
            ),
            error=_safe_error_message(error) if error is not None else None,
        )


def _verify_published_metadata(
    manifest: JsonObject,
    *,
    metadata: JsonObject,
    downloaded: DownloadedDataset,
) -> None:
    source = manifest.get("source")
    file_metadata = manifest.get("file")
    if (
        source != metadata["source"]
        or manifest.get("archive") != metadata["archive"]
        or manifest.get("validation") != metadata["validation"]
        or manifest.get("attribution") != metadata["attribution"]
        or not isinstance(file_metadata, dict)
        or file_metadata.get("filename") != ARCHIVE_FILENAME
        or file_metadata.get("content_type") != downloaded.content_type
        or file_metadata.get("size_bytes") != downloaded.file_size_bytes
        or file_metadata.get("sha256") != downloaded.checksum_sha256
    ):
        raise SnapshotIntegrityError(
            "FAOSTAT snapshot manifest does not match the validated archive"
        )


def _safe_error_message(error: Exception) -> str:
    message = re.sub(r"[\r\n\t]+", " ", str(error)).strip()
    return message[:1000]
