"""Download the official FAOSTAT Producer Prices domain archive."""

from __future__ import annotations

import ssl
import stat
import unicodedata
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlparse
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile

import truststore

from kenya_food_intelligence.sources.http import (
    HTTPClient,
    HTTPDownloadResponse,
    HTTPRequestError,
    HTTPResponseLengthMismatch,
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
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 100
ARCHIVE_READ_CHUNK_BYTES = 64 * 1024
USER_AGENT = "kenya-food-price-intelligence-faostat-profiler/0.1"
SOURCE_ID = "faostat_producer_prices_normalized"
ARCHIVE_FILENAME = "Prices_E_All_Data_(Normalized).zip"
EXPECTED_ARCHIVE_MEMBERS = (
    DATA_CSV_MEMBER,
    "Prices_E_AreaCodes.csv",
    "Prices_E_Elements.csv",
    "Prices_E_Flags.csv",
    "Prices_E_ItemCodes.csv",
)
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


class FaostatArchiveSecurityError(FaostatArchiveError):
    """The archive contains an unsafe or unsupported member."""


class FaostatResourceLimitError(FaostatArchiveError):
    """The archive exceeds a configured compressed or decompressed limit."""


class FaostatArchiveIntegrityError(FaostatArchiveError):
    """The ZIP structure, member data, or CRC validation failed."""


@dataclass(frozen=True)
class ArchiveMemberValidation:
    name: str
    compressed_size_bytes: int
    uncompressed_size_bytes: int
    crc32: str
    validated_size_bytes: int
    validation: str = "crc_passed"

    def as_manifest(self) -> dict[str, str | int]:
        return {
            "name": self.name,
            "compressed_size_bytes": self.compressed_size_bytes,
            "uncompressed_size_bytes": self.uncompressed_size_bytes,
            "crc32": self.crc32,
            "validated_size_bytes": self.validated_size_bytes,
            "validation": self.validation,
        }


@dataclass(frozen=True)
class ArchiveValidation:
    member_count: int
    total_declared_uncompressed_bytes: int
    total_validated_uncompressed_bytes: int
    members: tuple[ArchiveMemberValidation, ...]

    def as_manifest(self) -> dict[str, object]:
        return {
            "member_count": self.member_count,
            "total_declared_uncompressed_bytes": (
                self.total_declared_uncompressed_bytes
            ),
            "total_validated_uncompressed_bytes": (
                self.total_validated_uncompressed_bytes
            ),
            "members": [member.as_manifest() for member in self.members],
        }


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
    archive_validation: ArchiveValidation | None = None


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
        max_download_bytes: int = MAX_ARCHIVE_BYTES,
        max_members: int = MAX_ARCHIVE_MEMBERS,
        max_member_uncompressed_bytes: int = MAX_MEMBER_BYTES,
        max_total_uncompressed_bytes: int = MAX_TOTAL_UNCOMPRESSED_BYTES,
    ) -> None:
        _validate_positive_limit("max_download_bytes", max_download_bytes)
        _validate_positive_limit("max_members", max_members)
        _validate_positive_limit(
            "max_member_uncompressed_bytes",
            max_member_uncompressed_bytes,
        )
        _validate_positive_limit(
            "max_total_uncompressed_bytes",
            max_total_uncompressed_bytes,
        )
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
        self.max_download_bytes = max_download_bytes
        self.max_members = max_members
        self.max_member_uncompressed_bytes = max_member_uncompressed_bytes
        self.max_total_uncompressed_bytes = max_total_uncompressed_bytes

    def download_dataset(self, output_dir: Path) -> DownloadedDataset:
        """Download to the profiling directory using its established filename."""
        source = discover_dataset()
        output_dir.mkdir(parents=True, exist_ok=True)
        return self._download_to(source, output_dir / ARCHIVE_FILENAME)

    def download_to(self, output_path: Path) -> DownloadedDataset:
        """Download to a caller-owned unique temporary path and validate it."""
        source = discover_dataset()
        if output_path.exists() or output_path.is_symlink():
            raise FaostatError("FAOSTAT download destination must not already exist")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        return self._download_to(source, output_path)

    def _download_to(
        self,
        source: DatasetSource,
        archive_path: Path,
    ) -> DownloadedDataset:
        try:
            response = self.http.download_to(
                source.download_url,
                archive_path,
                headers={"Accept": "application/zip, application/octet-stream"},
                max_bytes=self.max_download_bytes,
            )
        except HTTPStatusError as error:
            raise FaostatHTTPError(
                f"FAOSTAT returned HTTP {error.status} for {source.download_url}"
            ) from error
        except HTTPRequestError as error:
            raise FaostatHTTPError(str(error)) from error
        except HTTPResponseTooLarge as error:
            raise FaostatResourceLimitError(str(error)) from error
        except HTTPResponseLengthMismatch as error:
            raise FaostatHTTPError(str(error)) from error

        try:
            _validate_download_response(response, source.download_url)
            archive_validation = inspect_archive(
                archive_path,
                data_member=source.data_member,
                max_archive_bytes=self.max_download_bytes,
                max_members=self.max_members,
                max_member_uncompressed_bytes=self.max_member_uncompressed_bytes,
                max_total_uncompressed_bytes=self.max_total_uncompressed_bytes,
            )
        except FaostatError:
            archive_path.unlink(missing_ok=True)
            raise
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
            archive_validation=archive_validation,
        )


