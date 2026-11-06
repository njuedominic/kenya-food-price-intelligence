from __future__ import annotations

import hashlib
from email.message import Message
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from kenya_food_intelligence.profiling.knbs import (
    PDFProfileError,
    parse_numeric_source,
    profile_report,
)
from kenya_food_intelligence.sources.http import (
    HTTPRequestError,
    HTTPResponse,
    HTTPStatusError,
)
from kenya_food_intelligence.sources.knbs import (
    DownloadedReport,
    KNBSClient,
    KNBSDocumentError,
    KNBSHTTPError,
    ReportLink,
)

PDF_URLS = {
    "2026-08": (
        "https://www.knbs.or.ke/wp-content/uploads/2026/08/"
        "Kenya-Consumer-Price-Indices-and-Inflation-Rates-August-2026.pdf"
    ),
    "2026-09": (
        "https://www.knbs.or.ke/wp-content/uploads/2026/09/"
        "Kenya-Consumer-Price-Indices-and-Inflation-Rates-September-2026.pdf"
    ),
    "2026-10": (
        "https://www.knbs.or.ke/wp-content/uploads/2026/10/"
        "Kenya-Consumer-Price-Indices-and-Inflation-Rates-October-2026.pdf"
    ),
}


class FakeResponse(BytesIO):
    def __init__(
        self,
        body: bytes,
        *,
        url: str,
        content_type: str = "application/pdf",
        status: int = 200,
    ) -> None:
        super().__init__(body)
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Last-Modified"] = "Fri, 30 Oct 2026 07:55:11 GMT"
        self.url = url

    def geturl(self) -> str:
        return self.url


class FakePage:
    def __init__(self, text: str, tables: list[list[list[str | None]]]) -> None:
        self.text = text
        self.tables = tables

    def extract_text(self) -> str:
        return self.text

    def extract_tables(self, table_settings: dict[str, object] | None = None):
        return self.tables


class FakePDF:
    def __init__(self, pages: list[FakePage]) -> None:
        self.pages = pages

    def __enter__(self) -> FakePDF:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def make_pdf(page_texts: list[str]) -> bytes:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    for text in page_texts:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference})}
        )
        content = DecodedStreamObject()
        text_lines = []
        for line in text.splitlines():
            escaped_line = (
                line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            )
            text_lines.append(f"({escaped_line}) Tj T*")
        content.set_data(
            f"BT /F1 12 Tf 14 TL 50 700 Td {' '.join(text_lines)} ET".encode()
        )
        page[NameObject("/Contents")] = writer._add_object(content)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def downloaded_report(path: Path, month: str = "2026-10") -> DownloadedReport:
    data = path.read_bytes()
    month_name = {"2026-08": "August", "2026-09": "September", "2026-10": "October"}[
        month
    ]
    report = ReportLink(
        reference_month=month,
        title=f"Consumer Price Indices and Inflation Rates - {month_name} 2026",
        document_url=PDF_URLS[month],
        publication_page_url=None,
        publication_date="2026-10-30",
    )
    return DownloadedReport(
        report=report,
        path=path,
        downloaded_at_utc="2026-11-06T08:00:00+00:00",
        http_status=200,
        content_type="application/pdf",
        file_size_bytes=len(data),
        checksum_sha256=hashlib.sha256(data).hexdigest(),
        last_modified="Fri, 30 Oct 2026 07:55:11 GMT",
        etag='"test-etag"',
    )


