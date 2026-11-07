from __future__ import annotations

import hashlib
import io
import json
import warnings
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from kenya_food_intelligence.ingestion.faostat import FAOSTATIngestor
from kenya_food_intelligence.sources.faostat import (
    ARCHIVE_FILENAME,
    DATA_CSV_MEMBER,
    DOWNLOAD_URL,
    EXPECTED_ARCHIVE_MEMBERS,
    MAX_ARCHIVE_BYTES,
    FaostatArchiveError,
    FaostatArchiveIntegrityError,
    FaostatArchiveSecurityError,
    FAOSTATClient,
    FaostatError,
    FaostatHTTPError,
    FaostatResourceLimitError,
    inspect_archive,
)
from kenya_food_intelligence.sources.http import (
    HTTPClient,
    HTTPResponseLengthMismatch,
    HTTPResponseTooLarge,
)
from kenya_food_intelligence.storage.bronze import (
    BronzeStore,
)


def _archive_bytes(
    *,
    payload: bytes = b"Area,Item,Value\nKenya,Maize,5\nGlobal,Wheat,9\n",
    extras: tuple[tuple[str, bytes], ...] = (),
    compression: int = ZIP_DEFLATED,
) -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w", compression=compression) as archive:
        for name in EXPECTED_ARCHIVE_MEMBERS:
            body = payload if name == DATA_CSV_MEMBER else f"name,{name}\n".encode()
            archive.writestr(name, body)
        for name, body in extras:
            archive.writestr(name, body)
    return output.getvalue()


class FakeResponse(io.BytesIO):
    def __init__(
        self,
        body: bytes,
        *,
        content_type: str | None = "application/zip",
        content_length: str | None = None,
        url: str = DOWNLOAD_URL,
    ) -> None:
        super().__init__(body)
        self.status = 200
        self.headers = Message()
        if content_type is not None:
            self.headers["Content-Type"] = content_type
        if content_length is not None:
            self.headers["Content-Length"] = content_length
        self.headers["Last-Modified"] = "Fri, 09 Oct 2026 10:00:00 GMT"
        self.headers["ETag"] = '"test-archive"'
        self.url = url
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        chunk = super().read(size)
        self.bytes_read += len(chunk)
        return chunk

    def geturl(self) -> str:
        return self.url

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _client(**options: int | float) -> FAOSTATClient:
    settings: dict[str, int | float] = {"max_retries": 0}
    settings.update(options)
    return FAOSTATClient(**settings)


def test_archive_inspection_records_member_inventory_and_crc(tmp_path: Path) -> None:
    path = tmp_path / "source.zip"
    body = _archive_bytes()
    path.write_bytes(body)

    result = inspect_archive(path)

    assert result.member_count == len(EXPECTED_ARCHIVE_MEMBERS)
    assert result.total_declared_uncompressed_bytes == sum(
        member.uncompressed_size_bytes for member in result.members
    )
    assert result.total_validated_uncompressed_bytes == (
        result.total_declared_uncompressed_bytes
    )
    assert [member.name for member in result.members] == list(EXPECTED_ARCHIVE_MEMBERS)
    assert all(member.validation == "crc_passed" for member in result.members)


@pytest.mark.parametrize(
    ("name", "match", "error"),
    [
        ("../escape.csv", "Unsafe", FaostatArchiveSecurityError),
        ("/absolute.csv", "Unsafe", FaostatArchiveSecurityError),
        ("C:\\escape.csv", "Windows-style", FaostatArchiveSecurityError),
        ("folder\\member.csv", "Windows-style", FaostatArchiveSecurityError),
        ("unexpected.csv", "Unexpected FAOSTAT", FaostatArchiveIntegrityError),
    ],
)
def test_archive_rejects_unsafe_or_unexpected_members(
    tmp_path: Path,
    name: str,
    match: str,
    error: type[FaostatArchiveError],
) -> None:
    path = tmp_path / "unsafe.zip"
    path.write_bytes(_archive_bytes(extras=((name, b"ignored"),)))

    with pytest.raises(error, match=match):
        inspect_archive(path)


