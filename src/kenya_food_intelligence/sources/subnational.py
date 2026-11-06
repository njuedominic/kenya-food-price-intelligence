"""Discover and download Kenya-specific World Bank RTFP profile files."""

from __future__ import annotations

import json
import re
import ssl
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlparse
from zipfile import BadZipFile, ZipFile

import truststore

from kenya_food_intelligence.sources.http import (
    HTTPClient,
    HTTPDownloadResponse,
    HTTPRequestError,
    HTTPResponseTooLarge,
    HTTPStatusError,
)

STUDY_ID = "KEN_2021_RTFP_v02_M"
CATALOG_URL = "https://microdata.worldbank.org/catalog/6167"
FILES_URL = f"https://microdata.worldbank.org/api/downloads/{STUDY_ID}/files?type=data"
TABLE_INFO_URL = (
    "https://microdata.worldbank.org/api/tables/info/fcv/"
    "wld_2021_rtfp_v02_m?data_dictionary=true"
)
USER_AGENT = "kenya-food-price-intelligence-subnational-profiler/0.1"
MAX_ARCHIVE_BYTES = 50_000_000
MAX_MEMBER_BYTES = 250_000_000
MAX_ARCHIVE_MEMBERS = 10
EXPECTED_CONTENT_TYPES = {
    "application/zip",
    "application/x-zip-compressed",
    "application/octet-stream",
}
_VERSION_RE = re.compile(r"_(20\d{2}-\d{2}-\d{2})\.zip$")
_RESOURCE_PREFIXES = {
    "market_data": "KEN_RTFP_mkt_",
    "model_details": "KEN_RTP_details_mkt_",
    "ticker_info": "KEN_RTP_ticker_info_mkt_",
}


class SubnationalSourceError(Exception):
    """A World Bank RTFP source or downloaded archive is invalid."""


@dataclass(frozen=True)
class RTFPResource:
    """A Kenya-specific archive advertised by the official catalog."""

    kind: str
    filename: str
    version: str
    download_url: str
    size_bytes: int | None
    description: str


@dataclass(frozen=True)
class DownloadedRTFPResource:
    """A streamed archive and its retrieval provenance."""

    resource: RTFPResource
    path: Path
    retrieved_at_utc: str
    status: int
    content_type: str | None
    size_bytes: int
    checksum_sha256: str
    final_url: str
    last_modified: str | None
    etag: str | None


