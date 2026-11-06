"""Discover and download official KNBS CPI publication PDFs."""

from __future__ import annotations

import hashlib
import re
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

import truststore

from kenya_food_intelligence.sources.http import (
    HTTPClient,
    HTTPDownloadResponse,
    HTTPRequestError,
    HTTPResponseTooLarge,
    HTTPStatusError,
)

LISTING_URL = "https://www.knbs.or.ke/cpi-and-inflation-rates/"
EXPLORER_URL = "https://cpiexplorer.knbs.or.ke/"
MAX_HTML_BYTES = 5_000_000
MAX_PDF_BYTES = 40_000_000
MAX_DOCUMENTS_PER_RUN = 12
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


class AmbiguousPublicationError(KNBSError):
    """The official listing has multiple PDF candidates for a reference month."""

    def __init__(self, reference_month: str, candidates: tuple[str, ...]) -> None:
        self.reference_month = reference_month
        self.candidates = candidates
        super().__init__(
            f"Multiple official PDF candidates for {reference_month}: "
            + ", ".join(candidates)
        )


@dataclass(frozen=True)
class ReportLink:
    reference_month: str
    title: str | None
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
        max_pdf_bytes: int = MAX_PDF_BYTES,
    ) -> None:
        if (
            isinstance(max_pdf_bytes, bool)
            or not isinstance(max_pdf_bytes, int)
            or max_pdf_bytes <= 0
        ):
            raise ValueError("max_pdf_bytes must be a positive integer")
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
        self.max_pdf_bytes = max_pdf_bytes

    def discover_reports(self, reference_months: list[str]) -> dict[str, ReportLink]:
        """Find month-specific PDF URLs in the official KNBS listing."""
        if not reference_months or len(reference_months) > MAX_DOCUMENTS_PER_RUN:
            raise ValueError(
                f"Request between 1 and {MAX_DOCUMENTS_PER_RUN} reference months"
            )
        for month in reference_months:
            _validate_reference_month(month)
        if len(set(reference_months)) != len(reference_months):
            raise ValueError("Reference months must not contain duplicates")

        listing = self._get_html(LISTING_URL)
        parsed_listing = _parse_page(listing)
        discovered: dict[str, ReportLink] = {}
        pdf_candidates: dict[str, dict[str, tuple[str, str]]] = {
            month: {} for month in reference_months
        }
        page_candidates: dict[str, dict[str, str]] = {
            month: {} for month in reference_months
        }
        titles: dict[str, str] = {}
        for href, link_text in parsed_listing.anchors:
            if not _is_official_knbs_url(href):
                continue
            lowered_url = unquote(href).lower()
            lowered_text = link_text.lower()
            if not _is_cpi_publication_link(lowered_url, lowered_text):
                continue
            for reference_month in reference_months:
                month_name = MONTH_NAMES[int(reference_month[5:7]) - 1]
                if not _contains_reference_month(
                    f"{lowered_url} {lowered_text}",
                    month_name,
                    reference_month[:4],
                ):
                    continue
                if urlparse(href).path.lower().endswith(".pdf"):
                    pdf_candidates[reference_month][href] = (link_text, href)
                elif "/reports/" in urlparse(href).path.lower():
                    page_candidates[reference_month][href] = link_text
                    if re.search(
                        r"consumer price|inflation rates|\bCPI\b", link_text, re.I
                    ):
                        titles[reference_month] = link_text

        for reference_month in reference_months:
            candidates = pdf_candidates[reference_month]
            page_links = page_candidates[reference_month]
            if len(candidates) > 1 or len(page_links) > 1:
                raise AmbiguousPublicationError(
                    reference_month,
                    tuple(sorted(set(candidates) | set(page_links))),
                )
            if not candidates:
                continue
            pdf_url, (pdf_link_text, _) = next(iter(candidates.items()))
            page_url = next(iter(page_links), None)
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
                title=titles.get(reference_month)
                or (
                    pdf_link_text
                    if re.search(
                        r"consumer price|inflation rates|\bCPI\b",
                        pdf_link_text,
                        re.I,
                    )
                    else None
                ),
                document_url=pdf_url,
                publication_page_url=page_url,
                publication_date=publication_date,
                publication_date_basis=publication_date_basis,
            )
        return discovered

    def download_to(self, report: ReportLink, destination: Path) -> DownloadedReport:
        """Stream a discovered PDF to a temporary path under finite limits."""
        if not _is_official_knbs_url(report.document_url):
            raise KNBSDocumentError("Document URL is not on an official KNBS host")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            response: HTTPDownloadResponse = self.http.download_to(
                report.document_url,
                destination,
                headers={"Accept": "application/pdf"},
                max_bytes=self.max_pdf_bytes,
            )
        except HTTPStatusError as error:
            raise KNBSHTTPError(
                f"KNBS returned HTTP {error.status} for {report.document_url}"
            ) from error
        except HTTPRequestError as error:
            raise KNBSHTTPError(str(error)) from error
        except HTTPResponseTooLarge as error:
            raise KNBSDocumentError(str(error)) from error

        if response.status != 200:
            raise KNBSDocumentError(
                f"Expected HTTP 200 for PDF, received {response.status}"
            )
        if response.size_bytes > self.max_pdf_bytes:
            destination.unlink(missing_ok=True)
            raise KNBSDocumentError(f"PDF response exceeds {self.max_pdf_bytes} bytes")
        if not _is_official_knbs_url(response.final_url):
            destination.unlink(missing_ok=True)
            raise KNBSDocumentError(
                "PDF request redirected outside the official KNBS host"
            )
        media_type = (
            response.content_type.split(";", 1)[0].strip().lower()
            if response.content_type
            else ""
        )
        if media_type not in {"application/pdf", "application/octet-stream"}:
            destination.unlink(missing_ok=True)
            raise KNBSDocumentError(
                f"Expected a PDF content type, received {response.content_type!r}"
            )
        if response.size_bytes == 0:
            destination.unlink(missing_ok=True)
            raise KNBSDocumentError("Downloaded PDF is empty")
        try:
            with destination.open("rb") as file:
                signature = file.read(5)
        except OSError as error:
            raise KNBSDocumentError(
                f"Unable to read downloaded PDF: {error}"
            ) from error
        if signature != b"%PDF-":
            destination.unlink(missing_ok=True)
            raise KNBSDocumentError("Downloaded content does not have a PDF signature")

        return DownloadedReport(
            report=report,
            path=destination,
            downloaded_at_utc=datetime.now(UTC).isoformat(),
            http_status=response.status,
            content_type=response.content_type,
            file_size_bytes=response.size_bytes,
            checksum_sha256=response.checksum_sha256,
            last_modified=response.headers.get("Last-Modified"),
            etag=response.headers.get("ETag"),
        )

    def download(self, report: ReportLink, output_dir: Path) -> DownloadedReport:
        """Download a discovered PDF without following untrusted external links."""
        if not _is_official_knbs_url(report.document_url):
            raise KNBSDocumentError("Document URL is not on an official KNBS host")
        try:
            response = self.http.get(
                report.document_url,
                headers={"Accept": "application/pdf"},
                max_bytes=self.max_pdf_bytes,
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
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname in {"www.knbs.or.ke", "knbs.or.ke"}
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
    )


def _is_cpi_publication_link(url: str, text: str) -> bool:
    lowered = f"{url} {text}"
    return bool(re.search(r"\bcpi\b|consumer[- ]price|inflation", lowered, re.I))


def _contains_reference_month(value: str, month_name: str, year: str) -> bool:
    return bool(
        re.search(rf"\b{re.escape(month_name)}\b", value, re.I)
        and re.search(rf"\b{re.escape(year)}\b", value)
    )


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
