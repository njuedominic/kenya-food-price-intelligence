"""Immutable, content-addressed storage for Bronze source payloads."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kenya_food_intelligence.sources.world_bank import RawIndicatorPage

type JsonObject = dict[str, Any]

MANIFEST_SCHEMA_VERSION = 1
SOURCE_ID = "world_bank_indicators_api"
_COUNTRY_PATTERN = re.compile(r"[A-Z]{3}\Z")
_INDICATOR_PATTERN = re.compile(r"[A-Z0-9]+(?:\.[A-Z0-9]+)*\Z")
_SNAPSHOT_PATTERN = re.compile(r"sha256-[0-9a-f]{64}\Z")
_RUN_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_SOURCE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_PUBLICATION_PATTERN = re.compile(r"\d{4}-(0[1-9]|1[0-2])\Z")
_FILENAME_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}\Z")


class BronzeStorageError(Exception):
    """A Bronze snapshot or run record could not be safely stored."""


class SnapshotIntegrityError(BronzeStorageError):
    """Existing snapshot content is missing, corrupt, or inconsistent."""


class BronzeStore:
    """Publish immutable source snapshots and append-only ingestion runs."""

    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root

    def publish_snapshot(
        self,
        *,
        country_code: str,
        indicator_code: str,
        per_page: int,
        pages: tuple[RawIndicatorPage, ...],
    ) -> tuple[str, str, Path, JsonObject]:
        _validate_identity(country_code, indicator_code, per_page)
        if not pages:
            raise BronzeStorageError("Cannot publish a snapshot without response pages")
        if tuple(page.metadata.page for page in pages) != tuple(
            range(1, len(pages) + 1)
        ):
            raise BronzeStorageError("Response pages must be complete and ordered")

        page_descriptors = [
            {
                "page": page.metadata.page,
                "request_url": page.request_url,
                "sha256": hashlib.sha256(page.body).hexdigest(),
            }
            for page in pages
        ]
        descriptor: JsonObject = {
            "source_id": SOURCE_ID,
            "country_code": country_code,
            "indicator_code": indicator_code,
            "request_parameters": {"format": "json", "per_page": per_page},
            "pages": page_descriptors,
        }
        descriptor_bytes = _canonical_json(descriptor)
        digest = hashlib.sha256(descriptor_bytes).hexdigest()
        snapshot_id = f"sha256-{digest}"

        indicator_partition = (
            self.output_root / "bronze" / "world_bank" / f"indicator={indicator_code}"
        )
        snapshots_dir = indicator_partition / "snapshots"
        snapshot_dir = snapshots_dir / snapshot_id
        lock_path = (
            self.output_root
            / "manifests"
            / "world_bank"
            / "locks"
            / f"indicator={indicator_code}.lock"
        )
        try:
            snapshots_dir.mkdir(parents=True, exist_ok=True)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise BronzeStorageError(
                f"Unable to create World Bank Bronze directories: {error}"
            ) from error

        with _exclusive_lock(lock_path):
            try:
                _remove_abandoned_staging(snapshots_dir)
            except OSError as error:
                raise BronzeStorageError(
                    f"Unable to clean abandoned staging data: {error}"
                ) from error
            if snapshot_dir.exists() or snapshot_dir.is_symlink():
                manifest = _verify_snapshot(
                    snapshot_dir,
                    snapshot_id=snapshot_id,
                    country_code=country_code,
                    indicator_code=indicator_code,
                )
                return "unchanged", snapshot_id, snapshot_dir, manifest

            try:
                staging_dir = Path(
                    tempfile.mkdtemp(prefix=".staging-", dir=snapshots_dir)
                )
            except OSError as error:
                raise BronzeStorageError(
                    f"Unable to create a World Bank staging directory: {error}"
                ) from error
            try:
                _write_bytes(staging_dir / "descriptor.json", descriptor_bytes + b"\n")
                manifest_pages = []
                total_payload_bytes = 0
                total_observations = 0
                for page in pages:
                    filename = f"page-{page.metadata.page:04d}.json"
                    _write_bytes(staging_dir / filename, page.body)
                    size_bytes = len(page.body)
                    observation_count = len(page.observations)
                    total_payload_bytes += size_bytes
                    total_observations += observation_count
                    manifest_pages.append(
                        {
                            "page": page.metadata.page,
                            "filename": filename,
                            "request_url": page.request_url,
                            "content_type": page.content_type,
                            "sha256": hashlib.sha256(page.body).hexdigest(),
                            "size_bytes": size_bytes,
                            "observation_count": observation_count,
                        }
                    )

                first_metadata = pages[0].metadata
                if total_observations != first_metadata.total:
                    raise BronzeStorageError(
                        "Retrieved observations do not match the API-reported total"
                    )
                years = sorted(
                    {
                        str(observation["date"])
                        for page in pages
                        for observation in page.observations
                    }
                )
                indicator_description = next(
                    (
                        observation["indicator"]["value"]
                        for page in pages
                        for observation in page.observations
                    ),
                    None,
                )
                manifest: JsonObject = {
                    "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
                    "identity": {
                        "source_id": SOURCE_ID,
                        "country_code": country_code,
                        "indicator_code": indicator_code,
                        "snapshot_id": snapshot_id,
                    },
                    "source": {
                        "api_root": "https://api.worldbank.org/v2",
                        "dataset_identity": "World Bank Indicators API",
                        "indicator_description": indicator_description,
                        "request_parameters": descriptor["request_parameters"],
                        "request_urls": [page.request_url for page in pages],
                    },
                    "pagination": {
                        "api_page_count": first_metadata.pages,
                        "retrieved_page_count": len(pages),
                        "per_page": first_metadata.per_page,
                        "api_total_observations": first_metadata.total,
                    },
                    "reference_year_coverage": {
                        "minimum_year": years[0] if years else None,
                        "maximum_year": years[-1] if years else None,
                        "distinct_year_count": len(years),
                    },
                    "integrity": {
                        "pages": manifest_pages,
                        "total_payload_bytes": total_payload_bytes,
                        "total_observation_count": total_observations,
                        "content_derived_snapshot_digest": digest,
                    },
                    "validation": {
                        "status": "passed",
                        "valid_json_and_response_envelope": True,
                        "indicator_and_country_identity": True,
                        "pagination_metadata_consistent": True,
                        "complete_pagination": True,
                        "observation_count_matches_api_total": True,
                        "null_values_allowed": True,
                    },
                    "snapshot_created_at_utc": datetime.now(UTC).isoformat(),
                }
                _write_bytes(
                    staging_dir / "manifest.json",
                    _pretty_json(manifest),
                )
                _verify_snapshot(
                    staging_dir,
                    snapshot_id=snapshot_id,
                    country_code=country_code,
                    indicator_code=indicator_code,
                )
                _fsync_directory(staging_dir)
                os.rename(staging_dir, snapshot_dir)
                _fsync_directory(snapshots_dir)
            except OSError as error:
                raise BronzeStorageError(
                    f"Unable to publish World Bank Bronze snapshot: {error}"
                ) from error
            finally:
                if staging_dir.exists():
                    shutil.rmtree(staging_dir)

        return "created", snapshot_id, snapshot_dir, manifest

    def publish_binary_snapshot(
        self,
        *,
        source_id: str,
        publication_identity: str,
        payload_path: Path,
        filename: str,
        metadata: JsonObject,
        expected_sha256: str,
        expected_size_bytes: int,
    ) -> tuple[str, str, Path, JsonObject]:
        """Publish one content-addressed binary payload without buffering it."""
        _validate_binary_identity(source_id, publication_identity, filename)
        if payload_path.is_symlink() or not payload_path.is_file():
            raise BronzeStorageError("Binary snapshot payload is not a regular file")

        publication_dir = (
            self.output_root
            / "bronze"
            / source_id
            / f"publication={publication_identity}"
        )
        snapshots_dir = publication_dir / "snapshots"
        lock_path = (
            self.output_root
            / "manifests"
            / source_id
            / "locks"
            / f"publication={publication_identity}.lock"
        )
        try:
            snapshots_dir.mkdir(parents=True, exist_ok=True)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise BronzeStorageError(
                f"Unable to create {source_id} Bronze directories: {error}"
            ) from error

        with _exclusive_lock(lock_path):
            try:
                _remove_abandoned_staging(snapshots_dir)
                staging_dir = Path(
                    tempfile.mkdtemp(prefix=".staging-", dir=snapshots_dir)
                )
            except OSError as error:
                raise BronzeStorageError(
                    f"Unable to create binary snapshot staging directory: {error}"
                ) from error

            try:
                payload_digest, payload_size = _copy_and_hash(
                    payload_path,
                    staging_dir / filename,
                )
                if (
                    payload_digest != expected_sha256
                    or payload_size != expected_size_bytes
                ):
                    raise SnapshotIntegrityError(
                        "Downloaded payload changed before snapshot publication"
                    )
                descriptor: JsonObject = {
                    "descriptor_schema_version": 2,
                    "identity": {
                        "source_id": source_id,
                        "publication_identity": publication_identity,
                        "sha256": payload_digest,
                        "size_bytes": payload_size,
                    },
                    "provenance": {"filename": filename},
                }
                identity_bytes = _canonical_json(descriptor["identity"])
                descriptor_bytes = _canonical_json(descriptor)
                snapshot_id = "sha256-" + hashlib.sha256(identity_bytes).hexdigest()
                snapshot_dir = snapshots_dir / snapshot_id
                if snapshot_dir.exists() or snapshot_dir.is_symlink():
                    manifest = _verify_binary_snapshot(
                        snapshot_dir,
                        source_id=source_id,
                        publication_identity=publication_identity,
                        snapshot_id=snapshot_id,
                    )
                    return "unchanged", snapshot_id, snapshot_dir, manifest

                _write_bytes(
                    staging_dir / "descriptor.json",
                    descriptor_bytes + b"\n",
                )
                manifest: JsonObject = {
                    "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
                    "identity": {
                        "source_id": source_id,
                        "publication_identity": publication_identity,
                        "reference_month": publication_identity,
                        "snapshot_id": snapshot_id,
                    },
                    "source": metadata.get("source", {}),
                    "file": {
                        "filename": filename,
                        "content_type": metadata.get("content_type"),
                        "size_bytes": payload_size,
                        "sha256": payload_digest,
                        "page_count": metadata.get("page_count"),
                    },
                    "validation": metadata.get("validation", {}),
                    "attribution": metadata.get("attribution"),
                    "integrity": {
                        "content_derived_snapshot_digest": snapshot_id.removeprefix(
                            "sha256-"
                        )
                    },
                    "snapshot_created_at_utc": datetime.now(UTC).isoformat(),
                }
                _write_bytes(staging_dir / "manifest.json", _pretty_json(manifest))
                _verify_binary_snapshot(
                    staging_dir,
                    source_id=source_id,
                    publication_identity=publication_identity,
                    snapshot_id=snapshot_id,
                )
                _fsync_directory(staging_dir)
                os.rename(staging_dir, snapshot_dir)
                _fsync_directory(snapshots_dir)
                return "created", snapshot_id, snapshot_dir, manifest
            except SnapshotIntegrityError:
                raise
            except OSError as error:
                raise BronzeStorageError(
                    f"Unable to publish {source_id} Bronze snapshot: {error}"
                ) from error
            finally:
                if staging_dir.exists():
                    shutil.rmtree(staging_dir)

    def write_source_run_record(self, record: JsonObject) -> Path:
        """Atomically append a run record for a source without WB partitions."""
        source_id = record.get("source_id")
        run_id = record.get("ingestion_run_id")
        if (
            not isinstance(source_id, str)
            or _SOURCE_PATTERN.fullmatch(source_id) is None
        ):
            raise BronzeStorageError("Run record source ID is unsafe")
        if not isinstance(run_id, str) or _RUN_ID_PATTERN.fullmatch(run_id) is None:
            raise BronzeStorageError("Run ID must be a 32-character UUID hex value")
        if record.get("outcome") not in {"created", "unchanged", "failed"}:
            raise BronzeStorageError("Run record outcome is invalid")

        run_dir = self.output_root / "manifests" / source_id / "runs"
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise BronzeStorageError(
                f"Unable to create ingestion run-record directory: {error}"
            ) from error
        return _write_run_record_at(run_dir, run_id, record)

    def write_run_record(self, record: JsonObject) -> Path:
        """Atomically append one immutable run outcome."""
        country_code = record.get("country_code")
        indicator_code = record.get("indicator_code")
        run_id = record.get("ingestion_run_id")
        if (
            not isinstance(country_code, str)
            or not isinstance(indicator_code, str)
            or not isinstance(run_id, str)
        ):
            raise BronzeStorageError("Run record identity fields are invalid")
        _validate_identity(country_code, indicator_code, 1)
        if _RUN_ID_PATTERN.fullmatch(run_id) is None:
            raise BronzeStorageError("Run ID must be a 32-character UUID hex value")
        if record.get("outcome") not in {"created", "unchanged", "failed"}:
            raise BronzeStorageError("Run record outcome is invalid")

        run_dir = (
            self.output_root
            / "manifests"
            / "world_bank"
            / "runs"
            / f"indicator={indicator_code}"
        )
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise BronzeStorageError(
                f"Unable to create ingestion run-record directory: {error}"
            ) from error
        return _write_run_record_at(run_dir, run_id, record)


def _write_run_record_at(run_dir: Path, run_id: str, record: JsonObject) -> Path:
    target = run_dir / f"{run_id}.json"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=run_dir,
            prefix=f".{run_id}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            file.write(_pretty_json(record))
            file.flush()
            os.fsync(file.fileno())
        os.link(temporary_path, target)
        _fsync_directory(run_dir)
        return target
    except FileExistsError as error:
        raise BronzeStorageError(f"Run record already exists: {run_id}") from error
    except OSError as error:
        raise BronzeStorageError(
            f"Unable to publish ingestion run record: {error}"
        ) from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as error:
        raise BronzeStorageError(f"Unable to open ingestion lock: {error}") from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError as error:
        os.close(descriptor)
        raise BronzeStorageError(
            f"Unable to lock ingestion partition: {error}"
        ) from error
    try:
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _validate_identity(country_code: str, indicator_code: str, per_page: int) -> None:
    if _COUNTRY_PATTERN.fullmatch(country_code) is None:
        raise BronzeStorageError("Country code is not safe for a storage partition")
    if _INDICATOR_PATTERN.fullmatch(indicator_code) is None:
        raise BronzeStorageError("Indicator code is not safe for a storage partition")
    if isinstance(per_page, bool) or not 1 <= per_page <= 1000:
        raise BronzeStorageError("Page size must be between 1 and 1000")


def _validate_binary_identity(
    source_id: str,
    publication_identity: str,
    filename: str,
) -> None:
    if _SOURCE_PATTERN.fullmatch(source_id) is None:
        raise BronzeStorageError("Source ID is not safe for a storage partition")
    if _PUBLICATION_PATTERN.fullmatch(publication_identity) is None:
        raise BronzeStorageError("Publication identity must use YYYY-MM format")
    if _FILENAME_PATTERN.fullmatch(filename) is None or filename in {".", ".."}:
        raise BronzeStorageError("Binary payload filename is unsafe")


def _copy_and_hash(source: Path, destination: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size_bytes = 0
    with source.open("rb") as input_file, destination.open("xb") as output_file:
        while chunk := input_file.read(1024 * 1024):
            output_file.write(chunk)
            digest.update(chunk)
            size_bytes += len(chunk)
        output_file.flush()
        os.fsync(output_file.fileno())
    return digest.hexdigest(), size_bytes


def _verify_binary_snapshot(
    snapshot_dir: Path,
    *,
    source_id: str,
    publication_identity: str,
    snapshot_id: str,
) -> JsonObject:
    if _SNAPSHOT_PATTERN.fullmatch(snapshot_id) is None:
        raise SnapshotIntegrityError("Snapshot identifier is invalid")
    if snapshot_dir.is_symlink() or not snapshot_dir.is_dir():
        raise SnapshotIntegrityError(f"Snapshot directory is missing: {snapshot_dir}")
    descriptor_path = snapshot_dir / "descriptor.json"
    manifest_path = snapshot_dir / "manifest.json"
    if any(
        path.is_symlink() or not path.is_file()
        for path in (descriptor_path, manifest_path)
    ):
        raise SnapshotIntegrityError("Snapshot metadata is missing or unsafe")

    try:
        descriptor_bytes = descriptor_path.read_bytes()
        descriptor = json.loads(descriptor_bytes)
        manifest = json.loads(manifest_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SnapshotIntegrityError(
            f"Snapshot metadata is unreadable: {error}"
        ) from error
    if not isinstance(descriptor, dict) or not isinstance(manifest, dict):
        raise SnapshotIntegrityError("Snapshot metadata must be JSON objects")
    if descriptor_bytes != _canonical_json(descriptor) + b"\n":
        raise SnapshotIntegrityError("Snapshot descriptor is not canonical")
    identity_descriptor = descriptor.get("identity")
    provenance = descriptor.get("provenance")
    if (
        descriptor.get("descriptor_schema_version") != 2
        or not isinstance(identity_descriptor, dict)
        or not isinstance(provenance, dict)
    ):
        raise SnapshotIntegrityError("Snapshot descriptor schema is unsupported")
    filename = provenance.get("filename")
    if (
        not isinstance(filename, str)
        or _FILENAME_PATTERN.fullmatch(filename) is None
        or filename in {".", ".."}
    ):
        raise SnapshotIntegrityError("Snapshot descriptor filename is unsafe")
    payload_path = snapshot_dir / filename
    if payload_path.is_symlink() or not payload_path.is_file():
        raise SnapshotIntegrityError("Snapshot payload is missing or unsafe")
    expected_names = {"descriptor.json", "manifest.json", filename}
    if {path.name for path in snapshot_dir.iterdir()} != expected_names:
        raise SnapshotIntegrityError("Snapshot contains unexpected or missing files")

    try:
        payload_digest, payload_size = _hash_file(payload_path)
    except OSError as error:
        raise SnapshotIntegrityError(
            f"Snapshot payload is unreadable: {error}"
        ) from error
    expected_identity = {
        "source_id": source_id,
        "publication_identity": publication_identity,
        "sha256": payload_digest,
        "size_bytes": payload_size,
    }
    if identity_descriptor != expected_identity:
        raise SnapshotIntegrityError(
            "Snapshot payload checksum or descriptor is inconsistent"
        )
    if hashlib.sha256(
        _canonical_json(identity_descriptor)
    ).hexdigest() != snapshot_id.removeprefix("sha256-"):
        raise SnapshotIntegrityError("Snapshot identity digest does not match ID")
    identity = manifest.get("identity")
    if identity != {
        "source_id": source_id,
        "publication_identity": publication_identity,
        "reference_month": publication_identity,
        "snapshot_id": snapshot_id,
    }:
        raise SnapshotIntegrityError("Snapshot manifest identity is inconsistent")
    file_metadata = manifest.get("file")
    source_metadata = manifest.get("source")
    validation = manifest.get("validation")
    integrity = manifest.get("integrity")
    if not isinstance(file_metadata, dict) or any(
        file_metadata.get(key) != value
        for key, value in {
            "filename": filename,
            "sha256": payload_digest,
            "size_bytes": payload_size,
        }.items()
    ):
        raise SnapshotIntegrityError("Snapshot file metadata is inconsistent")
    if (
        manifest.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION
        or not isinstance(source_metadata, dict)
        or not isinstance(source_metadata.get("listing_url"), str)
        or not isinstance(source_metadata.get("document_url"), str)
        or not isinstance(validation, dict)
        or validation.get("status") != "passed"
        or validation.get("http_status_200") is not True
        or validation.get("pdf_signature") is not True
        or validation.get("eof_marker") is not True
        or validation.get("readable_pdf_structure") is not True
        or not isinstance(validation.get("page_count"), int)
        or isinstance(validation.get("page_count"), bool)
        or validation.get("page_count") < 1
        or not (
            validation.get("reference_month_match") is True
            or validation.get("reference_month_match") == "unverified"
        )
        or not (
            validation.get("source_title_month_match") is True
            or validation.get("source_title_month_match") == "unverified"
        )
        or file_metadata.get("page_count") != validation.get("page_count")
        or file_metadata.get("content_type")
        not in {"application/pdf", "application/octet-stream"}
        or not isinstance(integrity, dict)
        or integrity.get("content_derived_snapshot_digest")
        != snapshot_id.removeprefix("sha256-")
        or not isinstance(manifest.get("attribution"), str)
    ):
        raise SnapshotIntegrityError("Snapshot manifest validation is inconsistent")
    return manifest


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
            size_bytes += len(chunk)
    return digest.hexdigest(), size_bytes


def _remove_abandoned_staging(snapshots_dir: Path) -> None:
    for candidate in snapshots_dir.iterdir():
        if not candidate.name.startswith(".staging-"):
            continue
        if candidate.is_symlink():
            candidate.unlink()
        elif candidate.is_dir():
            shutil.rmtree(candidate)


def _verify_snapshot(
    snapshot_dir: Path,
    *,
    snapshot_id: str,
    country_code: str,
    indicator_code: str,
) -> JsonObject:
    if _SNAPSHOT_PATTERN.fullmatch(snapshot_id) is None:
        raise SnapshotIntegrityError("Snapshot identifier is invalid")
    if snapshot_dir.is_symlink() or not snapshot_dir.is_dir():
        raise SnapshotIntegrityError(f"Snapshot directory is missing: {snapshot_dir}")
    manifest_path = snapshot_dir / "manifest.json"
    descriptor_path = snapshot_dir / "descriptor.json"
    if (
        manifest_path.is_symlink()
        or descriptor_path.is_symlink()
        or not manifest_path.is_file()
        or not descriptor_path.is_file()
    ):
        raise SnapshotIntegrityError("Snapshot manifest or descriptor is missing")

    try:
        manifest_bytes = manifest_path.read_bytes()
        descriptor_bytes = descriptor_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        descriptor = json.loads(descriptor_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SnapshotIntegrityError(
            f"Snapshot metadata is unreadable: {error}"
        ) from error
    if not isinstance(manifest, dict) or not isinstance(descriptor, dict):
        raise SnapshotIntegrityError("Snapshot manifest and descriptor must be objects")
    if manifest.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION:
        raise SnapshotIntegrityError("Snapshot manifest schema version is unsupported")
    if descriptor_bytes != _canonical_json(descriptor) + b"\n":
        raise SnapshotIntegrityError("Snapshot descriptor is not canonical")
    descriptor_digest = hashlib.sha256(_canonical_json(descriptor)).hexdigest()
    if descriptor_digest != snapshot_id.removeprefix("sha256-"):
        raise SnapshotIntegrityError("Snapshot descriptor digest does not match ID")
    identity = manifest.get("identity")
    if not isinstance(identity, dict) or identity != {
        "source_id": SOURCE_ID,
        "country_code": country_code,
        "indicator_code": indicator_code,
        "snapshot_id": snapshot_id,
    }:
        raise SnapshotIntegrityError("Snapshot manifest identity is inconsistent")
    if (
        descriptor.get("source_id") != SOURCE_ID
        or descriptor.get("country_code") != country_code
        or descriptor.get("indicator_code") != indicator_code
    ):
        raise SnapshotIntegrityError("Snapshot descriptor identity is inconsistent")

    descriptor_pages = descriptor.get("pages")
    descriptor_parameters = descriptor.get("request_parameters")
    if not isinstance(descriptor_pages, list) or not isinstance(
        descriptor_parameters, dict
    ):
        raise SnapshotIntegrityError(
            "Snapshot descriptor request metadata is malformed"
        )
    source = manifest.get("source")
    if (
        not isinstance(source, dict)
        or source.get("api_root") != "https://api.worldbank.org/v2"
        or source.get("dataset_identity") != "World Bank Indicators API"
        or source.get("request_parameters") != descriptor_parameters
        or descriptor_parameters.get("format") != "json"
        or isinstance(descriptor_parameters.get("per_page"), bool)
        or not isinstance(descriptor_parameters.get("per_page"), int)
        or not 1 <= descriptor_parameters["per_page"] <= 1000
        or source.get("request_urls")
        != [
            page.get("request_url") if isinstance(page, dict) else None
            for page in descriptor_pages
        ]
    ):
        raise SnapshotIntegrityError("Snapshot source request metadata is inconsistent")
    integrity = manifest.get("integrity")
    manifest_pages = integrity.get("pages") if isinstance(integrity, dict) else None
    pagination = manifest.get("pagination")
    if (
        not isinstance(descriptor_pages, list)
        or not isinstance(manifest_pages, list)
        or not isinstance(pagination, dict)
        or len(descriptor_pages) != len(manifest_pages)
        or not descriptor_pages
        or pagination.get("retrieved_page_count") != len(descriptor_pages)
        or pagination.get("per_page") != descriptor_parameters.get("per_page")
        or pagination.get("api_total_observations") is None
        or pagination.get("api_page_count") is None
    ):
        raise SnapshotIntegrityError("Snapshot page metadata is incomplete")

    expected_names = {"manifest.json", "descriptor.json"}
    actual_payload_bytes = 0
    actual_observations = 0
    observation_years: set[str] = set()
    expected_description: str | None = None
    reference_metadata: dict[str, int] | None = None
    for expected_page, (source_page, manifest_page) in enumerate(
        zip(descriptor_pages, manifest_pages, strict=True), start=1
    ):
        if not isinstance(source_page, dict) or not isinstance(manifest_page, dict):
            raise SnapshotIntegrityError("Snapshot page metadata is malformed")
        filename = f"page-{expected_page:04d}.json"
        expected_names.add(filename)
        if manifest_page.get("filename") != filename:
            raise SnapshotIntegrityError("Snapshot page filenames are inconsistent")
        if (
            source_page.get("page") != expected_page
            or manifest_page.get("page") != expected_page
            or source_page.get("sha256") != manifest_page.get("sha256")
            or source_page.get("request_url") != manifest_page.get("request_url")
            or not isinstance(manifest_page.get("content_type"), str)
            or manifest_page["content_type"].split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise SnapshotIntegrityError("Snapshot page descriptor is inconsistent")
        page_path = snapshot_dir / filename
        if page_path.is_symlink() or not page_path.is_file():
            raise SnapshotIntegrityError(f"Snapshot payload is missing: {filename}")
        try:
            body = page_path.read_bytes()
            payload = json.loads(body)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SnapshotIntegrityError(
                f"Snapshot payload {filename} is unreadable: {error}"
            ) from error
        digest = hashlib.sha256(body).hexdigest()
        if (
            digest != source_page.get("sha256")
            or digest != manifest_page.get("sha256")
            or len(body) != manifest_page.get("size_bytes")
        ):
            raise SnapshotIntegrityError(
                f"Snapshot payload integrity failed: {filename}"
            )
        if (
            not isinstance(payload, list)
            or len(payload) != 2
            or not isinstance(payload[0], dict)
            or not (isinstance(payload[1], list) or payload[1] is None)
        ):
            raise SnapshotIntegrityError(
                f"Snapshot payload structure is invalid: {filename}"
            )
        metadata = payload[0]
        observations = payload[1] or []
        if (
            metadata.get("page") != expected_page
            or metadata.get("per_page") != pagination.get("per_page")
            or metadata.get("pages") != pagination.get("api_page_count")
            or metadata.get("total") != pagination.get("api_total_observations")
        ):
            raise SnapshotIntegrityError(
                f"Snapshot pagination is inconsistent: {filename}"
            )
        normalized_metadata = {
            "pages": metadata["pages"],
            "per_page": metadata["per_page"],
            "total": metadata["total"],
        }
        if reference_metadata is not None and normalized_metadata != reference_metadata:
            raise SnapshotIntegrityError("Snapshot page metadata changed")
        reference_metadata = normalized_metadata
        if len(observations) != manifest_page.get("observation_count"):
            raise SnapshotIntegrityError(
                f"Snapshot observation count is inconsistent: {filename}"
            )
        if observations and expected_description is None:
            first_observation = observations[0]
            if isinstance(first_observation, dict) and isinstance(
                first_observation.get("indicator"), dict
            ):
                description = first_observation["indicator"].get("value")
                if isinstance(description, str):
                    expected_description = description
        for observation in observations:
            if isinstance(observation, dict) and isinstance(
                observation.get("date"), str
            ):
                observation_years.add(observation["date"])
        actual_observations += len(observations)
        actual_payload_bytes += len(body)

    if {path.name for path in snapshot_dir.iterdir()} != expected_names:
        raise SnapshotIntegrityError("Snapshot contains unexpected files")
    if (
        actual_observations != integrity.get("total_observation_count")
        or actual_observations != pagination.get("api_total_observations")
        or actual_payload_bytes != integrity.get("total_payload_bytes")
        or integrity.get("content_derived_snapshot_digest")
        != snapshot_id.removeprefix("sha256-")
    ):
        raise SnapshotIntegrityError("Snapshot totals do not match stored content")
    if source.get("indicator_description") != expected_description:
        raise SnapshotIntegrityError("Snapshot indicator description is inconsistent")
    years = sorted(observation_years)
    year_coverage = manifest.get("reference_year_coverage")
    if not isinstance(year_coverage, dict) or year_coverage != {
        "minimum_year": years[0] if years else None,
        "maximum_year": years[-1] if years else None,
        "distinct_year_count": len(years),
    }:
        raise SnapshotIntegrityError("Snapshot reference-year coverage is inconsistent")
    validation = manifest.get("validation")
    if (
        not isinstance(validation, dict)
        or validation.get("status") != "passed"
        or validation.get("valid_json_and_response_envelope") is not True
        or validation.get("indicator_and_country_identity") is not True
        or validation.get("pagination_metadata_consistent") is not True
        or validation.get("complete_pagination") is not True
        or validation.get("observation_count_matches_api_total") is not True
        or validation.get("null_values_allowed") is not True
    ):
        raise SnapshotIntegrityError("Snapshot validation marker is missing")
    return manifest


def _canonical_json(value: JsonObject) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _pretty_json(value: JsonObject) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _write_bytes(path: Path, content: bytes) -> None:
    with path.open("xb") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