def test_archive_rejects_empty_non_zip_and_truncated_files(tmp_path: Path) -> None:
    empty = tmp_path / "empty.zip"
    with ZipFile(empty, "w"):
        pass
    non_zip = tmp_path / "not.zip"
    non_zip.write_bytes(b"not a zip")
    valid = _archive_bytes()
    truncated = tmp_path / "truncated.zip"
    truncated.write_bytes(valid[:-12])

    with pytest.raises(FaostatArchiveIntegrityError, match="no members"):
        inspect_archive(empty)
    with pytest.raises(FaostatArchiveIntegrityError, match="ZIP signature"):
        inspect_archive(non_zip)
    with pytest.raises(FaostatArchiveIntegrityError):
        inspect_archive(truncated)


def test_archive_rejects_corrupt_member_crc(tmp_path: Path) -> None:
    damaged = bytearray(_archive_bytes())
    with ZipFile(io.BytesIO(damaged)) as archive:
        info = archive.getinfo(DATA_CSV_MEMBER)
    name_length = int.from_bytes(
        damaged[info.header_offset + 26 : info.header_offset + 28],
        "little",
    )
    extra_length = int.from_bytes(
        damaged[info.header_offset + 28 : info.header_offset + 30],
        "little",
    )
    data_offset = info.header_offset + 30 + name_length + extra_length
    damaged[data_offset] ^= 0xFF
    path = tmp_path / "corrupt.zip"
    path.write_bytes(damaged)

    with pytest.raises(FaostatArchiveIntegrityError):
        inspect_archive(path)


def test_archive_rejects_duplicate_member_names(tmp_path: Path) -> None:
    output = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
            for name in EXPECTED_ARCHIVE_MEMBERS:
                archive.writestr(
                    name,
                    b"csv" if name == DATA_CSV_MEMBER else b"lookup",
                )
            archive.writestr(DATA_CSV_MEMBER.swapcase(), b"ambiguous")
            archive.writestr(DATA_CSV_MEMBER, b"duplicate")
    path = tmp_path / "duplicate.zip"
    path.write_bytes(output.getvalue())

    with pytest.raises(FaostatArchiveSecurityError, match="duplicate"):
        inspect_archive(path)


def test_archive_rejects_symlink_and_encrypted_members(tmp_path: Path) -> None:
    output = io.BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name in EXPECTED_ARCHIVE_MEMBERS:
            info = ZipInfo(name)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o120777 << 16
            archive.writestr(info, b"data")
    symlink_path = tmp_path / "symlink.zip"
    symlink_path.write_bytes(output.getvalue())
    with pytest.raises(FaostatArchiveSecurityError, match="member type"):
        inspect_archive(symlink_path)

    encrypted = bytearray(_archive_bytes())
    with ZipFile(io.BytesIO(encrypted)) as archive:
        offset = archive.getinfo(DATA_CSV_MEMBER).header_offset
    encrypted[offset + 6] |= 0x01
    central = encrypted.find(b"PK\x01\x02")
    encrypted[central + 8] |= 0x01
    encrypted_path = tmp_path / "encrypted.zip"
    encrypted_path.write_bytes(encrypted)
    with pytest.raises(FaostatArchiveSecurityError, match="Encrypted"):
        inspect_archive(encrypted_path)


def test_archive_rejects_unsupported_compression_method(tmp_path: Path) -> None:
    damaged = bytearray(_archive_bytes())
    with ZipFile(io.BytesIO(damaged)) as archive:
        info = archive.getinfo(DATA_CSV_MEMBER)
    damaged[info.header_offset + 8 : info.header_offset + 10] = (99).to_bytes(
        2,
        "little",
    )
    central = damaged.find(b"PK\x01\x02")
    damaged[central + 10 : central + 12] = (99).to_bytes(2, "little")
    path = tmp_path / "unsupported-compression.zip"
    path.write_bytes(damaged)

    with pytest.raises(FaostatArchiveSecurityError, match="compression method"):
        inspect_archive(path)


