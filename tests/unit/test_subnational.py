from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from kenya_food_intelligence.profiling.subnational import (
    SubnationalProfileError,
    profile_rtfp,
)
from kenya_food_intelligence.sources.subnational import (
    DownloadedRTFPResource,
    RTFPResource,
    SubnationalSourceError,
    WorldBankRTFPClient,
    validate_zip,
)

VERSION = "2026-11-02"
DATA_FIELDS = [
    "ISO3",
    "country",
    "adm1_name",
    "adm2_name",
    "mkt_name",
    "lat",
    "lon",
    "geo_id",
    "price_date",
    "year",
    "month",
    "currency",
    "components",
    "start_dense_data",
    "last_survey_point",
    "data_coverage",
    "data_coverage_recent",
    "index_confidence_score",
    "spatially_interpolated",
    "beans",
    "maize_fao",
    "o_beans",
    "o_maize_fao",
]


def _row(
    *,
    market: str = "Market A",
    geo_id: str = "market-a",
    date: str = "2026-09-01",
    beans: str = "100",
    maize: str = "200",
) -> dict[str, str]:
    row = {field: "" for field in DATA_FIELDS}
    row.update(
        {
            "ISO3": "KEN",
            "country": "Kenya",
            "adm1_name": "Coast",
            "adm2_name": "Kilifi",
            "mkt_name": market,
            "lat": "-3.6",
            "lon": "39.8",
            "geo_id": geo_id,
            "price_date": date,
            "year": date[:4],
            "month": str(int(date[5:7])),
            "currency": "KES",
            "components": "beans (1 KG), maize_fao (1 Kg)",
            "start_dense_data": "Jan 2007",
            "last_survey_point": "Sep 2026",
            "data_coverage": "4.49",
            "data_coverage_recent": "5.7",
            "index_confidence_score": "0.97",
            "spatially_interpolated": "0",
            "beans": beans,
            "maize_fao": maize,
            "o_beans": "101",
            "o_maize_fao": "201",
        }
    )
    return row


def _csv_content(fieldnames: list[str], rows: list[dict[str, str]]) -> str:
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def _write_archive(
    path: Path,
    csv_name: str,
    csv_content: str,
    *,
    extra_member: tuple[str, str] | None = None,
) -> None:
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(csv_name, csv_content)
        if extra_member is not None:
            archive.writestr(*extra_member)


def _downloaded(
    path: Path, kind: str, csv_content: str, csv_name: str
) -> DownloadedRTFPResource:
    _write_archive(path, csv_name, csv_content)
    resource = RTFPResource(
        kind=kind,
        filename=path.name,
        version=VERSION,
        download_url=f"https://microdata.worldbank.org/{path.name}",
        size_bytes=path.stat().st_size,
        description="Fixture",
    )
    return DownloadedRTFPResource(
        resource=resource,
        path=path,
        retrieved_at_utc=datetime.now(UTC).isoformat(),
        status=200,
        content_type="application/zip",
        size_bytes=path.stat().st_size,
        checksum_sha256="fixture-checksum",
        final_url=resource.download_url,
        last_modified=None,
        etag=None,
    )


