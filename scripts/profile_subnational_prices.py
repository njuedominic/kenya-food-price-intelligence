#!/usr/bin/env python3
"""Profile official Kenya market food-price sources for feasibility."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kenya_food_intelligence.profiling.subnational import (
    SubnationalProfileError,
    profile_rtfp,
)
from kenya_food_intelligence.sources.subnational import (
    SubnationalSourceError,
    WorldBankRTFPClient,
)

WFP_PACKAGE_URL = "https://data.humdata.org/dataset/wfp-food-prices-for-kenya"
WFP_API_DOCS_URL = (
    "https://hdx-hapi.readthedocs.io/en/latest/data_usage_guides/"
    "food_security_nutrition_and_poverty/"
)
NDMA_LISTING_URL = (
    "https://knowledgeweb.ndma.go.ke/Public/Resources/CountyBulletins.aspx?ID=11"
)
NDMA_DETAIL_URL = (
    "https://knowledgeweb.ndma.go.ke/Public/Resources/ResourceDetails.aspx?"
    "doc=27c777ce-d9a9-499d-bd4e-4b6413b9d49b"
)
NDMA_PDF_URL = (
    "https://knowledgeweb.ndma.go.ke/Library/doclink.aspx?"
    "document=27c777ce-d9a9-499d-bd4e-4b6413b9d49b"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--backoff-factor", type=float, default=0.5)
    parser.add_argument(
        "--download-dir",
        type=Path,
        default=Path("data/landing/subnational/world_bank"),
        help="Directory for ignored Kenya-only RTFP archives",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("reports/source_profiling"),
        help="Directory for sanitized JSON profiles",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report: dict[str, Any] = {
        "report_name": "Kenya subnational food-price feasibility",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "profiling_outcome": "failed",
        "recommendation": (
            "B. Approve only a restricted pilot for selected markets. Keep World "
            "Bank model estimates explicitly labelled, avoid county aggregation, "
            "and do not treat them as measured prices or official county inflation."
        ),
        "secondary_source_evidence": _secondary_source_evidence(),
    }
    try:
        client = WorldBankRTFPClient(
            timeout=args.timeout,
            max_retries=args.max_retries,
            backoff_factor=args.backoff_factor,
        )
        version, resources = client.discover_resources()
        table_metadata = client.fetch_table_metadata()
        downloaded = client.download_resources(resources, args.download_dir)
        report["world_bank_rtfp"] = profile_rtfp(
            version=version,
            resources=resources,
            downloaded=downloaded,
            table_metadata=table_metadata,
        )
    except ValueError as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        path = _write_report(args.output_dir, report)
        print(
            f"Configuration error: {error}; report written to {path}", file=sys.stderr
        )
        return 2
    except (SubnationalSourceError, SubnationalProfileError) as error:
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        path = _write_report(args.output_dir, report)
        print(
            f"Subnational profiling failed: {error}; report written to {path}",
            file=sys.stderr,
        )
        return 1

    report["profiling_outcome"] = "success"
    report["generated_at_utc"] = datetime.now(UTC).isoformat()
    path = _write_report(args.output_dir, report)
    print(f"Feasibility report written to {path}")
    return 0


def _secondary_source_evidence() -> dict[str, Any]:
    return {
        "wfp": {
            "official_dataviz_url": (
                "https://dataviz.vam.wfp.org/eastern-africa/kenya/economic/prices"
            ),
            "hdx_dataset_url": WFP_PACKAGE_URL,
            "hapi_documentation_url": WFP_API_DOCS_URL,
            "dataset_title": "Kenya - Food Prices",
            "dataset_version": (
                "No immutable Kenya release version exposed in the inspected "
                "HDX metadata."
            ),
            "dataset_metadata_modified": "2026-11-05T21:00:59.677527",
            "resource_last_modified": "2026-11-04T01:24:21.670860",
            "license": (
                "Creative Commons Attribution for Intergovernmental "
                "Organisations (CC BY-IGO)"
            ),
            "update_frequency": "Weekly release cadence; mostly monthly observations",
            "coverage_note": (
                "HAPI documentation says the global database reaches back to "
                "1992 for a few countries and many countries began in 2003 or "
                "later; Kenya-specific history was not verified."
            ),
            "revision_policy": (
                "In-place revisions versus append-only updates were not "
                "established; preserve snapshots and checksums."
            ),
            "dataviz_access_note": (
                "The WFP DataViz page displayed a loading state and a failed "
                "resource in the inspection session; no page observations were "
                "confirmed there."
            ),
            "retrieval_probe": {
                "retrieved_at_utc": "2026-11-06T12:36:55.113567+00:00",
                "data_downloaded": False,
                "reason": (
                    "The official HDX resource links returned HTTP 202 with an "
                    "empty HTML response in this environment. No access control "
                    "was bypassed."
                ),
                "resources": [
                    {
                        "resource_id": "517ee1bf-2437-4f8c-aa1b-cb9925b9d437",
                        "filename": "wfp_food_prices_ken.csv",
                        "source_url": (
                            "https://data.humdata.org/dataset/"
                            "e0d3fba6-f9a2-45d7-b949-140c455197ff/resource/"
                            "517ee1bf-2437-4f8c-aa1b-cb9925b9d437/download/"
                            "wfp_food_prices_ken.csv"
                        ),
                        "catalog_size_bytes": 3793902,
                        "catalog_md5": "6dda86354ca220f162a77274c3f59b20",
                        "attempted_at_utc": "2026-11-06T12:36:53.417148+00:00",
                        "http_status": 202,
                        "content_type": "text/html; charset=UTF-8",
                        "response_body_size_bytes": 0,
                    },
                    {
                        "resource_id": "fde6f6c7-9776-4b52-a826-d77f9d3f6688",
                        "filename": "wfp_markets_ken.csv",
                        "source_url": (
                            "https://data.humdata.org/dataset/"
                            "e0d3fba6-f9a2-45d7-b949-140c455197ff/resource/"
                            "fde6f6c7-9776-4b52-a826-d77f9d3f6688/download/"
                            "wfp_markets_ken.csv"
                        ),
                        "catalog_size_bytes": 14282,
                        "catalog_md5": "91fab679cc0d0d5edc1c3d64e0d5204a",
                        "attempted_at_utc": "2026-11-06T12:36:55.113567+00:00",
                        "http_status": 202,
                        "content_type": "text/html; charset=UTF-8",
                        "response_body_size_bytes": 0,
                    },
                ],
            },
            "hapi_api": {
                "documented_endpoint": (
                    "https://hapi.humdata.org/api/v2/"
                    "food-security-nutrition-poverty/food-prices-market-monitor"
                ),
                "required_identifier": (
                    "Documented app_identifier containing an application name "
                    "and contact email; no user email was supplied, so no "
                    "identifier was fabricated or sent."
                ),
                "documented_fields": [
                    "resource_hdx_id",
                    "commodity_code",
                    "commodity_name",
                    "commodity_category",
                    "price_flag",
                    "price_type",
                    "price",
                    "unit",
                    "currency_code",
                    "reference_period_start",
                    "reference_period_end",
                    "location_code",
                    "location_name",
                    "admin1_code",
                    "admin1_name",
                    "admin2_code",
                    "admin2_name",
                    "admin_level",
                    "market_code",
                    "market_name",
                    "lat",
                    "lon",
                ],
            },
            "data_quality_status": (
                "Official metadata and documented schema verified; Kenya rows "
                "and market coverage not independently downloaded or profiled."
            ),
            "observation_status": (
                "WFP Price Database records are a candidate market-price source, "
                "but direct observation status and flags were not verified in "
                "Kenya rows because no CSV response was obtained."
            ),
        },
        "ndma": {
            "listing_url": NDMA_LISTING_URL,
            "report_detail_url": NDMA_DETAIL_URL,
            "download_url": NDMA_PDF_URL,
            "final_document_url": (
                "https://knowledgeweb.ndma.go.ke/Content/LibraryDocuments/"
                "Kilifi_County_Drought_Early_Warning_Bulletin_Sept_202620261026152610.pdf"
            ),
            "title": "Kilifi County Drought Early Warning Bulletin Sept 2026",
            "reference_period": "September 2026, as at 30 September 2026",
            "pdf_creation_date": "2026-10-26T15:24:45+03:00",
            "retrieved_at_utc": "2026-11-06T12:37:33.825242+00:00",
            "http_status": 200,
            "content_type": "application/pdf",
            "file_size_bytes": 891702,
            "checksum_sha256": (
                "e04d091f9397822cd08870ea94f20ec2853ed61969a5ac37444d4ff50f5bd6f9"
            ),
            "pdf_pages": 20,
            "extraction_method": "pypdf text extraction; no OCR",
            "extracted_price_evidence": [
                {
                    "page": 9,
                    "geographic_area": "Kilifi County average",
                    "commodity": "Maize",
                    "reference_month": "September 2026",
                    "reported_price": 48.8,
                    "currency": "KES",
                    "unit": "kg",
                    "evidence": (
                        "Narrative says average price per kilo decreased from "
                        "KES 50.30 to KES 48.8."
                    ),
                },
                {
                    "page": 9,
                    "geographic_area": "Kilifi County average",
                    "commodity": "Beans",
                    "reference_month": "September 2026",
                    "reported_price": 144.0,
                    "currency": "KES",
                    "unit": "kg",
                    "evidence": "Narrative reports KES 144.0 per kilo.",
                    "ambiguity": (
                        "The page 10 chart text extraction gives a September "
                        "beans value of KES 144.1/kg; retain both source readings."
                    ),
                },
            ],
            "license": (
                "The PDF footer states © 2026 NDMA, All Rights Reserved; no "
                "open reuse license was identified."
            ),
            "restriction": (
                "One publicly listed bulletin inspected; extracted county "
                "averages do not identify individual markets."
            ),
        },
    }


def _write_report(output_dir: Path, report: dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / f"subnational_feasibility_{timestamp}.json"
    path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


if __name__ == "__main__":
    raise SystemExit(main())
