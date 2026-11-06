from __future__ import annotations

import hashlib
import json
from pathlib import Path

from kenya_food_intelligence.storage.bronze import BronzeStore

SOURCE_ID = "knbs_cpi_monthly"
PUBLICATION = "2026-10"
PDF_BYTES = b"%PDF-1.4 example document bytes %%EOF"


def _publish(
    store: BronzeStore,
    payload_path: Path,
    filename: str,
    document_url: str,
) -> tuple[str, str, Path, dict[str, object]]:
    return store.publish_binary_snapshot(
        source_id=SOURCE_ID,
        publication_identity=PUBLICATION,
        payload_path=payload_path,
        filename=filename,
        expected_sha256=hashlib.sha256(PDF_BYTES).hexdigest(),
        expected_size_bytes=len(PDF_BYTES),
        metadata={
            "source": {
                "listing_url": "https://www.knbs.or.ke/cpi-and-inflation-rates/",
                "document_url": document_url,
            },
            "content_type": "application/pdf",
            "page_count": 1,
            "validation": {
                "status": "passed",
                "http_status_200": True,
                "pdf_signature": True,
                "eof_marker": True,
                "readable_pdf_structure": True,
                "page_count": 1,
                "reference_month_match": True,
                "source_title_month_match": True,
            },
            "attribution": "KNBS",
        },
    )


def test_renamed_identical_binary_reuses_snapshot_without_manifest_mutation(
    tmp_path: Path,
) -> None:
    store = BronzeStore(tmp_path / "store")
    first_download = tmp_path / "october-cpi.pdf"
    renamed_download = tmp_path / "october-cpi-revised-name.pdf"
    first_download.write_bytes(PDF_BYTES)
    renamed_download.write_bytes(PDF_BYTES)

    created, snapshot_id, snapshot_path, _ = _publish(
        store,
        first_download,
        "october-cpi.pdf",
        "https://www.knbs.or.ke/files/october-cpi.pdf",
    )
    manifest_before = (snapshot_path / "manifest.json").read_bytes()
    unchanged, repeated_id, repeated_path, _ = _publish(
        store,
        renamed_download,
        "october-cpi-revised-name.pdf",
        "https://www.knbs.or.ke/files/october-cpi-revised-name.pdf",
    )

    assert created == "created"
    assert unchanged == "unchanged"
    assert repeated_id == snapshot_id
    assert repeated_path == snapshot_path
    assert (snapshot_path / "manifest.json").read_bytes() == manifest_before
    assert (snapshot_path / "october-cpi.pdf").read_bytes() == PDF_BYTES
    assert not (snapshot_path / "october-cpi-revised-name.pdf").exists()

    descriptor = json.loads((snapshot_path / "descriptor.json").read_text())
    assert descriptor["descriptor_schema_version"] == 2
    assert descriptor["provenance"]["filename"] == "october-cpi.pdf"