def test_discovers_exact_month_links_from_official_listing() -> None:
    links = "\n".join(
        f'<a href="https://www.knbs.or.ke/reports/cpi-{month}/">'
        f"Consumer Price Indices and Inflation Rates {month}</a>"
        f'<a href="{url}">PDF</a>'
        for month, url in zip(
            ("August 2026", "September 2026", "October 2026"),
            PDF_URLS.values(),
            strict=True,
        )
    )
    listing = f"<html><body>{links}</body></html>"
    pages = {
        "August 2026": (
            "<html><head><title>August source</title>"
            '<meta property="article:published_time" '
            'content="2026-08-31T12:00:00+03:00">'
            "</head></html>"
        ),
        "September 2026": (
            "<html><head><title>September source</title>"
            '<meta property="article:published_time" '
            'content="2026-09-30T12:00:00+03:00">'
            "</head></html>"
        ),
        "October 2026": "<html><head><title>October source</title></head></html>",
    }
    client = KNBSClient(max_retries=0)
    with patch.object(
        client,
        "_get_html",
        side_effect=[
            listing,
            pages["August 2026"],
            pages["September 2026"],
            pages["October 2026"],
        ],
    ):
        reports = client.discover_reports(["2026-08", "2026-09", "2026-10"])

    assert reports["2026-08"].document_url == PDF_URLS["2026-08"]
    assert reports["2026-09"].document_url == PDF_URLS["2026-09"]
    assert reports["2026-10"].document_url == PDF_URLS["2026-10"]
    assert reports["2026-08"].publication_date == "2026-08-31"
    assert reports["2026-08"].publication_date_basis == "article:published_time"
    assert reports["2026-09"].publication_date == "2026-09-30"
    assert reports["2026-10"].publication_date is None


def test_download_records_http_metadata_and_checksum(tmp_path: Path) -> None:
    body = make_pdf(["October 2026 CPI publication"])
    report = ReportLink("2026-10", "October 2026 CPI", PDF_URLS["2026-10"], None, None)
    client = KNBSClient(max_retries=0)
    with patch.object(
        client.http,
        "get",
        return_value=HTTPResponse(
            status=200,
            content_type="application/pdf",
            final_url=report.document_url,
            headers={
                "Last-Modified": "Fri, 30 Oct 2026 07:55:11 GMT",
                "ETag": '"test-etag"',
            },
            body=body,
        ),
    ):
        result = client.download(report, tmp_path)

    assert result.path.read_bytes() == body
    assert result.http_status == 200
    assert result.content_type == "application/pdf"
    assert result.file_size_bytes == len(body)
    assert result.checksum_sha256 == hashlib.sha256(body).hexdigest()
    assert result.last_modified == "Fri, 30 Oct 2026 07:55:11 GMT"
    assert result.etag == '"test-etag"'
    assert result.downloaded_at_utc.endswith("+00:00")


def test_download_timeout_is_reported_without_retrying_non_http_error(
    tmp_path: Path,
) -> None:
    report = ReportLink("2026-10", "October 2026 CPI", PDF_URLS["2026-10"], None, None)
    client = KNBSClient(max_retries=0)
    with patch.object(
        client.http,
        "get",
        side_effect=HTTPRequestError(report.document_url, 1),
    ):
        with pytest.raises(KNBSHTTPError, match="after 1 attempts"):
            client.download(report, tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_download_retries_transient_http_error_with_configured_backoff(
    tmp_path: Path,
) -> None:
    body = make_pdf(["October 2026 CPI publication"])
    report = ReportLink("2026-10", "October 2026 CPI", PDF_URLS["2026-10"], None, None)
    temporary_failure = HTTPError(
        report.document_url, 503, "Service Unavailable", {}, BytesIO()
    )
    client = KNBSClient(timeout=3.0, max_retries=1, backoff_factor=0.25)
    with (
        patch(
            "kenya_food_intelligence.sources.http.urlopen",
            side_effect=[
                temporary_failure,
                FakeResponse(body, url=report.document_url),
            ],
        ) as open_url,
        patch("kenya_food_intelligence.sources.http.time.sleep") as sleep,
    ):
        result = client.download(report, tmp_path)

    assert result.path.exists()
    assert open_url.call_count == 2
    assert [call.kwargs["timeout"] for call in open_url.call_args_list] == [3.0, 3.0]
    sleep.assert_called_once_with(0.25)


def test_http_403_is_reported_without_retry(tmp_path: Path) -> None:
    report = ReportLink("2026-10", "October 2026 CPI", PDF_URLS["2026-10"], None, None)
    client = KNBSClient(max_retries=0)
    with patch.object(
        client.http,
        "get",
        side_effect=HTTPStatusError(403, report.document_url),
    ):
        with pytest.raises(KNBSHTTPError, match="HTTP 403"):
            client.download(report, tmp_path)


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (b"not a PDF", "application/pdf"),
        (b"%PDF-1.4 fake", "text/html"),
    ],
)
def test_invalid_document_format_is_rejected(
    tmp_path: Path, body: bytes, content_type: str
) -> None:
    report = ReportLink("2026-10", "October 2026 CPI", PDF_URLS["2026-10"], None, None)
    client = KNBSClient(max_retries=0)
    with patch.object(
        client.http,
        "get",
        return_value=HTTPResponse(200, content_type, report.document_url, {}, body),
    ):
        with pytest.raises(KNBSDocumentError):
            client.download(report, tmp_path)