def _fixtures(tmp_path: Path, rows: list[dict[str, str]]) -> tuple:
    data_name = "KEN_RTFP_mkt_2007_2026-11-02.csv"
    details_name = "KEN_RTP_details_mkt_2007_2026-11-02.csv"
    ticker_name = "KEN_RTP_ticker_info_mkt_2007_2026-11-02.csv"
    downloaded = {
        "market_data": _downloaded(
            tmp_path / f"KEN_RTFP_mkt_2007_{VERSION}.zip",
            "market_data",
            _csv_content(DATA_FIELDS, rows),
            data_name,
        ),
        "model_details": _downloaded(
            tmp_path / f"KEN_RTP_details_mkt_2007_{VERSION}.zip",
            "model_details",
            _csv_content(
                ["country", "iso3", "currency", "imputation_model"],
                [
                    {
                        "country": "Kenya",
                        "iso3": "KEN",
                        "currency": "KES",
                        "imputation_model": "maize_fao: cubist (Andree, 2021)",
                    }
                ],
            ),
            details_name,
        ),
        "ticker_info": _downloaded(
            tmp_path / f"KEN_RTP_ticker_info_mkt_2007_{VERSION}.zip",
            "ticker_info",
            _csv_content(
                [
                    "iso3",
                    "country",
                    "ticker",
                    "full_name",
                    "units",
                    "food_index_weight",
                    "data_coverage",
                    "r2_imputation",
                ],
                [
                    {
                        "iso3": "KEN",
                        "country": "Kenya",
                        "ticker": "maize_fao",
                        "full_name": "Maize (white)",
                        "units": "Kg",
                        "food_index_weight": "1",
                        "data_coverage": "5.46%",
                        "r2_imputation": "0.97",
                    }
                ],
            ),
            ticker_name,
        ),
    }
    resources = {kind: value.resource for kind, value in downloaded.items()}
    metadata = {
        "metadata": {
            "title": "Monthly food price estimates by product and market",
            "updated_at": "2026-11-05",
            "data_dictionary": [
                {
                    "name": "maize_fao",
                    "data_type": "string",
                    "label": "Maize (white)",
                    "unit_of_measurement": None,
                }
            ],
        },
        "updated_at": "2026-11-05",
        "count": 762728,
    }
    return resources, downloaded, metadata


def test_discovery_selects_latest_same_version_kenya_files() -> None:
    files = []
    for version, resource_id in (
        ("2026-10-05", 1),
        (VERSION, 2),
    ):
        for prefix in (
            "KEN_RTFP_mkt_",
            "KEN_RTP_details_mkt_",
            "KEN_RTP_ticker_info_mkt_",
        ):
            filename = f"{prefix}2007_{version}.zip"
            files.append(
                {
                    "filename": filename,
                    "file_size_bytes": "100",
                    "description": "Fixture",
                    "data_access_type": "open",
                    "links": {
                        "download": (
                            "https://microdata.worldbank.org/api/downloads/"
                            f"download/KEN/{resource_id}?filename={filename}"
                        )
                    },
                }
            )
    body = json.dumps({"status": "success", "files": files}).encode()
    client = WorldBankRTFPClient(max_retries=0)
    client.http = SimpleNamespace(
        get=lambda *_args, **_kwargs: SimpleNamespace(status=200, body=body)
    )

    version, resources = client.discover_resources()

    assert version == VERSION
    assert set(resources) == {"market_data", "model_details", "ticker_info"}
    assert all(resource.version == VERSION for resource in resources.values())


def test_discovery_rejects_download_urls_outside_world_bank() -> None:
    files = []
    for prefix in (
        "KEN_RTFP_mkt_",
        "KEN_RTP_details_mkt_",
        "KEN_RTP_ticker_info_mkt_",
    ):
        filename = f"{prefix}2007_{VERSION}.zip"
        files.append(
            {
                "filename": filename,
                "file_size_bytes": "100",
                "data_access_type": "open",
                "links": {
                    "download": f"https://example.invalid/{filename}",
                },
            }
        )
    body = json.dumps({"status": "success", "files": files}).encode()
    client = WorldBankRTFPClient(max_retries=0)
    client.http = SimpleNamespace(
        get=lambda *_args, **_kwargs: SimpleNamespace(status=200, body=body)
    )

    with pytest.raises(SubnationalSourceError, match="non-World Bank"):
        client.discover_resources()


def test_profile_reports_missingness_duplicates_and_market_aggregate(
    tmp_path: Path,
) -> None:
    rows = [
        _row(),
        _row(),  # duplicate market-month business key
        _row(
            market="Market Average",
            geo_id="market-average",
            date="2026-10-01",
            beans="",
            maize="250",
        ),
    ]
    resources, downloaded, metadata = _fixtures(tmp_path, rows)

    result = profile_rtfp(
        version=VERSION,
        resources=resources,
        downloaded=downloaded,
        table_metadata=metadata,
    )

    assert result["observation_count"] == 3
    assert result["business_key"] == ["ISO3", "geo_id", "price_date"]
    assert result["business_key_duplicate_count"] == 1
    assert result["reference_period"]["distinct_months"] == 2
    assert result["geography"]["unique_geo_ids"] == 2
    assert result["geography"]["market_average_geo_ids"] == 1
    assert result["geography"]["catalog_markets_excluding_aggregate"] == 1
    assert result["geography"]["defensibly_mapped_current_counties"] == 0
    assert result["field_schema"]["maize_fao"]["source_type"] == "string"
    assert result["field_schema"]["maize_fao"]["inferred_csv_type"] == "numeric"
    assert result["price_field_statistics"]["beans"]["missing_count"] == 1
    assert result["commodity_tickers"][0]["units"] == "Kg"
    assert result["model_details"]["imputation_model"].startswith("maize_fao")


