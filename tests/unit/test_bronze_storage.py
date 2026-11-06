from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from kenya_food_intelligence.sources.world_bank import PageMetadata, RawIndicatorPage
from kenya_food_intelligence.storage.bronze import (
    BronzeStorageError,
    BronzeStore,
    SnapshotIntegrityError,
)

INDICATOR = "FP.CPI.TOTL"


def _raw_page(
    *,
    indicator_code: str = INDICATOR,
    page_number: int = 1,
    pages: int = 1,
    per_page: int = 100,
    total: int = 1,
    year: str = "2024",
    value: int | float | None = 100,
) -> RawIndicatorPage:
    observations = (
        [
            {
                "indicator": {"id": indicator_code, "value": "Consumer price index"},
                "country": {"id": "KE", "value": "Kenya"},
                "countryiso3code": "KEN",
                "date": year,
                "value": value,
                "unit": "",
                "obs_status": "",
                "decimal": 1,
            }
        ]
        if total
        else []
    )
    metadata = {
        "page": page_number,
        "pages": pages,
        "per_page": per_page,
        "total": total,
    }
    data = None if total == 0 else observations
    body = json.dumps([metadata, data], separators=(",", ":")).encode()
    return RawIndicatorPage(
        metadata=PageMetadata(
            page=page_number,
            pages=pages,
            per_page=per_page,
            total=total,
        ),
        observations=observations,
        request_url=(
            "https://api.worldbank.org/v2/country/KEN/indicator/"
            f"{indicator_code}?format=json&per_page={per_page}&page={page_number}"
        ),
        content_type="application/json",
        body=body,
    )


def _publish(
    store: BronzeStore,
    pages: tuple[RawIndicatorPage, ...],
    *,
    indicator_code: str = INDICATOR,
) -> tuple[str, str, Path, dict[str, object]]:
    return store.publish_snapshot(
        country_code="KEN",
        indicator_code=indicator_code,
        per_page=pages[0].metadata.per_page,
        pages=pages,
    )


def test_publishes_complete_snapshot_with_verified_manifest(tmp_path: Path) -> None:
    store = BronzeStore(tmp_path)
    pages = (_raw_page(value=None),)

    outcome, snapshot_id, snapshot_path, manifest = _publish(store, pages)

    assert outcome == "created"
    assert snapshot_id.startswith("sha256-")
    assert (snapshot_path / "page-0001.json").read_bytes() == pages[0].body
    assert manifest["manifest_schema_version"] == 1
    assert manifest["pagination"] == {
        "api_page_count": 1,
        "retrieved_page_count": 1,
        "per_page": 100,
        "api_total_observations": 1,
    }
    assert manifest["integrity"]["total_observation_count"] == 1
    assert manifest["validation"]["status"] == "passed"
    assert manifest["source"]["indicator_description"] == "Consumer price index"
    assert manifest["reference_year_coverage"] == {
        "minimum_year": "2024",
        "maximum_year": "2024",
        "distinct_year_count": 1,
    }


def test_publishes_valid_empty_indicator_response(tmp_path: Path) -> None:
    empty_page = _raw_page(pages=0, total=0)

    outcome, _, snapshot_path, manifest = _publish(BronzeStore(tmp_path), (empty_page,))

    assert outcome == "created"
    assert (snapshot_path / "page-0001.json").read_bytes() == empty_page.body
    assert manifest["pagination"]["api_page_count"] == 0
    assert manifest["pagination"]["retrieved_page_count"] == 1
    assert manifest["reference_year_coverage"] == {
        "minimum_year": None,
        "maximum_year": None,
        "distinct_year_count": 0,
    }


def test_identical_snapshot_is_reused_and_original_manifest_is_unchanged(
    tmp_path: Path,
) -> None:
    store = BronzeStore(tmp_path)
    pages = (_raw_page(),)
    outcome, snapshot_id, snapshot_path, _ = _publish(store, pages)
    original_manifest = (snapshot_path / "manifest.json").read_bytes()

    repeated_outcome, repeated_id, repeated_path, _ = _publish(store, pages)

    assert outcome == "created"
    assert repeated_outcome == "unchanged"
    assert repeated_id == snapshot_id
    assert repeated_path == snapshot_path
    assert (snapshot_path / "manifest.json").read_bytes() == original_manifest
    assert sorted(path.name for path in snapshot_path.iterdir()) == [
        "descriptor.json",
        "manifest.json",
        "page-0001.json",
    ]


def test_changed_bytes_create_a_new_immutable_snapshot(tmp_path: Path) -> None:
    store = BronzeStore(tmp_path)
    first = (_raw_page(year="2024", value=100),)
    revised = (_raw_page(year="2024", value=101),)

    _, first_id, first_path, _ = _publish(store, first)
    _, revised_id, revised_path, _ = _publish(store, revised)

    assert first_id != revised_id
    assert first_path != revised_path
    assert (first_path / "page-0001.json").read_bytes() == first[0].body
    assert (revised_path / "page-0001.json").read_bytes() == revised[0].body
    assert len(list(first_path.parent.iterdir())) == 2


def test_existing_payload_corruption_is_detected(tmp_path: Path) -> None:
    store = BronzeStore(tmp_path)
    _, _, snapshot_path, _ = _publish(store, (_raw_page(),))
    payload_path = snapshot_path / "page-0001.json"
    payload_path.write_bytes(payload_path.read_bytes() + b" ")

    with pytest.raises(SnapshotIntegrityError, match="integrity failed"):
        _publish(store, (_raw_page(),))


