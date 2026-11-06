"""Discover and download official KNBS CPI publication PDFs."""

from __future__ import annotations

import hashlib
import re
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import truststore

from kenya_food_intelligence.sources.http import (
    HTTPClient,
    HTTPRequestError,
    HTTPResponseTooLarge,
    HTTPStatusError,
)

LISTING_URL = "https://www.knbs.or.ke/cpi-and-inflation-rates/"
EXPLORER_URL = "https://cpiexplorer.knbs.or.ke/"
MAX_HTML_BYTES = 5_000_000
MAX_PDF_BYTES = 40_000_000
USER_AGENT = "kenya-food-price-intelligence-knbs-profiler/0.1"
MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


class KNBSError(Exception):
    """Base exception for KNBS source discovery and download failures."""


class KNBSHTTPError(KNBSError):
    """A KNBS request failed or returned a non-retryable response."""


class KNBSDocumentError(KNBSError):
    """A linked document does not meet the expected PDF contract."""


@dataclass(frozen=True)
class ReportLink:
    reference_month: str
    title: str
    document_url: str
    publication_page_url: str | None
    publication_date: str | None
    publication_date_basis: str | None = None


@dataclass(frozen=True)
class DownloadedReport:
    report: ReportLink
    path: Path
    downloaded_at_utc: str
    http_status: int
    content_type: str | None
    file_size_bytes: int
    checksum_sha256: str
    last_modified: str | None
    etag: str | None


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []
        self.metadata: dict[str, str] = {}
        self.dates: list[str] = []
        self.title = ""
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs if value is not None}
        if tag == "a":
            self._anchor_href = values.get("href")
            self._anchor_text = []
        elif tag == "meta":
            name = (
                values.get("property") or values.get("name") or values.get("itemprop")
            )
            content = values.get("content")
            if name and content:
                self.metadata[name.lower()] = content
        elif tag == "time" and values.get("datetime"):
            self.dates.append(values["datetime"])
        elif tag == "title":
            self._in_title = True

    def handle_data(self, data: str) -> None:
        if self._anchor_href is not None:
            self._anchor_text.append(data)
        if self._in_title:
            self.title += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor_href is not None:
            text = " ".join(" ".join(self._anchor_text).split())
            self.anchors.append((self._anchor_href, text))
            self._anchor_href = None
            self._anchor_text = []
        elif tag == "title":
            self._in_title = False