def validate_archive(path: Path, data_member: str = DATA_CSV_MEMBER) -> list[str]:
    """Validate ZIP integrity, safe member paths, and the expected CSV member."""
    validation = inspect_archive(path, data_member=data_member)
    return [member.name for member in validation.members]


def inspect_archive(
    path: Path,
    *,
    data_member: str = DATA_CSV_MEMBER,
    max_archive_bytes: int = MAX_ARCHIVE_BYTES,
    max_members: int = MAX_ARCHIVE_MEMBERS,
    max_member_uncompressed_bytes: int = MAX_MEMBER_BYTES,
    max_total_uncompressed_bytes: int = MAX_TOTAL_UNCOMPRESSED_BYTES,
) -> ArchiveValidation:
    """Inspect every ZIP member in bounded chunks without extracting it."""
    for name, value in (
        ("max_archive_bytes", max_archive_bytes),
        ("max_members", max_members),
        ("max_member_uncompressed_bytes", max_member_uncompressed_bytes),
        ("max_total_uncompressed_bytes", max_total_uncompressed_bytes),
    ):
        _validate_positive_limit(name, value)
    if not path.is_file() or path.is_symlink():
        raise FaostatArchiveError("Dataset archive must be a regular file")
    try:
        archive_size = path.stat().st_size
    except OSError as error:
        raise FaostatArchiveError(f"Unable to stat dataset archive: {error}") from error
    if archive_size <= 0:
        raise FaostatArchiveIntegrityError("Downloaded ZIP archive is empty")
    if archive_size > max_archive_bytes:
        raise FaostatResourceLimitError(
            f"ZIP archive exceeds the {max_archive_bytes}-byte limit"
        )
    with path.open("rb") as archive_file:
        if archive_file.read(4)[:2] != b"PK":
            raise FaostatArchiveIntegrityError(
                "Downloaded file does not have a ZIP signature"
            )

    try:
        with ZipFile(path) as archive:
            members = archive.infolist()
            if not members:
                raise FaostatArchiveIntegrityError(
                    "Downloaded ZIP archive has no members"
                )
            if len(members) > max_members:
                raise FaostatResourceLimitError(
                    f"ZIP archive exceeds the {max_members}-member limit"
                )
            normalized_names: dict[str, str] = {}
            total_uncompressed_bytes = 0
            for member in members:
                _validate_member(member)
                normalized = unicodedata.normalize(
                    "NFC",
                    member.filename.replace("\\", "/"),
                ).casefold()
                if normalized in normalized_names:
                    raise FaostatArchiveSecurityError(
                        f"ZIP archive contains ambiguous duplicate member names: "
                        f"{normalized_names[normalized]!r} and {member.filename!r}"
                    )
                normalized_names[normalized] = member.filename
                if member.file_size > max_member_uncompressed_bytes:
                    raise FaostatResourceLimitError(
                        f"ZIP member exceeds the size limit: {member.filename}"
                    )
                total_uncompressed_bytes += member.file_size
                if total_uncompressed_bytes > max_total_uncompressed_bytes:
                    raise FaostatResourceLimitError(
                        "ZIP archive exceeds the total uncompressed size limit"
                    )
            names = [member.filename for member in members]
            if data_member not in names:
                raise FaostatArchiveIntegrityError(
                    f"Expected CSV member {data_member!r} is missing"
                )
            expected_members = set(EXPECTED_ARCHIVE_MEMBERS)
            if set(names) != expected_members:
                unexpected = sorted(set(names) - expected_members)
                missing = sorted(expected_members - set(names))
                raise FaostatArchiveIntegrityError(
                    "Unexpected FAOSTAT archive structure "
                    f"(missing={missing}, unexpected={unexpected})"
                )
            validated_members: list[ArchiveMemberValidation] = []
            total_validated_bytes = 0
            for member in members:
                actual_member_bytes = 0
                with archive.open(member, "r") as contents:
                    while True:
                        remaining = min(
                            max_member_uncompressed_bytes - actual_member_bytes,
                            max_total_uncompressed_bytes - total_validated_bytes,
                        )
                        chunk = contents.read(
                            min(ARCHIVE_READ_CHUNK_BYTES, remaining + 1)
                        )
                        if not chunk:
                            break
                        actual_member_bytes += len(chunk)
                        total_validated_bytes += len(chunk)
                        if actual_member_bytes > max_member_uncompressed_bytes:
                            raise FaostatResourceLimitError(
                                "ZIP member expands beyond the limit: "
                                f"{member.filename}"
                            )
                        if total_validated_bytes > max_total_uncompressed_bytes:
                            raise FaostatResourceLimitError(
                                "ZIP archive expands beyond the total validation limit"
                            )
                if actual_member_bytes != member.file_size:
                    raise FaostatArchiveIntegrityError(
                        f"ZIP member size does not match its directory entry: "
                        f"{member.filename}"
                    )
                validated_members.append(
                    ArchiveMemberValidation(
                        name=member.filename,
                        compressed_size_bytes=member.compress_size,
                        uncompressed_size_bytes=member.file_size,
                        crc32=f"{member.CRC:08x}",
                        validated_size_bytes=actual_member_bytes,
                    )
                )
            return ArchiveValidation(
                member_count=len(members),
                total_declared_uncompressed_bytes=total_uncompressed_bytes,
                total_validated_uncompressed_bytes=total_validated_bytes,
                members=tuple(validated_members),
            )
    except FaostatError:
        raise
    except (
        BadZipFile,
        EOFError,
        OSError,
        RuntimeError,
        ValueError,
        NotImplementedError,
        zlib.error,
    ) as error:
        raise FaostatArchiveIntegrityError(
            f"Downloaded file is not a valid, readable ZIP: {error}"
        ) from error