def test_missing_snapshot_manifest_is_detected_without_overwrite(
    tmp_path: Path,
) -> None:
    store = BronzeStore(tmp_path)
    _, _, snapshot_path, _ = _publish(store, (_raw_page(),))
    (snapshot_path / "manifest.json").unlink()

    with pytest.raises(
        SnapshotIntegrityError, match="manifest or descriptor is missing"
    ):
        _publish(store, (_raw_page(),))
    assert not (snapshot_path / "manifest.json").exists()


def test_interrupted_staging_publishes_nothing_and_retry_recovers(
    tmp_path: Path,
) -> None:
    store = BronzeStore(tmp_path)
    pages = (_raw_page(),)
    from kenya_food_intelligence.storage import bronze

    original_write = bronze._write_bytes

    def fail_on_payload(path: Path, content: bytes) -> None:
        if path.name == "page-0001.json":
            raise OSError("simulated interrupted write")
        original_write(path, content)

    with patch("kenya_food_intelligence.storage.bronze._write_bytes", fail_on_payload):
        with pytest.raises(BronzeStorageError, match="simulated interrupted write"):
            _publish(store, pages)

    snapshots = tmp_path / "bronze/world_bank/indicator=FP.CPI.TOTL/snapshots"
    assert list(snapshots.iterdir()) == []
    assert _publish(store, pages)[0] == "created"


def test_abandoned_staging_directory_is_cleaned_before_retry(tmp_path: Path) -> None:
    store = BronzeStore(tmp_path)
    snapshots = tmp_path / "bronze/world_bank/indicator=FP.CPI.TOTL/snapshots"
    snapshots.mkdir(parents=True)
    abandoned = snapshots / ".staging-abandoned"
    abandoned.mkdir()
    (abandoned / "partial").write_bytes(b"unfinished")

    outcome, _, _, _ = _publish(store, (_raw_page(),))

    assert outcome == "created"
    assert not abandoned.exists()


def test_unsafe_partition_values_are_rejected(tmp_path: Path) -> None:
    store = BronzeStore(tmp_path)
    with pytest.raises(BronzeStorageError, match="Indicator code"):
        store.publish_snapshot(
            country_code="KEN",
            indicator_code="../../secrets",
            per_page=100,
            pages=(_raw_page(),),
        )
    with pytest.raises(BronzeStorageError, match="Country code"):
        store.publish_snapshot(
            country_code="../",
            indicator_code=INDICATOR,
            per_page=100,
            pages=(_raw_page(),),
        )


def test_indicators_are_stored_in_separate_partitions(tmp_path: Path) -> None:
    store = BronzeStore(tmp_path)
    index = (_raw_page(),)
    inflation = (_raw_page(indicator_code="FP.CPI.TOTL.ZG"),)

    index_result = _publish(store, index)
    inflation_result = _publish(
        store,
        inflation,
        indicator_code="FP.CPI.TOTL.ZG",
    )

    assert index_result[2].parent.parent != inflation_result[2].parent.parent
    assert "indicator=FP.CPI.TOTL" in str(index_result[2])
    assert "indicator=FP.CPI.TOTL.ZG" in str(inflation_result[2])


def test_concurrent_identical_publications_converge_without_overwrite(
    tmp_path: Path,
) -> None:
    store = BronzeStore(tmp_path)
    pages = (_raw_page(),)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_publish, store, pages) for _ in range(2)]
        results = [future.result() for future in futures]

    assert sorted(result[0] for result in results) == ["created", "unchanged"]
    assert results[0][1] == results[1][1]
    assert results[0][2] == results[1][2]


def test_concurrent_revisions_publish_separate_snapshots(tmp_path: Path) -> None:
    store = BronzeStore(tmp_path)
    first = (_raw_page(value=100),)
    revised = (_raw_page(value=101),)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_publish, store, pages) for pages in (first, revised)
        ]
        results = [future.result() for future in futures]

    assert [result[0] for result in results] == ["created", "created"]
    assert results[0][1] != results[1][1]
    assert len(list(results[0][2].parent.iterdir())) == 2


def test_run_records_are_atomic_append_only_and_have_unique_ids(
    tmp_path: Path,
) -> None:
    store = BronzeStore(tmp_path)
    base = {
        "run_record_schema_version": 1,
        "source_id": "world_bank_indicators_api",
        "country_code": "KEN",
        "indicator_code": INDICATOR,
        "started_at_utc": datetime.now(UTC).isoformat(),
        "completed_at_utc": datetime.now(UTC).isoformat(),
        "outcome": "created",
        "snapshot_id": "sha256-" + "0" * 64,
    }
    first = {**base, "ingestion_run_id": "1" * 32}
    second = {**base, "ingestion_run_id": "2" * 32, "outcome": "unchanged"}

    first_path = store.write_run_record(first)
    second_path = store.write_run_record(second)

    assert first_path != second_path
    assert json.loads(first_path.read_text()) == first
    assert json.loads(second_path.read_text()) == second
    with pytest.raises(BronzeStorageError, match="already exists"):
        store.write_run_record(first)
    assert sorted(path.suffix for path in first_path.parent.iterdir()) == [
        ".json",
        ".json",
    ]


def test_run_record_rejects_unsafe_identity_and_outcome(tmp_path: Path) -> None:
    store = BronzeStore(tmp_path)
    record = {
        "country_code": "KEN",
        "indicator_code": "../outside",
        "ingestion_run_id": "a" * 32,
        "outcome": "created",
    }
    with pytest.raises(BronzeStorageError, match="Indicator code"):
        store.write_run_record(record)
    record["indicator_code"] = INDICATOR
    record["outcome"] = "success"
    with pytest.raises(BronzeStorageError, match="outcome"):
        store.write_run_record(record)
