from __future__ import annotations

import hashlib
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from kenya_food_intelligence.ingestion.knbs import KNBSIngestor
from kenya_food_intelligence.sources.http import (
    HTTPDownloadResponse,
    HTTPResponseTooLarge,
)
from kenya_food_intelligence.sources.knbs import (
    AmbiguousPublicationError,
    DownloadedReport,
    KNBSClient,
    KNBSDocumentError,
    KNBSError,
    ReportLink,
)
from kenya_food_intelligence.storage.bronze import (
    BronzeStorageError,
    BronzeStore,
)

MONTH = "2026-10"
PDF_URL = (
    "https://www.knbs.or.ke/wp-content/uploads/2026/10/"
    "Kenya-Consumer-Price-Indices-and-Inflation-Rates-October-2026.pdf"
)
TITLE = "Consumer Price Indices and Inflation Rates - October 2026"
SOURCE_ID = "knbs_cpi_monthly"


def _pdf(texts: list[str]) -> bytes:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    for text in texts:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference})}
        )
        content = DecodedStreamObject()
        safe_text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content.set_data(f"BT /F1 12 Tf 50 700 Td ({safe_text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(content)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _report(url: str = PDF_URL, title: str | None = TITLE) -> ReportLink:
    return ReportLink(
        reference_month=MONTH,
        title=title,
        document_url=url,
        publication_page_url="https://www.knbs.or.ke/reports/october-2026/",
        publication_date="2026-10-30",
        publication_date_basis="article:published_time",
    )


class StubKNBSClient:
    def __init__(
        self,
        payloads: dict[str, bytes],
        *,
        reports: dict[str, ReportLink] | None = None,
        discovery_error: Exception | None = None,
    ) -> None:
        self.payloads = payloads
        self.reports = reports or {MONTH: _report()}
        self.discovery_error = discovery_error
        self._lock = threading.Lock()

    def discover_reports(self, months: list[str]) -> dict[str, ReportLink]:
        if self.discovery_error is not None:
            raise self.discovery_error
        return {month: self.reports[month] for month in months if month in self.reports}

    def download_to(self, report: ReportLink, destination: Path) -> DownloadedReport:
        with self._lock:
            body = self.payloads[report.document_url]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
        return DownloadedReport(
            report=report,
            path=destination,
            downloaded_at_utc="2026-11-06T12:00:00+00:00",
            http_status=200,
            content_type="application/pdf",
            file_size_bytes=len(body),
            checksum_sha256=hashlib.sha256(body).hexdigest(),
            last_modified=None,
            etag=None,
        )


def _ingestor(
    tmp_path: Path,
    body: bytes,
    *,
    report: ReportLink | None = None,
) -> KNBSIngestor:
    link = report or _report()
    client = StubKNBSClient(
        {link.document_url: body},
        reports={MONTH: link},
    )
    return KNBSIngestor(client, BronzeStore(tmp_path))


def test_discovery_matches_exact_reference_month_from_listing() -> None:
    month_page = (
        '<a href="https://www.knbs.or.ke/reports/october-2026/">'
        f"{TITLE}</a>"
        f'<a href="{PDF_URL}">Download PDF</a>'
    )
    client = KNBSClient(max_retries=0)
    with patch.object(client, "_get_html", return_value=f"<html>{month_page}</html>"):
        discovered = client.discover_reports([MONTH])

    assert discovered[MONTH].document_url == PDF_URL
    assert discovered[MONTH].title == TITLE


def test_discovery_reports_ambiguous_official_pdf_candidates() -> None:
    second_url = PDF_URL.replace(".pdf", "-revised.pdf")
    listing = (
        f'<a href="{PDF_URL}">October 2026 CPI report</a>'
        f'<a href="{second_url}">October 2026 CPI revised report</a>'
    )
    client = KNBSClient(max_retries=0)
    with (
        patch.object(client, "_get_html", return_value=listing),
        pytest.raises(AmbiguousPublicationError) as error,
    ):
        client.discover_reports([MONTH])

    assert set(error.value.candidates) == {PDF_URL, second_url}


def test_missing_publication_is_not_guessed() -> None:
    client = KNBSClient(max_retries=0)
    with patch.object(client, "_get_html", return_value="<html>No reports yet</html>"):
        assert client.discover_reports([MONTH]) == {}


@pytest.mark.parametrize("month", ["2026-13", "2026-00", "October 2026", "2026-1"])
def test_invalid_month_is_rejected(month: str, tmp_path: Path) -> None:
    ingestor = _ingestor(tmp_path, _pdf(["October 2026 CPI"]))
    with pytest.raises(ValueError, match="YYYY-MM"):
        ingestor.ingest(month)


def test_successful_ingestion_preserves_raw_pdf_and_records_metadata(
    tmp_path: Path,
) -> None:
    body = _pdf(["Consumer Price Indices and Inflation Rates - October 2026"])
    result = _ingestor(tmp_path, body).ingest(MONTH)

    assert result.outcome == "created"
    assert result.file_size_bytes == len(body)
    assert result.checksum_sha256 == hashlib.sha256(body).hexdigest()
    assert result.page_count == 1
    assert result.snapshot_path is not None
    assert (result.snapshot_path / "report.pdf").read_bytes() == body
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["identity"]["publication_identity"] == MONTH
    assert manifest["source"]["document_title"] == TITLE
    assert manifest["source"]["publication_date"] == "2026-10-30"
    assert manifest["file"]["size_bytes"] == len(body)
    assert manifest["file"]["sha256"] == result.checksum_sha256
    assert manifest["file"]["page_count"] == 1
    assert manifest["validation"]["reference_month_match"] is True
    run_record = json.loads(result.run_record_path.read_text())
    assert run_record["outcome"] == "created"
    assert run_record["requested_reference_month"] == MONTH


def test_pdf_page_count_is_not_fixed_to_thirteen(tmp_path: Path) -> None:
    body = _pdf(["October 2026 CPI"] * 2)
    assert _ingestor(tmp_path, body).ingest(MONTH).page_count == 2


def test_identical_rerun_reuses_immutable_snapshot(tmp_path: Path) -> None:
    body = _pdf(["October 2026 CPI"])
    ingestor = _ingestor(tmp_path, body)
    first = ingestor.ingest(MONTH)
    manifest = first.manifest_path.read_bytes()
    second = ingestor.ingest(MONTH)

    assert first.outcome == "created"
    assert second.outcome == "unchanged"
    assert second.snapshot_id == first.snapshot_id
    assert second.snapshot_path == first.snapshot_path
    assert second.manifest_path.read_bytes() == manifest
    assert len(list(first.snapshot_path.parent.glob("*/report.pdf"))) == 1


def test_revised_pdf_creates_new_snapshot_and_retains_previous(
    tmp_path: Path,
) -> None:
    first_body = _pdf(["October 2026 CPI original"])
    revised_body = _pdf(["October 2026 CPI revised"])
    first = _ingestor(tmp_path, first_body).ingest(MONTH)
    revised = _ingestor(tmp_path, revised_body).ingest(MONTH)

    assert revised.outcome == "created"
    assert revised.snapshot_id != first.snapshot_id
    assert (first.snapshot_path / "report.pdf").read_bytes() == first_body
    assert (revised.snapshot_path / "report.pdf").read_bytes() == revised_body
    assert len(list(first.snapshot_path.parent.iterdir())) == 2


def test_same_content_at_changed_url_reuses_snapshot_and_records_new_url(
    tmp_path: Path,
) -> None:
    body = _pdf(["October 2026 CPI"])
    first = _ingestor(tmp_path, body).ingest(MONTH)
    changed_url = PDF_URL.replace("October-2026.pdf", "October-2026-revised.pdf")
    second_report = _report(url=changed_url)
    second = _ingestor(tmp_path, body, report=second_report).ingest(MONTH)

    assert second.outcome == "unchanged"
    assert second.snapshot_id == first.snapshot_id
    run = json.loads(second.run_record_path.read_text())
    assert run["discovered_source_url"] == changed_url
    assert (
        run["discovered_source_filename"]
        == "Kenya-Consumer-Price-Indices-and-Inflation-Rates-October-2026-revised.pdf"
    )


@pytest.mark.parametrize(
    "body",
    [
        b"<html>not a PDF</html>",
        b"%PDF-1.4 corrupt structure %%EOF",
        _pdf(["October 2026 CPI"])[:-30],
    ],
)
def test_invalid_or_truncated_pdf_fails_without_snapshot(
    tmp_path: Path,
    body: bytes,
) -> None:
    result = _ingestor(tmp_path, body).ingest(MONTH)
    snapshots = tmp_path / "bronze" / SOURCE_ID / f"publication={MONTH}" / "snapshots"

    assert result.outcome == "failed"
    assert result.snapshot_id is None
    assert not snapshots.exists() or list(snapshots.iterdir()) == []
    assert json.loads(result.run_record_path.read_text())["outcome"] == "failed"


def test_wrong_reference_month_in_pdf_is_rejected(tmp_path: Path) -> None:
    body = _pdf(["Consumer Price Indices and Inflation Rates - September 2026"])
    result = _ingestor(tmp_path, body).ingest(MONTH)

    assert result.outcome == "failed"
    assert "different CPI reference month" in result.error


def test_wrong_cover_month_is_not_masked_by_later_text(tmp_path: Path) -> None:
    body = _pdf(
        [
            "Consumer Price Indices and Inflation Rates - September 2026",
            "October 2026 prices are compared in this table",
        ]
    )
    result = _ingestor(tmp_path, body).ingest(MONTH)

    assert result.outcome == "failed"
    assert "different CPI reference month" in result.error


def test_unverifiable_reference_period_is_explicitly_flagged(tmp_path: Path) -> None:
    body = _pdf(["National statistics publication"])
    result = _ingestor(tmp_path, body).ingest(MONTH)

    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["validation"]["reference_month_match"] == "unverified"
    assert manifest["validation"]["source_title_month_match"] is True


def test_discovery_failure_is_written_as_failed_run(tmp_path: Path) -> None:
    client = StubKNBSClient(
        {},
        discovery_error=KNBSError("official listing unavailable"),
    )
    result = KNBSIngestor(client, BronzeStore(tmp_path)).ingest(MONTH)

    record = json.loads(result.run_record_path.read_text())
    assert result.outcome == "failed"
    assert record["error"]["category"] == "KNBSError"
    assert record["discovered_source_url"] is None


def test_existing_snapshot_corruption_is_detected_and_not_overwritten(
    tmp_path: Path,
) -> None:
    body = _pdf(["October 2026 CPI"])
    ingestor = _ingestor(tmp_path, body)
    first = ingestor.ingest(MONTH)
    payload = first.snapshot_path / "report.pdf"
    payload.write_bytes(payload.read_bytes() + b"corruption")

    result = ingestor.ingest(MONTH)

    assert result.outcome == "failed"
    assert "checksum" in result.error
    assert payload.read_bytes() == body + b"corruption"


def test_existing_manifest_corruption_is_detected_and_not_overwritten(
    tmp_path: Path,
) -> None:
    body = _pdf(["October 2026 CPI"])
    ingestor = _ingestor(tmp_path, body)
    first = ingestor.ingest(MONTH)
    manifest_path = first.manifest_path
    manifest = json.loads(manifest_path.read_text())
    manifest["file"]["page_count"] = 99
    manifest_path.write_text(json.dumps(manifest))

    result = ingestor.ingest(MONTH)

    assert result.outcome == "failed"
    assert "manifest" in result.error
    assert json.loads(manifest_path.read_text())["file"]["page_count"] == 99


def test_binary_snapshot_rejects_unsafe_partition_and_filename(tmp_path: Path) -> None:
    payload = tmp_path / "payload.pdf"
    payload.write_bytes(_pdf(["October 2026 CPI"]))
    store = BronzeStore(tmp_path / "store")
    with pytest.raises(BronzeStorageError, match="Source ID"):
        store.publish_binary_snapshot(
            source_id="../outside",
            publication_identity=MONTH,
            payload_path=payload,
            filename="report.pdf",
            metadata={},
            expected_sha256=hashlib.sha256(payload.read_bytes()).hexdigest(),
            expected_size_bytes=payload.stat().st_size,
        )
    with pytest.raises(BronzeStorageError, match="filename"):
        store.publish_binary_snapshot(
            source_id=SOURCE_ID,
            publication_identity=MONTH,
            payload_path=payload,
            filename="../outside.pdf",
            metadata={},
            expected_sha256=hashlib.sha256(payload.read_bytes()).hexdigest(),
            expected_size_bytes=payload.stat().st_size,
        )


def test_concurrent_identical_and_revised_ingestions_converge_safely(
    tmp_path: Path,
) -> None:
    original = _pdf(["October 2026 CPI original"])
    revised = _pdf(["October 2026 CPI revised"])

    def run(body: bytes) -> object:
        return _ingestor(tmp_path, body).ingest(MONTH)

    with ThreadPoolExecutor(max_workers=2) as executor:
        identical = list(executor.map(run, [original, original]))
    assert sorted(result.outcome for result in identical) == ["created", "unchanged"]
    assert identical[0].snapshot_id == identical[1].snapshot_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        revisions = list(executor.map(run, [original, revised]))
    assert sorted(result.outcome for result in revisions) == ["created", "unchanged"]
    assert revisions[0].snapshot_id != revisions[1].snapshot_id
    assert len(list(identical[0].snapshot_path.parent.iterdir())) == 2


def test_knbs_stream_download_respects_byte_limit_and_preserves_bytes(
    tmp_path: Path,
) -> None:
    body = _pdf(["October 2026 CPI"])

    class Response(io.BytesIO):
        def __init__(self) -> None:
            super().__init__(body)
            self.status = 200
            self.headers = Message()
            self.headers["Content-Type"] = "application/pdf"
            self.headers["Content-Length"] = str(len(body))

        def geturl(self) -> str:
            return PDF_URL

    destination = tmp_path / "download" / "report.pdf"
    client = KNBSClient(max_retries=0, max_pdf_bytes=len(body))
    with patch(
        "kenya_food_intelligence.sources.http.urlopen",
        return_value=Response(),
    ):
        downloaded = client.download_to(_report(), destination)
    assert destination.read_bytes() == body
    assert downloaded.checksum_sha256 == hashlib.sha256(body).hexdigest()

    too_small_client = KNBSClient(max_retries=0, max_pdf_bytes=len(body) - 1)
    with (
        patch(
            "kenya_food_intelligence.sources.http.urlopen",
            return_value=Response(),
        ),
        pytest.raises(KNBSDocumentError, match="exceeds"),
    ):
        too_small_client.download_to(_report(), tmp_path / "too-large.pdf")
    assert not (tmp_path / "too-large.pdf").exists()


def test_stream_download_retries_timeout(tmp_path: Path) -> None:
    body = _pdf(["October 2026 CPI"])

    class Response(io.BytesIO):
        status = 200

        def __init__(self) -> None:
            super().__init__(body)
            self.headers = Message()
            self.headers["Content-Type"] = "application/pdf"

        def geturl(self) -> str:
            return PDF_URL

    client = KNBSClient(max_retries=1, backoff_factor=0.25)
    with (
        patch(
            "kenya_food_intelligence.sources.http.urlopen",
            side_effect=[TimeoutError("temporary timeout"), Response()],
        ) as open_url,
        patch("kenya_food_intelligence.sources.http.time.sleep") as sleep,
    ):
        result = client.download_to(_report(), tmp_path / "report.pdf")

    assert result.path.read_bytes() == body
    assert open_url.call_count == 2
    sleep.assert_called_once_with(0.25)


def test_stream_download_does_not_retry_http_403(tmp_path: Path) -> None:
    failure = HTTPError(PDF_URL, 403, "Forbidden", {}, io.BytesIO())
    client = KNBSClient(max_retries=2)
    with (
        patch(
            "kenya_food_intelligence.sources.http.urlopen",
            side_effect=failure,
        ) as open_url,
        pytest.raises(KNBSError, match="HTTP 403"),
    ):
        client.download_to(_report(), tmp_path / "report.pdf")

    assert open_url.call_count == 1
    assert not (tmp_path / "report.pdf").exists()


def test_pdf_stream_enforces_observed_size_limit_before_full_read(
    tmp_path: Path,
) -> None:
    body = b"%PDF-" + b"x" * 200_000 + b"%%EOF"

    class Response(io.BytesIO):
        def __init__(self) -> None:
            super().__init__(body)
            self.status = 200
            self.headers = Message()
            self.headers["Content-Type"] = "application/pdf"
            self.bytes_read = 0

        def read(self, size: int = -1) -> bytes:
            chunk = super().read(size)
            self.bytes_read += len(chunk)
            return chunk

        def geturl(self) -> str:
            return PDF_URL

    response = Response()
    client = KNBSClient(max_retries=0, max_pdf_bytes=100)
    with (
        patch(
            "kenya_food_intelligence.sources.http.urlopen",
            return_value=response,
        ),
        pytest.raises(KNBSDocumentError, match="exceeds"),
    ):
        client.download_to(_report(), tmp_path / "oversized.pdf")

    assert response.bytes_read == 64 * 1024
    assert response.bytes_read < len(body)
    assert not (tmp_path / "oversized.pdf").exists()


def test_listing_response_and_document_download_reject_wrong_media_types(
    tmp_path: Path,
) -> None:
    client = KNBSClient(max_retries=0)
    with patch.object(
        client.http,
        "download_to",
        return_value=HTTPDownloadResponse(
            status=200,
            content_type="text/html",
            final_url=PDF_URL,
            headers={},
            size_bytes=20,
            checksum_sha256="0" * 64,
        ),
    ):
        with pytest.raises(KNBSDocumentError, match="content type"):
            client.download_to(_report(), tmp_path / "report.pdf")
    assert list(tmp_path.iterdir()) == []


def test_timeout_and_403_fail_without_success_snapshot(tmp_path: Path) -> None:
    client = StubKNBSClient({}, discovery_error=KNBSError("HTTP 403"))
    result = KNBSIngestor(client, BronzeStore(tmp_path)).ingest(MONTH)

    assert result.outcome == "failed"
    assert result.snapshot_path is None
    assert result.error == "HTTP 403"


def test_network_size_limit_exception_is_not_swallowed(tmp_path: Path) -> None:
    client = KNBSClient(max_retries=0)
    with patch.object(
        client.http,
        "download_to",
        side_effect=HTTPResponseTooLarge("exceeds max"),
    ):
        with pytest.raises(KNBSDocumentError, match="exceeds max"):
            client.download_to(_report(), tmp_path / "report.pdf")