def test_profile_flags_tukey_candidates_without_removing_values(
    tmp_path: Path,
) -> None:
    rows = [
        _row(
            market=f"Market {month}",
            geo_id=f"market-{month}",
            date=f"2026-{month:02d}-01",
            beans=str(value),
        )
        for month, value in enumerate((1, 2, 2, 3, 100), start=1)
    ]
    resources, downloaded, metadata = _fixtures(tmp_path, rows)

    result = profile_rtfp(
        version=VERSION,
        resources=resources,
        downloaded=downloaded,
        table_metadata=metadata,
    )

    beans = result["price_field_statistics"]["beans"]
    assert beans["non_null_count"] == 5
    assert beans["minimum"] == 1
    assert beans["maximum"] == 100
    assert beans["tukey_1_5_iqr_candidate_count"] == 1


def test_profile_reports_non_numeric_price_values(tmp_path: Path) -> None:
    resources, downloaded, metadata = _fixtures(tmp_path, [_row(beans="not-a-price")])

    result = profile_rtfp(
        version=VERSION,
        resources=resources,
        downloaded=downloaded,
        table_metadata=metadata,
    )

    assert result["price_field_statistics"]["beans"]["missing_count"] == 0
    assert result["price_field_statistics"]["beans"]["invalid_numeric_count"] == 1
    assert result["field_schema"]["beans"]["inferred_csv_type"] == "string"


def test_profile_rejects_missing_required_csv_fields(tmp_path: Path) -> None:
    resources, downloaded, metadata = _fixtures(tmp_path, [_row()])
    data_name = f"KEN_RTFP_mkt_2007_{VERSION}.csv"
    _write_archive(
        downloaded["market_data"].path,
        data_name,
        "ISO3,country\nKEN,Kenya\n",
    )

    with pytest.raises(SubnationalProfileError, match="missing required fields"):
        profile_rtfp(
            version=VERSION,
            resources=resources,
            downloaded=downloaded,
            table_metadata=metadata,
        )


def test_profile_rejects_misaligned_csv_rows(tmp_path: Path) -> None:
    resources, downloaded, metadata = _fixtures(tmp_path, [_row()])
    data_name = f"KEN_RTFP_mkt_2007_{VERSION}.csv"
    _write_archive(
        downloaded["market_data"].path,
        data_name,
        ",".join(DATA_FIELDS) + "\n" + ",".join(["KEN", "Kenya", "extra"]) + "\n",
    )

    with pytest.raises(SubnationalProfileError, match="column-count mismatch"):
        profile_rtfp(
            version=VERSION,
            resources=resources,
            downloaded=downloaded,
            table_metadata=metadata,
        )


def test_invalid_dates_are_flagged_without_silent_correction(tmp_path: Path) -> None:
    resources, downloaded, metadata = _fixtures(tmp_path, [_row(date="2026-13-01")])

    result = profile_rtfp(
        version=VERSION,
        resources=resources,
        downloaded=downloaded,
        table_metadata=metadata,
    )

    assert result["invalid_date_count"] == 1
    assert result["invalid_date_examples"] == ["2026-13-01"]
    assert result["reference_period"]["missing_calendar_months"] == 0


def test_zip_validation_rejects_unsafe_paths_and_corrupt_archives(
    tmp_path: Path,
) -> None:
    unsafe = tmp_path / "unsafe.zip"
    _write_archive(
        unsafe,
        "prices.csv",
        "a\n1\n",
        extra_member=("../outside.txt", "do not extract"),
    )
    with pytest.raises(SubnationalSourceError, match="Unsafe"):
        validate_zip(unsafe, "prices.csv")

    invalid = tmp_path / "invalid.zip"
    invalid.write_bytes(b"not a zip")
    with pytest.raises(SubnationalSourceError, match="Invalid RTFP ZIP"):
        validate_zip(invalid, "prices.csv")