@pytest.mark.parametrize(
    ("source", "expected"),
    [("1,234.50", 1234.5), ("7.3%", 7.3), ("−2.0", -2.0), ("-", None), ("bad", None)],
)
def test_numeric_parsing_preserves_unparseable_values_as_none(
    source: str, expected: float | None
) -> None:
    assert parse_numeric_source(source) == expected


def test_profiles_multiple_table_layouts_and_data_quality(tmp_path: Path) -> None:
    path = tmp_path / "controlled.pdf"
    path.write_bytes(
        make_pdf(
            [
                "Consumer Price Indices\nFood and Non-Alcoholic Beverages\n"
                "National Average Retail Prices",
                "Consumer Price Index Inflation Rates for Kenya",
            ]
        )
    )
    fake_pdf = FakePDF(
        [
            FakePage(
                "Consumer Price Indices\nFood and Non-Alcoholic Beverages\n"
                "Table 3: National Average Retail Prices of Selected Commodities\n"
                "Sugar 1 Kg 184.35 165.79 163.84 -1.2 -11.1\n"
                "Sugar 1 Kg 184.35 165.79 163.84 -1.2 -11.1\n"
                "Milk 1 Litre 72.03 77.54 - 3.9 11.9\n"
                "Gas/LPG 13 Kg 3 ,158.35 3 ,432.21 3 ,426.89 -0.2 8.5\n"
                "Local Flights (Nairobi to Return\n"
                "Mombasa) 1 Ticket 29,000.00 26,600.00 27,000.00 1.5 -6.9\n"
                "Mobile Phone Airtime 1 Per minute 2.43 2.44 2.45 0.4 0.7\n"
                "Electricity 200 kWh 5,764.15 5,533.76 5,529.66 -0.1 -4.1\n"
                "Electricity 50 kWh 1,315.80 1,258.21 1,257.18 -0.1 -4.5",
                [],
            ),
            FakePage(
                "Consumer Price Index Inflation Rates for Kenya\n"
                "Table 1: One and Twelve-Month Percentage Changes in the "
                "Consumer Price Indices\n"
                "Food and Non-Alcoholic Beverages 32.9094 0.9 10.2\n"
                "Transport 9.6468 not reported 17.1",
                [],
            ),
        ]
    )
    with patch(
        "kenya_food_intelligence.profiling.knbs.pdfplumber.open", return_value=fake_pdf
    ):
        profile = profile_report(downloaded_report(path))

    assert profile["pdf"]["page_count"] == 2
    assert profile["pdf"]["text_extractable"] is True
    assert profile["topic_pages"]["cpi"] == [1, 2]
    assert profile["topic_pages"]["food_and_non_alcoholic_beverages"] == [1, 2]
    assert profile["topic_pages"]["national_average_retail_prices"] == [1]
    assert profile["tables"][0]["column_headers"] == [
        "Commodity Name",
        "Quantity",
        "Unit of Measure",
        "Average Price October 2025",
        "Average Price September 2026",
        "Average Price October 2026",
        "% Change over last month",
        "% Change over last year",
    ]
    assert profile["tables"][1]["column_headers"] == [
        "13 COICOP Divisions",
        "Weight %",
        "% Change on last month",
        "% Change on same month of previous year",
    ]
    assert profile["tables"][1]["normalized_sample_rows"][0]["normalized_values"] == {
        "cpi_category": "Food and Non-Alcoholic Beverages",
        "weight_pct": 32.9094,
        "monthly_change_pct": 0.9,
        "annual_change_pct": 10.2,
    }
    normalized_retail = [
        row["normalized_values"]
        for row in profile["tables"][0]["normalized_sample_rows"]
    ]
    assert {row["commodity_name"]: row for row in normalized_retail}["Gas/LPG"][
        "average_price_current_month_kes"
    ] == 3426.89
    assert {row["commodity_name"]: row for row in normalized_retail}[
        "Local Flights (Nairobi to Return Mombasa)"
    ]["unit_of_measure"] == "Ticket"
    assert {row["commodity_name"]: row for row in normalized_retail}[
        "Mobile Phone Airtime"
    ]["unit_of_measure"] == "Per minute"
    checks = profile["data_quality_checks"]
    assert checks["duplicate_commodity_month_observations"] == [
        {
            "reference_month": "2026-10",
            "commodity_source_text": "Sugar",
            "quantity_source_text": "1",
            "unit_source_text": "Kg",
            "count": 2,
        }
    ]
    assert checks["missing_prices"] == [
        {"page": 1, "row_number": 4, "column": "Average Price October 2026"}
    ]
    assert checks["unparsed_table_lines"] == [
        {
            "page": 2,
            "lines": ["Transport 9.6468 not reported 17.1"],
        }
    ]


