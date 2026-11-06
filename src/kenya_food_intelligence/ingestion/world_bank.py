"""World Bank indicator acquisition and immutable Bronze publication."""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from kenya_food_intelligence.sources.world_bank import (
    WorldBankClient,
    WorldBankError,
)
from kenya_food_intelligence.storage.bronze import (
    BronzeStorageError,
    BronzeStore,
    JsonObject,
)

COUNTRY_CODE = "KEN"
INDICATORS = ("FP.CPI.TOTL", "FP.CPI.TOTL.ZG")
RUN_RECORD_SCHEMA_VERSION = 1
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestionResult:
    ingestion_run_id: str
    country_code: str
    indicator_code: str
    outcome: str
    snapshot_id: str | None
    observation_count: int | None
    page_count: int | None
    snapshot_path: Path | None
    manifest_path: Path | None
    run_record_path: Path
    error: str | None = None


class WorldBankIngestor:
    """Fetch complete World Bank pages and publish a Bronze snapshot."""

    def __init__(self, client: WorldBankClient, store: BronzeStore) -> None:
        self.client = client
        self.store = store

    def ingest(
        self,
        indicator_code: str,
        *,
        per_page: int = 100,
        country_code: str = COUNTRY_CODE,
    ) -> IngestionResult:
        if indicator_code not in INDICATORS:
            raise ValueError(f"Unsupported World Bank CPI indicator: {indicator_code}")
        if country_code != COUNTRY_CODE:
            raise ValueError("World Bank Bronze ingestion is limited to Kenya (KEN)")
        if isinstance(per_page, bool) or not 1 <= per_page <= 1000:
            raise ValueError("per_page must be between 1 and 1000")

        run_id = uuid.uuid4().hex
        started_at = datetime.now(UTC)
        logger.info(
            "Starting World Bank Bronze ingestion for %s (%s)",
            indicator_code,
            country_code,
        )
        observation_count: int | None = None
        page_count: int | None = None
        snapshot_id: str | None = None
        snapshot_path: Path | None = None
        manifest_path: Path | None = None

        try:
            result = self.client.get_indicator_observation_pages(
                country_code,
                indicator_code,
                per_page=per_page,
            )
            observation_count = sum(len(page.observations) for page in result.pages)
            page_count = len(result.pages)
            logger.info(
                "Validated complete World Bank response for %s: %d observations",
                indicator_code,
                observation_count,
            )
            outcome, snapshot_id, snapshot_path, _manifest = (
                self.store.publish_snapshot(
                    country_code=country_code,
                    indicator_code=indicator_code,
                    per_page=per_page,
                    pages=result.pages,
                )
            )
            manifest_path = snapshot_path / "manifest.json"
            logger.info(
                "World Bank Bronze snapshot %s for %s: %s",
                outcome,
                indicator_code,
                snapshot_id,
            )
        except (WorldBankError, BronzeStorageError) as error:
            completed_at = datetime.now(UTC)
            record = _run_record(
                run_id=run_id,
                country_code=country_code,
                indicator_code=indicator_code,
                per_page=per_page,
                started_at=started_at,
                completed_at=completed_at,
                outcome="failed",
                snapshot_id=snapshot_id,
                observation_count=observation_count,
                page_count=page_count,
                error=error,
            )
            run_path = self.store.write_run_record(record)
            logger.error(
                "World Bank Bronze ingestion failed for %s: %s",
                indicator_code,
                error,
            )
            return IngestionResult(
                ingestion_run_id=run_id,
                country_code=country_code,
                indicator_code=indicator_code,
                outcome="failed",
                snapshot_id=snapshot_id,
                observation_count=observation_count,
                page_count=page_count,
                snapshot_path=snapshot_path,
                manifest_path=manifest_path,
                run_record_path=run_path,
                error=str(error),
            )

        record = _run_record(
            run_id=run_id,
            country_code=country_code,
            indicator_code=indicator_code,
            per_page=per_page,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            outcome=outcome,
            snapshot_id=snapshot_id,
            observation_count=observation_count,
            page_count=page_count,
            error=None,
        )
        run_path = self.store.write_run_record(record)
        logger.info(
            "Completed World Bank Bronze ingestion for %s with outcome %s",
            indicator_code,
            outcome,
        )
        return IngestionResult(
            ingestion_run_id=run_id,
            country_code=country_code,
            indicator_code=indicator_code,
            outcome=outcome,
            snapshot_id=snapshot_id,
            observation_count=observation_count,
            page_count=page_count,
            snapshot_path=snapshot_path,
            manifest_path=manifest_path,
            run_record_path=run_path,
        )


def _run_record(
    *,
    run_id: str,
    country_code: str,
    indicator_code: str,
    per_page: int,
    started_at: datetime,
    completed_at: datetime,
    outcome: str,
    snapshot_id: str | None,
    observation_count: int | None,
    page_count: int | None,
    error: Exception | None,
) -> JsonObject:
    return {
        "run_record_schema_version": RUN_RECORD_SCHEMA_VERSION,
        "source_id": "world_bank_indicators_api",
        "country_code": country_code,
        "indicator_code": indicator_code,
        "ingestion_run_id": run_id,
        "started_at_utc": started_at.isoformat(),
        "completed_at_utc": completed_at.isoformat(),
        "outcome": outcome,
        "snapshot_id": snapshot_id,
        "snapshot_action": outcome if outcome in {"created", "unchanged"} else None,
        "request_parameters": {"format": "json", "per_page": per_page},
        "page_count": page_count,
        "observation_count": observation_count,
        "validation_status": "passed" if outcome != "failed" else "failed",
        "error": (
            {
                "category": type(error).__name__,
                "message": _safe_error_message(error),
            }
            if error is not None
            else None
        ),
    }


def _safe_error_message(error: Exception) -> str:
    message = re.sub(r"[\r\n\t]+", " ", str(error)).strip()
    return message[:1000]