def test_archive_enforces_member_count_and_declared_size_limits(
    tmp_path: Path,
) -> None:
    path = tmp_path / "source.zip"
    path.write_bytes(_archive_bytes())

    with pytest.raises(FaostatResourceLimitError, match="member limit"):
        inspect_archive(path, max_members=4)
    with pytest.raises(FaostatResourceLimitError, match="member exceeds"):
        inspect_archive(path, max_member_uncompressed_bytes=10)
    with pytest.raises(FaostatResourceLimitError, match="total uncompressed"):
        inspect_archive(path, max_total_uncompressed_bytes=10)
    with pytest.raises(FaostatResourceLimitError, match="archive exceeds"):
        inspect_archive(path, max_archive_bytes=10)


@pytest.mark.parametrize(
    ("member_limit", "total_limit", "message"),
    [
        (100, 200, "member expands beyond"),
        (200, 100, "total validation limit"),
    ],
)
def test_archive_enforces_actual_decompressed_limits_while_streaming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    member_limit: int,
    total_limit: int,
    message: str,
) -> None:
    path = tmp_path / "source.zip"
    path.write_bytes(_archive_bytes())
    original_zip_file = ZipFile

    class OversizedReader:
        def __init__(self, archive_path: Path) -> None:
            self.archive = original_zip_file(archive_path)

        def __enter__(self) -> OversizedReader:
            return self

        def __exit__(self, *_: object) -> None:
            self.archive.close()

        def infolist(self):
            members = self.archive.infolist()
            for member in members:
                member.file_size = 0
            return members

        def open(self, member, mode: str = "r"):
            if member.filename == DATA_CSV_MEMBER:
                return io.BytesIO(b"x" * 101)
            return self.archive.open(member, mode)

    monkeypatch.setattr(
        "kenya_food_intelligence.sources.faostat.ZipFile",
        OversizedReader,
    )
    with pytest.raises(FaostatResourceLimitError, match=message):
        inspect_archive(
            path,
            max_member_uncompressed_bytes=member_limit,
            max_total_uncompressed_bytes=total_limit,
        )


def test_highly_compressed_archive_is_accepted_without_ratio_heuristic(
    tmp_path: Path,
) -> None:
    path = tmp_path / "compressed.zip"
    path.write_bytes(_archive_bytes(payload=b"a" * 1_000_000))

    result = inspect_archive(path)

    assert result.members[0].uncompressed_size_bytes == 1_000_000
    assert result.members[0].compressed_size_bytes < 10_000


@pytest.mark.parametrize(
    ("content_type", "final_url", "exception", "message"),
    [
        ("text/html", DOWNLOAD_URL, FaostatArchiveError, "ZIP download"),
        (None, DOWNLOAD_URL, FaostatArchiveError, "ZIP download"),
        (
            "application/zip",
            "https://evil.example/archive.zip",
            FaostatHTTPError,
            "unapproved URL",
        ),
    ],
)
def test_download_rejects_wrong_media_type_and_redirect(
    tmp_path: Path,
    content_type: str | None,
    final_url: str,
    exception: type[Exception],
    message: str,
) -> None:
    response = FakeResponse(
        _archive_bytes(),
        content_type=content_type,
        content_length=str(len(_archive_bytes())),
        url=final_url,
    )
    destination = tmp_path / ARCHIVE_FILENAME
    with (
        patch(
            "kenya_food_intelligence.sources.http.urlopen",
            return_value=response,
        ),
        pytest.raises(exception, match=message),
    ):
        _client().download_to(destination)
    assert not destination.exists()


def test_download_accepts_missing_content_length_and_preserves_exact_bytes(
    tmp_path: Path,
) -> None:
    body = _archive_bytes()
    response = FakeResponse(body)
    downloaded_path = tmp_path / ARCHIVE_FILENAME
    with patch(
        "kenya_food_intelligence.sources.http.urlopen",
        return_value=response,
    ):
        downloaded = _client().download_to(downloaded_path)

    assert downloaded.path.read_bytes() == body
    assert downloaded.file_size_bytes == len(body)
    assert downloaded.checksum_sha256 == hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize("declared", ["not-a-number", "-1", "999999999999"])
