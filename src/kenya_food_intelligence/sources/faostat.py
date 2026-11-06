"""Download the official FAOSTAT Producer Prices domain archive."""

from __future__ import annotations

import ssl
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from zipfile import BadZipFile, ZipFile

import truststore

from kenya_food_intelligence.sources.http import (
    HTTPClient,
    HTTPDownloadResponse,
    HTTPRequestError,
    HTTPResponseTooLarge,
    HTTPStatusError,
)

DATASET_ID = "47c17894-8ca1-4bd0-ba4d-6078e919e9b1"
DATASET_URL = f"https://data.fao.org/catalog/dataset/{DATASET_ID}"
FAOSTAT_DOMAIN_URL = "https://www.fao.org/faostat/en/#data/PP"
DOWNLOAD_URL = (
    "https://bulks-faostat.fao.org/production/Prices_E_All_Data_(Normalized).zip"
)
DATA_CSV_MEMBER = "Prices_E_All_Data_(Normalized).csv"
MAX_ARCHIVE_BYTES = 50_000_000
MAX_MEMBER_BYTES = 1_000_000_000
MAX_ARCHIVE_MEMBERS = 20
USER_AGENT = "kenya-food-price-intelligence-faostat-profiler/0.1"
EXPECTED_CONTENT_TYPES = {
    "application/zip",
    "application/x-zip-compressed",
    "application/octet-stream",
}


class FaostatError(Exception):
    """Base exception for FAOSTAT download and archive failures."""


class FaostatHTTPError(FaostatError):
    """An FAOSTAT request failed or returned an unsuccessful status."""


class FaostatArchiveError(FaostatError):
    """The downloaded archive is invalid or unsafe to inspect."""


@dataclass(frozen=True)
class DatasetSource:
    dataset_id: str
    dataset_url: str
    domain_url: str
    download_url: str
    data_member: str


@dataclass(frozen=True)
class DownloadedDataset:
    source: DatasetSource
    path: Path
    retrieved_at_utc: str
    http_status: int
    content_type: str | None
    file_size_bytes: int
    checksum_sha256: str
    last_modified: str | None
    etag: str | None


def discover_dataset() -> DatasetSource:
    """Return the catalogued Producer Prices domain download descriptor."""
    return DatasetSource(
        dataset_id=DATASET_ID,
        dataset_url=DATASET_URL,
        domain_url=FAOSTAT_DOMAIN_URL,
        download_url=DOWNLOAD_URL,
        data_member=DATA_CSV_MEMBER,
    )


class FAOSTATClient:
    """Download and validate the bounded, normalized Producer Prices ZIP."""

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

    def download_dataset(self, output_dir: Path) -> DownloadedDataset:
        """Stream the official Producer Prices archive and validate its ZIP."""
        source = discover_dataset()
        output_dir.mkdir(parents=True, exist_ok=True)
        archive_path = output_dir / Path(source.download_url).name
        try:
            response = self.http.download_to(
                source.download_url,
                archive_path,
                headers={"Accept": "application/zip, application/octet-stream"},
                max_bytes=MAX_ARCHIVE_BYTES,
            )
        except HTTPStatusError as error:
            raise FaostatHTTPError(
                f"FAOSTAT returned HTTP {error.status} for {source.download_url}"
            ) from error
        except HTTPRequestError as error:
            raise FaostatHTTPError(str(error)) from error
        except HTTPResponseTooLarge as error:
            raise FaostatArchiveError(str(error)) from error

        _validate_download_response(response, source.download_url)
        _validate_archive(archive_path, source.data_member)
        return DownloadedDataset(
            source=source,
            path=archive_path,
            retrieved_at_utc=datetime.now(UTC).isoformat(),
            http_status=response.status,
            content_type=response.content_type,
            file_size_bytes=response.size_bytes,
            checksum_sha256=response.checksum_sha256,
            last_modified=response.headers.get("Last-Modified"),
            etag=response.headers.get("ETag"),
        )


def validate_archive(path: Path, data_member: str = DATA_CSV_MEMBER) -> list[str]:
    """Validate ZIP integrity, safe member paths, and the expected CSV member."""
    return _validate_archive(path, data_member)


def _validate_download_response(
    response: HTTPDownloadResponse, source_url: str
) -> None:
    if response.status != 200:
        raise FaostatHTTPError(
            f"FAOSTAT returned HTTP {response.status} for {source_url}"
        )
    content_type = (response.content_type or "").split(";", 1)[0].strip().lower()
    if content_type not in EXPECTED_CONTENT_TYPES:
        raise FaostatArchiveError(
            f"Expected a ZIP download from {source_url}; got {response.content_type!r}"
        )


def _validate_archive(path: Path, data_member: str) -> list[str]:
    if not path.is_file():
        raise FaostatArchiveError(f"Dataset archive does not exist: {path}")
    with path.open("rb") as archive_file:
        if archive_file.read(4)[:2] != b"PK":
            raise FaostatArchiveError("Downloaded file does not have a ZIP signature")

    try:
        with ZipFile(path) as archive:
            members = archive.infolist()
            if not members:
                raise FaostatArchiveError("Downloaded ZIP archive is empty")
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise FaostatArchiveError(
                    f"ZIP archive exceeds the {MAX_ARCHIVE_MEMBERS}-member limit"
                )
            seen_names: set[str] = set()
            total_uncompressed_bytes = 0
            for member in members:
                _validate_member_name(member.filename)
                if member.filename in seen_names:
                    raise FaostatArchiveError(
                        f"ZIP archive contains a duplicate member: {member.filename}"
                    )
                seen_names.add(member.filename)
                if member.file_size > MAX_MEMBER_BYTES:
                    raise FaostatArchiveError(
                        f"ZIP member exceeds the size limit: {member.filename}"
                    )
                total_uncompressed_bytes += member.file_size
                if total_uncompressed_bytes > MAX_MEMBER_BYTES:
                    raise FaostatArchiveError(
                        "ZIP archive exceeds the total uncompressed size limit"
                    )
            if data_member not in seen_names:
                raise FaostatArchiveError(
                    f"Expected CSV member {data_member!r} is missing"
                )
            corrupt_member = archive.testzip()
            if corrupt_member is not None:
                raise FaostatArchiveError(
                    f"ZIP CRC check failed for member {corrupt_member!r}"
                )
            return [member.filename for member in members]
    except (BadZipFile, EOFError, OSError, RuntimeError, zlib.error) as error:
        raise FaostatArchiveError(
            f"Downloaded file is not a valid ZIP: {error}"
        ) from error


def _validate_member_name(name: str) -> None:
    normalized = name.replace("\\", "/")
    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(name)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or ".." in posix_path.parts
    ):
        raise FaostatArchiveError(f"Unsafe ZIP member path: {name!r}")