def _validate_download_response(
    response: HTTPDownloadResponse, source_url: str
) -> None:
    if response.status != 200:
        raise FaostatHTTPError(
            f"FAOSTAT returned HTTP {response.status} for {source_url}"
        )
    if not _is_official_download_url(response.final_url):
        raise FaostatHTTPError(
            f"FAOSTAT redirected to an unapproved URL: {response.final_url}"
        )
    content_type = (response.content_type or "").split(";", 1)[0].strip().lower()
    if content_type not in EXPECTED_CONTENT_TYPES:
        raise FaostatArchiveError(
            f"Expected a ZIP download from {source_url}; got {response.content_type!r}"
        )
    if response.size_bytes <= 0:
        raise FaostatArchiveIntegrityError("Downloaded ZIP archive is empty")


def _is_official_download_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and (parsed.hostname == "fao.org" or parsed.hostname.endswith(".fao.org"))
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
    )


def _validate_member(member: object) -> None:
    name = member.filename
    if not isinstance(name, str) or not name or "\x00" in name:
        raise FaostatArchiveSecurityError("ZIP member name is empty or malformed")
    if "\\" in name:
        raise FaostatArchiveSecurityError(
            f"Unsafe Windows-style ZIP member path: {name!r}"
        )
    posix_path = PurePosixPath(name)
    windows_path = PureWindowsPath(name)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or any(part in {"", ".", ".."} for part in name.split("/"))
        or name.startswith(("//", "\\\\"))
    ):
        raise FaostatArchiveSecurityError(f"Unsafe ZIP member path: {name!r}")
    if member.is_dir():
        raise FaostatArchiveSecurityError(f"Unsupported ZIP directory member: {name!r}")
    if member.flag_bits & 0x1:
        raise FaostatArchiveSecurityError(
            f"Encrypted ZIP member is not supported: {name!r}"
        )
    mode = (member.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if file_type not in {0, stat.S_IFREG}:
        raise FaostatArchiveSecurityError(f"Unsupported ZIP member type: {name!r}")
    if member.compress_type not in {ZIP_STORED, ZIP_DEFLATED}:
        raise FaostatArchiveSecurityError(
            f"Unsupported ZIP compression method: {name!r}"
        )


def _validate_positive_limit(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