def test_http_download_rejects_malformed_or_oversized_content_length(
    tmp_path: Path,
    declared: str,
) -> None:
    body = _archive_bytes()
    response = FakeResponse(body, content_length=declared)
    client = HTTPClient(
        timeout=1,
        max_retries=0,
        backoff_factor=0,
        user_agent="test",
    )
    destination = tmp_path / "response.zip"
    expected_error = (
        HTTPResponseTooLarge
        if declared.isdigit() and int(declared) > MAX_ARCHIVE_BYTES
        else HTTPResponseLengthMismatch
    )
    with (
        patch(
            "kenya_food_intelligence.sources.http.urlopen",
            return_value=response,
        ),
        pytest.raises(expected_error),
    ):
        client.download_to(
            DOWNLOAD_URL,
            destination,
            max_bytes=MAX_ARCHIVE_BYTES,
        )
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("declared_delta", [-1, 1])
def test_http_download_rejects_misleading_content_length(
    tmp_path: Path,
    declared_delta: int,
) -> None:
    body = b"zip bytes"
    response = FakeResponse(body, content_length=str(len(body) + declared_delta))
    client = HTTPClient(
        timeout=1,
        max_retries=0,
        backoff_factor=0,
        user_agent="test",
    )
    destination = tmp_path / "response.zip"
    with (
        patch(
            "kenya_food_intelligence.sources.http.urlopen",
            return_value=response,
        ),
        pytest.raises(HTTPResponseLengthMismatch, match="declared"),
    ):
        client.download_to(
            DOWNLOAD_URL,
            destination,
            max_bytes=100,
        )
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_download_rejects_oversized_stream_and_interrupted_transfer(
    tmp_path: Path,
) -> None:
    body = _archive_bytes()
    response = FakeResponse(body)
    destination = tmp_path / ARCHIVE_FILENAME
    with (
        patch(
            "kenya_food_intelligence.sources.http.urlopen",
            return_value=response,
        ),
        pytest.raises(FaostatResourceLimitError),
    ):
        _client(max_download_bytes=100).download_to(destination)
    assert not destination.exists()

    client = _client(max_retries=0)
    with patch(
        "kenya_food_intelligence.sources.http.urlopen",
        side_effect=TimeoutError("interrupted"),
    ):
        with pytest.raises(FaostatHTTPError, match="GET failed"):
            client.download_to(destination)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_download_retries_read_timeout(tmp_path: Path) -> None:
    body = _archive_bytes()
    client = FAOSTATClient(max_retries=1, backoff_factor=0)
    with (
        patch(
            "kenya_food_intelligence.sources.http.urlopen",
            side_effect=[
                TimeoutError("read timed out"),
                FakeResponse(body, content_length=str(len(body))),
            ],
        ) as open_url,
        patch("kenya_food_intelligence.sources.http.time.sleep"),
    ):
        downloaded = client.download_to(tmp_path / ARCHIVE_FILENAME)

    assert downloaded.path.read_bytes() == body
    assert open_url.call_count == 2


def test_download_does_not_replace_an_existing_destination(tmp_path: Path) -> None:
    destination = tmp_path / ARCHIVE_FILENAME
    destination.write_bytes(b"do not replace")

    with pytest.raises(FaostatError, match="must not already exist"):
        _client().download_to(destination)

    assert destination.read_bytes() == b"do not replace"