class WorldBankRTFPClient:
    """Fetch current Kenya-only RTFP archives with bounded HTTP requests."""

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

    def discover_resources(self) -> tuple[str, dict[str, RTFPResource]]:
        """Resolve the latest same-version data, model, and ticker archives."""
        try:
            response = self.http.get(
                FILES_URL,
                headers={"Accept": "application/json"},
                max_bytes=2_000_000,
            )
            payload = json.loads(response.body)
        except (HTTPStatusError, HTTPRequestError, HTTPResponseTooLarge) as error:
            raise SubnationalSourceError(
                f"Unable to discover World Bank RTFP files: {error}"
            ) from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SubnationalSourceError(
                f"World Bank RTFP file listing is not valid JSON: {error}"
            ) from error

        if not isinstance(payload, dict):
            raise SubnationalSourceError(
                "World Bank RTFP file listing must be a JSON object"
            )
        if response.status != 200 or payload.get("status") != "success":
            raise SubnationalSourceError(
                f"World Bank RTFP file listing returned HTTP {response.status}"
            )
        files = payload.get("files")
        if not isinstance(files, list):
            raise SubnationalSourceError("World Bank RTFP listing has no file list")

        candidates: dict[str, dict[str, dict[str, object]]] = {
            kind: {} for kind in _RESOURCE_PREFIXES
        }
        for item in files:
            if not isinstance(item, dict):
                continue
            filename = item.get("filename")
            if not isinstance(filename, str):
                continue
            if (
                PurePosixPath(filename).name != filename
                or PureWindowsPath(filename).name != filename
                or not filename.endswith(".zip")
            ):
                raise SubnationalSourceError(
                    f"Unsafe World Bank RTFP filename: {filename!r}"
                )
            match = _VERSION_RE.search(filename)
            if match is None:
                continue
            kind = next(
                (
                    name
                    for name, prefix in _RESOURCE_PREFIXES.items()
                    if filename.startswith(prefix)
                ),
                None,
            )
            links = item.get("links")
            download_url = links.get("download") if isinstance(links, dict) else None
            if (
                kind is None
                or not isinstance(download_url, str)
                or item.get("data_access_type") != "open"
            ):
                continue
            _validate_download_url(download_url)
            candidates[kind][match.group(1)] = item

        common_versions = set.intersection(
            *(set(items) for items in candidates.values())
        )
        if not common_versions:
            raise SubnationalSourceError(
                "No same-version Kenya RTFP data, model, and ticker files "
                "were found in the official listing"
            )
        version = max(common_versions)
        resources: dict[str, RTFPResource] = {}
        for kind, by_version in candidates.items():
            item = by_version[version]
            try:
                size = int(item["file_size_bytes"])
            except (KeyError, TypeError, ValueError):
                size = None
            resources[kind] = RTFPResource(
                kind=kind,
                filename=str(item["filename"]),
                version=version,
                download_url=str(item["links"]["download"]),
                size_bytes=size,
                description=str(item.get("description", "")),
            )
        return version, resources

    def fetch_table_metadata(self) -> dict[str, object]:
        """Read the official table dictionary without downloading global data."""
        try:
            response = self.http.get(
                TABLE_INFO_URL,
                headers={"Accept": "application/json"},
                max_bytes=1_000_000,
            )
            payload = json.loads(response.body)
        except (HTTPStatusError, HTTPRequestError, HTTPResponseTooLarge) as error:
            raise SubnationalSourceError(
                f"Unable to retrieve World Bank RTFP table metadata: {error}"
            ) from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SubnationalSourceError(
                f"World Bank RTFP table metadata is not valid JSON: {error}"
            ) from error
        if not isinstance(payload, dict):
            raise SubnationalSourceError(
                "World Bank RTFP metadata must be a JSON object"
            )
        if response.status != 200 or payload.get("status") != "success":
            raise SubnationalSourceError(
                f"World Bank RTFP metadata returned HTTP {response.status}"
            )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise SubnationalSourceError("World Bank RTFP metadata has no result")
        return result

    def download_resources(
        self,
        resources: dict[str, RTFPResource],
        output_dir: Path,
    ) -> dict[str, DownloadedRTFPResource]:
        """Stream each current Kenya archive and verify ZIP paths and CRCs."""
        output_dir.mkdir(parents=True, exist_ok=True)
        downloaded: dict[str, DownloadedRTFPResource] = {}
        for kind, resource in resources.items():
            path = output_dir / resource.filename
            try:
                response = self.http.download_to(
                    resource.download_url,
                    path,
                    headers={"Accept": "application/zip, application/octet-stream"},
                    max_bytes=MAX_ARCHIVE_BYTES,
                )
            except (
                HTTPStatusError,
                HTTPRequestError,
                HTTPResponseTooLarge,
            ) as error:
                raise SubnationalSourceError(
                    f"Unable to download {resource.filename}: {error}"
                ) from error
            try:
                _validate_download_response(response, resource.download_url)
                expected_csv = f"{Path(resource.filename).stem}.csv"
                validate_zip(path, expected_csv)
            except SubnationalSourceError:
                path.unlink(missing_ok=True)
                raise
            downloaded[kind] = DownloadedRTFPResource(
                resource=resource,
                path=path,
                retrieved_at_utc=datetime.now(UTC).isoformat(),
                status=response.status,
                content_type=response.content_type,
                size_bytes=response.size_bytes,
                checksum_sha256=response.checksum_sha256,
                final_url=response.final_url,
                last_modified=response.headers.get("Last-Modified"),
                etag=response.headers.get("ETag"),
            )
        return downloaded


def validate_zip(path: Path, expected_csv: str) -> list[str]:
    """Check archive integrity and safe members without extracting any files."""
    try:
        with ZipFile(path) as archive:
            members = archive.infolist()
            if not members or len(members) > MAX_ARCHIVE_MEMBERS:
                raise SubnationalSourceError(
                    "RTFP archive has an empty or excessive member list"
                )
            names: set[str] = set()
            total_size = 0
            for member in members:
                _validate_member_name(member.filename)
                if member.filename in names:
                    raise SubnationalSourceError(
                        f"RTFP archive has a duplicate member: {member.filename}"
                    )
                names.add(member.filename)
                if member.file_size > MAX_MEMBER_BYTES:
                    raise SubnationalSourceError(
                        f"RTFP archive member exceeds size limit: {member.filename}"
                    )
                total_size += member.file_size
                if total_size > MAX_MEMBER_BYTES:
                    raise SubnationalSourceError(
                        "RTFP archive exceeds total uncompressed size limit"
                    )
            if expected_csv not in names:
                raise SubnationalSourceError(
                    f"RTFP archive is missing expected CSV {expected_csv!r}"
                )
            corrupt_member = archive.testzip()
            if corrupt_member is not None:
                raise SubnationalSourceError(
                    f"RTFP archive CRC check failed for {corrupt_member!r}"
                )
            return sorted(names)
    except SubnationalSourceError:
        raise
    except (BadZipFile, EOFError, OSError, RuntimeError, zlib.error) as error:
        raise SubnationalSourceError(f"Invalid RTFP ZIP archive: {error}") from error


def _validate_download_response(
    response: HTTPDownloadResponse, source_url: str
) -> None:
    content_type = (response.content_type or "").split(";", 1)[0].strip().lower()
    if response.status != 200 or content_type not in EXPECTED_CONTENT_TYPES:
        raise SubnationalSourceError(
            f"Expected an HTTP 200 ZIP from {source_url}; got "
            f"HTTP {response.status} with {response.content_type!r}"
        )


def _validate_download_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "microdata.worldbank.org":
        raise SubnationalSourceError(f"Refusing a non-World Bank download URL: {url}")


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
        raise SubnationalSourceError(f"Unsafe RTFP ZIP member path: {name!r}")