class KNBSClient:
    """Fetch official listing pages and validate bounded PDF downloads."""

    def __init__(
        self,
        *,
        timeout: float = 25.0,
        max_retries: int = 2,
        backoff_factor: float = 0.5,
    ) -> None:
        try:
            tls_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except (OSError, ssl.SSLError) as error:
            raise ValueError(
                f"Unable to initialize the platform TLS store: {error}"
            ) from error
        self.http = HTTPClient(
            timeout=timeout,
            max_retries=max_retries,
            backoff_factor=backoff_factor,
            user_agent=USER_AGENT,
            ssl_context=tls_context,
        )

    def discover_reports(self, reference_months: list[str]) -> dict[str, ReportLink]:
        """Find month-specific PDF URLs in the official KNBS listing."""
        for month in reference_months:
            _validate_reference_month(month)
        listing = self._get_html(LISTING_URL)
        parsed_listing = _parse_page(listing)
        discovered: dict[str, ReportLink] = {}

        for reference_month in reference_months:
            month_name = MONTH_NAMES[int(reference_month[5:7]) - 1]
            year = reference_month[:4]
            month_token = month_name.lower()
            pdf_url = None
            page_url = None
            title = f"Consumer Price Indices and Inflation Rates - {month_name} {year}"
            for href, link_text in parsed_listing.anchors:
                if not _is_official_knbs_url(href):
                    continue
                lowered = href.lower()
                text = link_text.lower()
                contains_month = month_token in lowered or month_token in text
                if year not in lowered and year not in text:
                    continue
                if not contains_month:
                    continue
                if lowered.endswith(".pdf") and (
                    "consumer-price" in lowered or "cpi" in lowered
                ):
                    pdf_url = href
                elif "/reports/" in lowered:
                    page_url = href
                    if re.search(r"consumer price|inflation rates", link_text, re.I):
                        title = link_text

            if pdf_url is None:
                continue
            publication_date = None
            publication_date_basis = None
            if page_url is not None:
                try:
                    page = _parse_page(self._get_html(page_url))
                except KNBSHTTPError:
                    page = None
                if page is not None:
                    publication_date, publication_date_basis = _publication_date(page)
            discovered[reference_month] = ReportLink(
                reference_month=reference_month,
                title=title,
                document_url=pdf_url,
                publication_page_url=page_url,
                publication_date=publication_date,
                publication_date_basis=publication_date_basis,
            )
        return discovered

    def download(self, report: ReportLink, output_dir: Path) -> DownloadedReport:
        """Download a discovered PDF without following untrusted external links."""
        if not _is_official_knbs_url(report.document_url):
            raise KNBSDocumentError("Document URL is not on an official KNBS host")
        try:
            response = self.http.get(
                report.document_url,
                headers={"Accept": "application/pdf"},
                max_bytes=MAX_PDF_BYTES,
            )
        except HTTPStatusError as error:
            raise KNBSHTTPError(
                f"KNBS returned HTTP {error.status} for {report.document_url}"
            ) from error
        except HTTPRequestError as error:
            raise KNBSHTTPError(str(error)) from error
        except HTTPResponseTooLarge as error:
            raise KNBSDocumentError(str(error)) from error

        if not _is_official_knbs_url(response.final_url):
            raise KNBSDocumentError(
                "PDF request redirected outside the official KNBS host"
            )
        if response.content_type is None or response.content_type.split(";", 1)[
            0
        ].strip().lower() not in {"application/pdf", "application/octet-stream"}:
            raise KNBSDocumentError(
                f"Expected a PDF content type, received {response.content_type!r}"
            )
        if not response.body.startswith(b"%PDF-"):
            raise KNBSDocumentError("Downloaded content does not have a PDF signature")

        output_dir.mkdir(parents=True, exist_ok=True)
        month_tag = report.reference_month
        checksum = hashlib.sha256(response.body).hexdigest()
        destination = output_dir / f"knbs-cpi-{month_tag}-{checksum[:12]}.pdf"
        destination.write_bytes(response.body)
        return DownloadedReport(
            report=report,
            path=destination,
            downloaded_at_utc=datetime.now(UTC).isoformat(),
            http_status=response.status,
            content_type=response.content_type,
            file_size_bytes=len(response.body),
            checksum_sha256=checksum,
            last_modified=response.headers.get("Last-Modified"),
            etag=response.headers.get("ETag"),
        )

    def probe_cpi_explorer(self) -> dict[str, str | int | None]:
        """Check only the public Explorer homepage; do not inspect private endpoints."""
        probe_client = HTTPClient(
            timeout=min(self.http.timeout, 8.0),
            max_retries=0,
            backoff_factor=0,
            user_agent=USER_AGENT,
            ssl_context=self.http.ssl_context,
        )
        try:
            response = probe_client.get(
                EXPLORER_URL,
                headers={"Accept": "text/html"},
                max_bytes=MAX_HTML_BYTES,
            )
        except HTTPStatusError as error:
            return {
                "url": EXPLORER_URL,
                "outcome": "http_restricted_or_unavailable",
                "http_status": error.status,
                "content_type": None,
            }
        except (HTTPRequestError, HTTPResponseTooLarge) as error:
            return {
                "url": EXPLORER_URL,
                "outcome": "unavailable",
                "http_status": None,
                "content_type": None,
                "error": str(error),
            }
        final_host = urlparse(response.final_url).hostname
        if final_host != "cpiexplorer.knbs.or.ke":
            return {
                "url": EXPLORER_URL,
                "outcome": "redirected_outside_official_host",
                "http_status": response.status,
                "content_type": response.content_type,
            }
        return {
            "url": EXPLORER_URL,
            "outcome": "homepage_accessible",
            "http_status": response.status,
            "content_type": response.content_type,
        }

    def _get_html(self, url: str) -> str:
        if not _is_official_knbs_url(url):
            raise KNBSHTTPError("HTML URL is not on an official KNBS host")
        try:
            response = self.http.get(
                url,
                headers={"Accept": "text/html"},
                max_bytes=MAX_HTML_BYTES,
            )
        except (HTTPStatusError, HTTPRequestError, HTTPResponseTooLarge) as error:
            raise KNBSHTTPError(
                f"Unable to retrieve KNBS page {url}: {error}"
            ) from error
        if not _is_official_knbs_url(response.final_url):
            raise KNBSHTTPError("KNBS page redirected outside the official KNBS host")
        media_type = (
            response.content_type.split(";", 1)[0].strip().lower()
            if response.content_type
            else ""
        )
        if media_type not in {"text/html", "application/xhtml+xml"}:
            raise KNBSHTTPError(
                f"Expected an HTML listing, received {response.content_type!r}"
            )
        return response.body.decode("utf-8", errors="replace")


def _parse_page(html: str) -> _PageParser:
    parser = _PageParser()
    parser.feed(html)
    return parser


def _is_official_knbs_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in {
        "www.knbs.or.ke",
        "knbs.or.ke",
    }


def _validate_reference_month(reference_month: str) -> None:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", reference_month):
        raise ValueError("Reference months must use YYYY-MM format")


def _publication_date(page: _PageParser) -> tuple[str | None, str | None]:
    candidates = [
        ("article:published_time", page.metadata.get("article:published_time")),
        ("datePublished", page.metadata.get("datepublished")),
        ("time[datetime]", page.dates[0] if page.dates else None),
    ]
    for basis, candidate in candidates:
        if candidate is None:
            continue
        try:
            value = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            continue
        return value.date().isoformat(), basis
    return None, None