def test_ingestion_is_idempotent_revisions_are_immutable_and_corruption_fails(
    tmp_path: Path,
) -> None:
    body_v1 = _archive_bytes(payload=b"global,maize,1\nkenya,maize,2\n")
    body_v2 = _archive_bytes(payload=b"global,maize,3\nkenya,maize,4\n")
    responses = iter([body_v1, body_v1, body_v2, body_v2])
    store = BronzeStore(tmp_path / "store")
    client = _client()
    ingestor = FAOSTATIngestor(client, store)

    def open_source(*_args, **_kwargs) -> FakeResponse:
        body = next(responses)
        return FakeResponse(body, content_length=str(len(body)))

    with patch(
        "kenya_food_intelligence.sources.http.urlopen",
        side_effect=open_source,
    ):
        first = ingestor.ingest()
        repeated = ingestor.ingest()
        revised = ingestor.ingest()

    assert first.outcome == "created"
    assert repeated.outcome == "unchanged"
    assert repeated.snapshot_id == first.snapshot_id
    assert revised.outcome == "created"
    assert revised.snapshot_id != first.snapshot_id
    assert first.snapshot_path is not None
    assert revised.snapshot_path is not None
    first_archive = first.snapshot_path / ARCHIVE_FILENAME
    revised_archive = revised.snapshot_path / ARCHIVE_FILENAME
    assert first_archive.read_bytes() == body_v1
    assert revised_archive.read_bytes() == body_v2
    assert len(list(first.snapshot_path.parent.iterdir())) == 2
    assert not list((tmp_path / "store" / "bronze").rglob("*.csv"))

    manifest_path = revised.snapshot_path / "manifest.json"
    manifest_before = manifest_path.read_bytes()
    manifest = json.loads(manifest_before)
    assert manifest["file"]["sha256"] == hashlib.sha256(body_v2).hexdigest()
    assert manifest["archive"]["member_count"] == len(EXPECTED_ARCHIVE_MEMBERS)
    assert manifest["archive"]["total_validated_uncompressed_bytes"] == sum(
        member["validated_size_bytes"] for member in manifest["archive"]["members"]
    )
    assert all(
        member["validation"] == "crc_passed"
        for member in manifest["archive"]["members"]
    )
    run_one = json.loads(first.run_record_path.read_text())
    run_two = json.loads(repeated.run_record_path.read_text())
    assert run_one["ingestion_run_id"] != run_two["ingestion_run_id"]
    assert run_one["outcome"] == "created"
    assert run_two["outcome"] == "unchanged"
    assert run_one["requested_url"] == DOWNLOAD_URL
    assert run_one["source_last_modified"] == "Fri, 09 Oct 2026 10:00:00 GMT"
    assert run_one["source_etag"] == '"test-archive"'
    assert "Authorization" not in first.run_record_path.read_text()
    assert manifest_path.read_bytes() == manifest_before

    revised_archive.write_bytes(b"corrupt")
    with patch(
        "kenya_food_intelligence.sources.http.urlopen",
        side_effect=open_source,
    ):
        failed = ingestor.ingest()

    assert failed.outcome == "failed"
    assert "checksum" in (failed.error or "").lower()
    assert revised_archive.read_bytes() == b"corrupt"
    assert manifest_path.read_bytes() == manifest_before
    failed_record = json.loads(failed.run_record_path.read_text())
    assert failed_record["outcome"] == "failed"


def test_ingestion_records_invalid_archive_failure_without_publishing(
    tmp_path: Path,
) -> None:
    store = BronzeStore(tmp_path / "store")
    ingestor = FAOSTATIngestor(_client(), store)
    response = FakeResponse(b"<html>not an archive</html>")

    with patch(
        "kenya_food_intelligence.sources.http.urlopen",
        return_value=response,
    ):
        result = ingestor.ingest()

    assert result.outcome == "failed"
    assert result.snapshot_path is None
    assert not list((tmp_path / "store" / "bronze").rglob("*.zip"))
    record = json.loads(result.run_record_path.read_text())
    assert record["outcome"] == "failed"
    assert record["error"]["category"] == "FaostatArchiveIntegrityError"


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_ingestion_detects_missing_or_corrupt_manifest_without_overwrite(
    tmp_path: Path,
    damage: str,
) -> None:
    body = _archive_bytes()
    store = BronzeStore(tmp_path / "store")
    ingestor = FAOSTATIngestor(_client(), store)

    with patch(
        "kenya_food_intelligence.sources.http.urlopen",
        side_effect=lambda *_args, **_kwargs: FakeResponse(
            body,
            content_length=str(len(body)),
        ),
    ):
        created = ingestor.ingest()
    assert created.outcome == "created"
    assert created.manifest_path is not None
    manifest_before = created.manifest_path.read_bytes()
    if damage == "missing":
        created.manifest_path.unlink()
    else:
        created.manifest_path.write_text("{", encoding="utf-8")
    damaged_manifest = (
        None if damage == "missing" else created.manifest_path.read_bytes()
    )

    with patch(
        "kenya_food_intelligence.sources.http.urlopen",
        side_effect=lambda *_args, **_kwargs: FakeResponse(
            body,
            content_length=str(len(body)),
        ),
    ):
        missing_manifest = ingestor.ingest()

    assert missing_manifest.outcome == "failed"
    assert "metadata" in (missing_manifest.error or "").lower()
    if damage == "missing":
        assert not created.manifest_path.exists()
    else:
        assert created.manifest_path.read_bytes() == damaged_manifest
    failed = json.loads(missing_manifest.run_record_path.read_text())
    assert failed["outcome"] == "failed"
    assert manifest_before


