"""Acquire and preserve official KNBS monthly CPI PDF publications."""

from __future__ import annotations

import hashlib
import logging
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from kenya_food_intelligence.sources.knbs import (
    LISTING_URL,
    MONTH_NAMES,
    DownloadedReport,
    KNBSClient,
    KNBSDocumentError,
    KNBSError,
    ReportLink,
)
from kenya_food_intelligence.storage.bronze import (
    BronzeStorageError,
    BronzeStore,
    JsonObject,
)

SOURCE_ID = "knbs_cpi_monthly"
RUN_RECORD_SCHEMA_VERSION = 1
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KNBSIngestionResult:
    ingestion_run_id: str
    reference_month: str
    outcome: str
    snapshot_id: str | None
    snapshot_path: Path | None
    manifest_path: Path | None
    run_record_path: Path
    checksum_sha256: str | None
    file_size_bytes: int | None
    page_count: int | None
    error: str | None = None


class KNBSIngestor:
    """Discover, validate, and publish one monthly KNBS CPI PDF."""

    def __init__(self, client: KNBSClient, store: BronzeStore) -> None:
        self.client = client
        self.store = store

    def ingest(self, reference_month: str) -> KNBSIngestionResult:
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", reference_month):
            raise ValueError("Reference month must use YYYY-MM format")

        run_id = uuid.uuid4().hex
        started_at = datetime.now(UTC)
        report: ReportLink | None = None
        downloaded: DownloadedReport | None = None
        snapshot_id: str | None = None
        snapshot_path: Path | None = None
        page_count: int | None = None
        validation: JsonObject | None = None
        error_message: str | None = None
        error_category: str | None = None
        outcome = "failed"

        logger.info("Starting KNBS Bronze ingestion for %s", reference_month)
        try:
            reports = self.client.discover_reports([reference_month])
            report = reports.get(reference_month)
            if report is None:
                raise KNBSError(
                    f"No official PDF publication was found for {reference_month} "
                    f"at {LISTING_URL}"
                )
            if report.reference_month != reference_month:
                raise KNBSError(
                    "Official listing returned a document for a different month"
                )
            with tempfile.TemporaryDirectory(prefix="knbs-cpi-") as temporary_dir:
                downloaded = self.client.download_to(
                    report,
                    Path(temporary_dir) / "report.pdf",
                )
                page_count, validation = _validate_pdf(downloaded, reference_month)
                outcome, snapshot_id, snapshot_path, _manifest = (
                    self.store.publish_binary_snapshot(
                        source_id=SOURCE_ID,
                        publication_identity=reference_month,
                        payload_path=downloaded.path,
                        filename="report.pdf",
                        expected_sha256=downloaded.checksum_sha256,
                        expected_size_bytes=downloaded.file_size_bytes,
                        metadata={
                            "source": {
                                "listing_url": LISTING_URL,
                                "document_url": report.document_url,
                                "publication_page_url": report.publication_page_url,
                                "document_title": report.title,
                                "publication_date": report.publication_date,
                                "publication_date_basis": report.publication_date_basis,
                                "source_organization": (
                                    "Kenya National Bureau of Statistics"
                                ),
                            },
                            "content_type": downloaded.content_type,
                            "page_count": page_count,
                            "validation": validation,
                            "attribution": (
                                "Kenya National Bureau of Statistics (KNBS); "
                                "confirm current source terms before redistribution."
                            ),
                        },
                    )
                )
            logger.info(
                "KNBS Bronze snapshot %s for %s: %s",
                outcome,
                reference_month,
                snapshot_id,
            )
        except (KNBSError, BronzeStorageError, OSError) as caught:
            error_category = type(caught).__name__
            error_message = _safe_error_message(caught)
            logger.error(
                "KNBS Bronze ingestion failed for %s: %s",
                reference_month,
                error_message,
            )

        completed_at = datetime.now(UTC)
        record: JsonObject = {
            "run_record_schema_version": RUN_RECORD_SCHEMA_VERSION,
            "source_id": SOURCE_ID,
            "ingestion_run_id": run_id,
            "started_at_utc": started_at.isoformat(),
            "completed_at_utc": completed_at.isoformat(),
            "requested_reference_month": reference_month,
            "discovered_source_url": report.document_url if report else None,
            "discovered_source_filename": (
                urlparse(report.document_url).path.rsplit("/", 1)[-1]
                if report is not None
                else None
            ),
            "retrieved_at_utc": (
                downloaded.downloaded_at_utc if downloaded is not None else None
            ),
            "outcome": outcome,
            "snapshot_id": snapshot_id,
            "checksum_sha256": (
                downloaded.checksum_sha256 if downloaded is not None else None
            ),
            "file_size_bytes": (
                downloaded.file_size_bytes if downloaded is not None else None
            ),
            "page_count": page_count,
            "validation": validation,
            "error": (
                {"category": error_category, "message": error_message}
                if error_message is not None
                else None
            ),
        }
        run_record_path = self.store.write_source_run_record(record)
        return KNBSIngestionResult(
            ingestion_run_id=run_id,
            reference_month=reference_month,
            outcome=outcome,
            snapshot_id=snapshot_id,
            snapshot_path=snapshot_path,
            manifest_path=(
                snapshot_path / "manifest.json" if snapshot_path is not None else None
            ),
            run_record_path=run_record_path,
            checksum_sha256=(
                downloaded.checksum_sha256 if downloaded is not None else None
            ),
            file_size_bytes=(
                downloaded.file_size_bytes if downloaded is not None else None
            ),
            page_count=page_count,
            error=error_message,
        )


