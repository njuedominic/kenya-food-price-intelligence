from __future__ import annotations

import hashlib
import io
import json
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from kenya_food_intelligence.ingestion.world_bank import WorldBankIngestor
from kenya_food_intelligence.sources.world_bank import WorldBankClient
from kenya_food_intelligence.storage.bronze import BronzeStore

INDICATOR = "FP.CPI.TOTL"


class MockResponse(io.BytesIO):
    def __init__(self, body: bytes) -> None:
        super().__init__(body)
        self.status = 200
        self.headers = Message()
        self.headers["Content-Type"] = "application/json; charset=utf-8"

    def geturl(self) -> str:
        return "https://api.worldbank.org/v2/mock"

    def __enter__(self) -> MockResponse:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _page(
    page_number: int,
    value: int | None,
    *,
    indicator_code: str = INDICATOR,
) -> bytes:
    observation = {
        "indicator": {"id": indicator_code, "value": "Consumer price index"},
        "country": {"id": "KE", "value": "Kenya"},
        "countryiso3code": "KEN",
        "date": str(2025 - page_number),
        "value": value,
        "unit": "",
        "obs_status": "",
        "decimal": 1,
    }
    return json.dumps(
        [
            {"page": page_number, "pages": 2, "per_page": 1, "total": 2},
            [observation],
        ],
        separators=(",", ":"),
    ).encode()


def test_local_end_to_end_create_reuse_and_source_revision(tmp_path: Path) -> None:
    first_page = _page(1, None)
    original_second_page = _page(2, 95)
    revised_second_page = _page(2, 96)
    response_bodies = [
        first_page,
        original_second_page,
        first_page,
        original_second_page,
        first_page,
        revised_second_page,
    ]
    client = WorldBankClient(max_retries=0)
    ingestor = WorldBankIngestor(client, BronzeStore(tmp_path))

    with patch(
        "kenya_food_intelligence.sources.http.urlopen",
        side_effect=[MockResponse(body) for body in response_bodies],
    ):
        created = ingestor.ingest(INDICATOR, per_page=1)
        unchanged = ingestor.ingest(INDICATOR, per_page=1)
        revised = ingestor.ingest(INDICATOR, per_page=1)

    assert (created.outcome, unchanged.outcome, revised.outcome) == (
        "created",
        "unchanged",
        "created",
    )
    assert created.ingestion_run_id != unchanged.ingestion_run_id
    assert created.ingestion_run_id != revised.ingestion_run_id
    assert created.snapshot_id == unchanged.snapshot_id
    assert revised.snapshot_id != created.snapshot_id
    assert created.observation_count == revised.observation_count == 2
    assert created.page_count == revised.page_count == 2
    assert created.snapshot_path is not None
    assert revised.snapshot_path is not None
    assert (created.snapshot_path / "page-0001.json").read_bytes() == first_page
    assert (
        created.snapshot_path / "page-0002.json"
    ).read_bytes() == original_second_page

    manifest = json.loads(created.manifest_path.read_text())
    assert manifest["integrity"]["total_observation_count"] == 2
    assert (
        manifest["integrity"]["pages"][0]["sha256"]
        == hashlib.sha256(first_page).hexdigest()
    )
    assert manifest["pagination"]["retrieved_page_count"] == 2
    run_records = list(
        (tmp_path / "manifests/world_bank/runs/indicator=FP.CPI.TOTL").glob("*.json")
    )
    assert len(run_records) == 3
    assert sorted(json.loads(path.read_text())["outcome"] for path in run_records) == [
        "created",
        "created",
        "unchanged",
    ]
    assert len(list(created.snapshot_path.parent.iterdir())) == 2


def test_partial_pagination_failure_writes_failed_run_without_snapshot(
    tmp_path: Path,
) -> None:
    first_page = _page(1, 100)
    failure = OSError("network unavailable")
    client = WorldBankClient(max_retries=0)
    ingestor = WorldBankIngestor(client, BronzeStore(tmp_path))
    with patch(
        "kenya_food_intelligence.sources.http.urlopen",
        side_effect=[MockResponse(first_page), failure],
    ):
        result = ingestor.ingest(INDICATOR, per_page=1)

    assert result.outcome == "failed"
    assert result.snapshot_id is None
    assert result.snapshot_path is None
    snapshots = tmp_path / "bronze/world_bank/indicator=FP.CPI.TOTL/snapshots"
    assert not snapshots.exists() or list(snapshots.iterdir()) == []
    run_record = json.loads(result.run_record_path.read_text())
    assert run_record["outcome"] == "failed"
    assert run_record["validation_status"] == "failed"
    assert run_record["page_count"] is None
    assert run_record["error"]["category"] == "WorldBankRequestError"


def test_oversized_response_writes_failed_run_without_snapshot(
    tmp_path: Path,
) -> None:
    first_page = _page(1, 100)
    limit = len(first_page) - 1
    client = WorldBankClient(
        max_retries=0,
        max_page_bytes=limit,
        max_total_bytes=limit,
    )
    ingestor = WorldBankIngestor(client, BronzeStore(tmp_path))
    with patch(
        "kenya_food_intelligence.sources.http.urlopen",
        return_value=MockResponse(first_page),
    ):
        result = ingestor.ingest(INDICATOR, per_page=1)

    assert result.outcome == "failed"
    assert result.snapshot_id is None
    assert result.snapshot_path is None
    snapshots = tmp_path / "bronze/world_bank/indicator=FP.CPI.TOTL/snapshots"
    assert not snapshots.exists() or list(snapshots.iterdir()) == []
    run_record = json.loads(result.run_record_path.read_text())
    assert run_record["outcome"] == "failed"
    assert run_record["validation_status"] == "failed"
    assert run_record["error"]["category"] == "WorldBankResponseError"
    assert "page response" in run_record["error"]["message"]


def test_both_approved_indicators_ingest_complete_mocked_pages(
    tmp_path: Path,
) -> None:
    client = WorldBankClient(max_retries=0)
    ingestor = WorldBankIngestor(client, BronzeStore(tmp_path))
    for indicator_code in ("FP.CPI.TOTL", "FP.CPI.TOTL.ZG"):
        responses = [
            MockResponse(_page(1, 100, indicator_code=indicator_code)),
            MockResponse(_page(2, 99, indicator_code=indicator_code)),
        ]
        with patch(
            "kenya_food_intelligence.sources.http.urlopen",
            side_effect=responses,
        ):
            result = ingestor.ingest(indicator_code, per_page=1)

        assert result.outcome == "created"
        assert result.page_count == 2
        assert result.observation_count == 2