def test_ingestion_creates_separate_source_provenance_runs_for_same_content(
    tmp_path: Path,
) -> None:
    body = _archive_bytes()
    ingestor = FAOSTATIngestor(_client(), BronzeStore(tmp_path / "store"))
    with patch(
        "kenya_food_intelligence.sources.http.urlopen",
        side_effect=lambda *_args, **_kwargs: FakeResponse(
            body,
            content_length=str(len(body)),
        ),
    ):
        first = ingestor.ingest()
        second = ingestor.ingest()
    records = sorted(
        (
            tmp_path
            / "store"
            / "manifests"
            / "faostat_producer_prices_normalized"
            / "runs"
        ).glob("*.json")
    )

    assert first.outcome == "created"
    assert second.outcome == "unchanged"
    assert len(records) == 2
    assert all(
        json.loads(path.read_text())["requested_url"] == DOWNLOAD_URL
        for path in records
    )


def test_concurrent_identical_and_revised_ingestions_converge_safely(
    tmp_path: Path,
) -> None:
    original = _archive_bytes(payload=b"global,maize,1\nkenya,maize,2\n")
    revised = _archive_bytes(payload=b"global,maize,3\nkenya,maize,4\n")
    client = _client()

    identical_store = BronzeStore(tmp_path / "identical")
    identical_ingestor = FAOSTATIngestor(client, identical_store)
    with (
        patch(
            "kenya_food_intelligence.sources.http.urlopen",
            side_effect=[
                FakeResponse(original, content_length=str(len(original))),
                FakeResponse(original, content_length=str(len(original))),
            ],
        ),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        identical = list(executor.map(lambda _: identical_ingestor.ingest(), range(2)))
    assert sorted(result.outcome for result in identical) == ["created", "unchanged"]
    assert identical[0].snapshot_id == identical[1].snapshot_id

    revised_store = BronzeStore(tmp_path / "revisions")
    revised_ingestor = FAOSTATIngestor(client, revised_store)
    with (
        patch(
            "kenya_food_intelligence.sources.http.urlopen",
            side_effect=[
                FakeResponse(original, content_length=str(len(original))),
                FakeResponse(revised, content_length=str(len(revised))),
            ],
        ),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        revisions = list(executor.map(lambda _: revised_ingestor.ingest(), range(2)))
    assert [result.outcome for result in revisions] == ["created", "created"]
    assert revisions[0].snapshot_id != revisions[1].snapshot_id
    assert all(result.snapshot_path is not None for result in revisions)
    assert len(list(revisions[0].snapshot_path.parent.iterdir())) == 2


def test_client_validates_configured_limits_as_positive_integers() -> None:
    for options in (
        {"max_download_bytes": 0},
        {"max_members": True},
        {"max_member_uncompressed_bytes": -1},
        {"max_total_uncompressed_bytes": 1.5},
    ):
        with pytest.raises(ValueError, match="positive integer"):
            _client(**options)