def _validate_pdf(
    downloaded: DownloadedReport,
    reference_month: str,
) -> tuple[int, JsonObject]:
    if downloaded.http_status != 200:
        raise KNBSDocumentError(
            f"Expected HTTP 200 for PDF, received {downloaded.http_status}"
        )
    media_type = (
        downloaded.content_type.split(";", 1)[0].strip().lower()
        if downloaded.content_type
        else ""
    )
    if media_type not in {"application/pdf", "application/octet-stream"}:
        raise KNBSDocumentError(
            f"Expected a PDF content type, received {downloaded.content_type!r}"
        )
    if downloaded.file_size_bytes <= 0:
        raise KNBSDocumentError("PDF size is empty")
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with downloaded.path.open("rb") as file:
            signature = file.read(5)
            if signature != b"%PDF-":
                raise KNBSDocumentError(
                    "Downloaded content does not have a PDF signature"
                )
            file.seek(0)
            while chunk := file.read(1024 * 1024):
                digest.update(chunk)
                size_bytes += len(chunk)
            file.seek(max(0, size_bytes - 4096))
            has_eof_marker = b"%%EOF" in file.read(4096)
        if size_bytes != downloaded.file_size_bytes:
            raise KNBSDocumentError("Downloaded PDF size changed before publication")
        if digest.hexdigest() != downloaded.checksum_sha256:
            raise KNBSDocumentError(
                "Downloaded PDF checksum changed before publication"
            )
        if not has_eof_marker:
            raise KNBSDocumentError("PDF is truncated or missing its EOF marker")

        reader = PdfReader(str(downloaded.path), strict=False)
        if reader.is_encrypted:
            raise KNBSDocumentError("Encrypted PDFs are not accepted")
        page_count = len(reader.pages)
        if page_count < 1:
            raise KNBSDocumentError("PDF must contain at least one page")
        page_texts = [page.extract_text() or "" for page in reader.pages]
    except KNBSDocumentError:
        raise
    except (OSError, ValueError, PdfReadError, EOFError) as error:
        raise KNBSDocumentError(f"Unable to read PDF structure: {error}") from error

    month_name = MONTH_NAMES[int(reference_month[5:7]) - 1]
    target_period = f"{month_name} {reference_month[:4]}"
    text = "\n".join(page_texts)
    cover_match, cover_basis = _check_explicit_period_mismatch(
        page_texts[:3],
        reference_month,
    )
    if cover_match is False:
        raise KNBSDocumentError(
            f"PDF cover identifies a different CPI reference month than {target_period}"
        )
    if cover_match is True:
        month_match: bool | None = True
        month_basis = cover_basis
    elif re.search(
        rf"\b{re.escape(month_name)}\s+{reference_month[:4]}\b",
        text,
        re.I,
    ):
        month_match = True
        month_basis = "PDF text"
    else:
        month_match = None
        month_basis = "not found in PDF text"

    title_match = (
        report_title_matches(downloaded.report.title, reference_month)
        if downloaded.report.title
        else None
    )
    if title_match is False:
        raise KNBSDocumentError(
            "Official listing title identifies a different reference month"
        )
    validation: JsonObject = {
        "status": "passed",
        "http_status_200": downloaded.http_status == 200,
        "pdf_content_type": downloaded.content_type,
        "pdf_signature": True,
        "eof_marker": True,
        "readable_pdf_structure": True,
        "page_count": page_count,
        "reference_month_match": (
            month_match if month_match is not None else "unverified"
        ),
        "reference_month_evidence": month_basis,
        "source_title_month_match": (
            title_match if title_match is not None else "unverified"
        ),
    }
    return page_count, validation


def report_title_matches(title: str, reference_month: str) -> bool | None:
    month_name = MONTH_NAMES[int(reference_month[5:7]) - 1]
    if re.search(
        rf"\b{re.escape(month_name)}\s+{reference_month[:4]}\b",
        title,
        re.I,
    ):
        return True
    period_pattern = "|".join(MONTH_NAMES)
    if re.search(rf"\b(?:{period_pattern})\s+\d{{4}}\b", title, re.I):
        return False
    return None


def _check_explicit_period_mismatch(
    cover_pages: list[str],
    reference_month: str,
) -> tuple[bool | None, str]:
    periods: set[str] = set()
    month_pattern = "|".join(MONTH_NAMES)
    for line in "\n".join(cover_pages).splitlines():
        if not re.search(r"\bCPI\b|consumer price|inflation", line, re.I):
            continue
        periods.update(
            f"{month} {year}"
            for month, year in re.findall(
                rf"\b({month_pattern})\s+(\d{{4}})\b",
                line,
                re.I,
            )
        )
    if periods:
        expected = f"{MONTH_NAMES[int(reference_month[5:7]) - 1]} {reference_month[:4]}"
        return expected in periods, "PDF cover text"
    return None, "not found in PDF text"


def _safe_error_message(error: Exception) -> str:
    message = re.sub(r"[\r\n\t]+", " ", str(error)).strip()
    return message[:1000]