def test_missing_expected_topic_is_flagged(tmp_path: Path) -> None:
    path = tmp_path / "controlled.pdf"
    path.write_bytes(make_pdf(["General CPI report"]))
    fake_pdf = FakePDF([FakePage("General CPI report", [])])
    with patch(
        "kenya_food_intelligence.profiling.knbs.pdfplumber.open", return_value=fake_pdf
    ):
        profile = profile_report(downloaded_report(path))

    assert (
        "food_and_non_alcoholic_beverages"
        in profile["data_quality_checks"]["missing_expected_topics"]
    )
    assert (
        "national_average_retail_prices"
        in profile["data_quality_checks"]["missing_expected_topics"]
    )


def test_manually_reviewed_october_retail_table_fixture(tmp_path: Path) -> None:
    fixture_path = (
        Path(__file__).parents[1]
        / "fixtures"
        / "knbs"
        / "retail_prices_october_2026.txt"
    )
    page_text = fixture_path.read_text(encoding="utf-8")
    path = tmp_path / "controlled.pdf"
    path.write_bytes(make_pdf([""] * 9 + [page_text]))
    profile = profile_report(downloaded_report(path))

    retail_table = next(
        table for table in profile["tables"] if table["candidate_price_table"]
    )
    assert retail_table["page"] == 10
    assert retail_table["extraction_method"] == (
        "pdfplumber text lines; commodity and trailing numeric fields aligned"
    )
    assert retail_table["column_headers"] == [
        "Commodity Name",
        "Quantity",
        "Unit of Measure",
        "Average Price October 2025",
        "Average Price September 2026",
        "Average Price October 2026",
        "% Change over last month",
        "% Change over last year",
    ]
    rows = {
        row["normalized_values"]["commodity_name"]: row
        for row in retail_table["normalized_sample_rows"]
    }
    assert rows["Sugar"]["source_cells"] == [
        "Sugar",
        "1",
        "Kg",
        "184.35",
        "165.79",
        "163.84",
        "-1.2",
        "-11.1",
    ]
    assert (
        rows["Gas/LPG"]["normalized_values"]["average_price_current_month_kes"]
        == 3429.61
    )
    assert retail_table["misaligned_rows"] == []
    assert retail_table["table_extraction_reliable"] is True


def test_malformed_pdf_raises_meaningful_error(tmp_path: Path) -> None:
    path = tmp_path / "invalid.pdf"
    path.write_bytes(b"%PDF-1.4 but not a valid PDF structure")
    with pytest.raises(PDFProfileError, match="Unable to parse PDF"):
        profile_report(downloaded_report(path))


def test_discovery_rejects_invalid_month_and_non_official_document_url() -> None:
    client = KNBSClient(max_retries=0)
    with pytest.raises(ValueError, match="YYYY-MM"):
        client.discover_reports(["October 2026"])

    report = ReportLink(
        "2026-10",
        "October CPI",
        "https://example.com/report.pdf",
        None,
        None,
    )
    with pytest.raises(KNBSDocumentError, match="official KNBS host"):
        client.download(report, Path("unused"))
