"""Profile Kenya market-level food-price estimates and source metadata."""

from __future__ import annotations

import csv
import io
import math
from collections import Counter, defaultdict
from datetime import date
from typing import Any
from zipfile import ZipFile

from kenya_food_intelligence.sources.subnational import (
    CATALOG_URL,
    TABLE_INFO_URL,
    DownloadedRTFPResource,
    RTFPResource,
    validate_zip,
)

type JsonObject = dict[str, Any]

REQUIRED_FIELDS = (
    "ISO3",
    "country",
    "adm1_name",
    "adm2_name",
    "mkt_name",
    "geo_id",
    "price_date",
    "currency",
)
BUSINESS_KEY = ("ISO3", "geo_id", "price_date")
TICKER_FIELDS = (
    "iso3",
    "country",
    "ticker",
    "full_name",
    "units",
    "food_index_weight",
    "data_coverage",
    "r2_imputation",
)


class SubnationalProfileError(Exception):
    """The RTFP archive does not meet the expected profile contract."""


def profile_rtfp(
    *,
    version: str,
    resources: dict[str, RTFPResource],
    downloaded: dict[str, DownloadedRTFPResource],
    table_metadata: dict[str, object],
) -> JsonObject:
    """Profile source files in-place without extracting archive contents."""
    data_file = downloaded.get("market_data")
    details_file = downloaded.get("model_details")
    ticker_file = downloaded.get("ticker_info")
    if data_file is None or details_file is None or ticker_file is None:
        raise SubnationalProfileError("Required RTFP data or metadata file is missing")

    data_member = f"{data_file.path.stem}.csv"
    details_member = f"{details_file.path.stem}.csv"
    ticker_member = f"{ticker_file.path.stem}.csv"
    try:
        with ZipFile(data_file.path) as archive:
            validate_zip(data_file.path, data_member)
            with archive.open(data_member) as raw:
                reader = csv.DictReader(
                    io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                )
                data_profile = _profile_market_csv(reader)
        details = _read_single_record(details_file, details_member)
        tickers = _read_tickers(ticker_file, ticker_member)
    except SubnationalProfileError:
        raise
    except (OSError, UnicodeError, csv.Error, KeyError) as error:
        raise SubnationalProfileError(
            f"Unable to parse RTFP archive: {error}"
        ) from error

    table = table_metadata.get("metadata")
    if not isinstance(table, dict):
        raise SubnationalProfileError("World Bank table metadata is missing")
    dictionary = table.get("data_dictionary", [])
    if not isinstance(dictionary, list):
        raise SubnationalProfileError("World Bank data dictionary is malformed")
    actual_fields = set(data_profile["actual_csv_fields"])
    source_fields = {
        str(field.get("name")): {
            "source_type": field.get("data_type"),
            "label": field.get("label"),
            "unit_of_measurement": field.get("unit_of_measurement"),
        }
        for field in dictionary
        if (
            isinstance(field, dict)
            and isinstance(field.get("name"), str)
            and field.get("name") in actual_fields
        )
    }
    for field, summary in data_profile["field_schema"].items():
        summary.update(source_fields.get(field, {}))

    return {
        "source_id": "WORLDBANK_RTFP_KEN",
        "dataset_name": table.get("title"),
        "catalog_url": CATALOG_URL,
        "files_api_url": "https://microdata.worldbank.org/api/downloads/"
        "KEN_2021_RTFP_v02_M/files?type=data",
        "table_metadata_url": TABLE_INFO_URL,
        "author": "Bo Pieter Johannes Andrée; World Bank DECDG/DECCS",
        "access_policy": "Open access in the World Bank catalog",
        "price_type": (
            "The inspected catalog schema does not classify the series as "
            "retail or wholesale."
        ),
        "observation_classification": (
            "The catalog describes modeled monthly food-price estimates. "
            "The sparse base item fields are not explicitly identified as "
            "direct observations in the table dictionary."
        ),
        "license": (
            "No explicit license was found in the inspected World Bank study or "
            "table metadata; confirm terms before redistribution."
        ),
        "citation": (
            "Andrée, Bo Pieter Johannes. World Bank Development Data Group. "
            "Monthly food price estimates by product and market (Kenya)."
        ),
        "retrieved_files": {
            kind: {
                "filename": file.path.name,
                "source_url": file.resource.download_url,
                "version": file.resource.version,
                "retrieved_at_utc": file.retrieved_at_utc,
                "http_status": file.status,
                "content_type": file.content_type,
                "file_size_bytes": file.size_bytes,
                "checksum_sha256": file.checksum_sha256,
                "last_modified": file.last_modified,
                "etag": file.etag,
            }
            for kind, file in downloaded.items()
        },
        "source_revision": {
            "file_version": version,
            "table_updated_at": table.get("updated_at"),
            "global_table_observation_count": table_metadata.get("count"),
            "catalog_resources": {
                kind: {
                    "filename": resource.filename,
                    "source_url": resource.download_url,
                    "version": resource.version,
                    "file_size_bytes": resource.size_bytes,
                    "description": resource.description,
                }
                for kind, resource in resources.items()
            },
            "revision_policy": (
                "The catalog marks prior versions as archived and advises using "
                "the latest file; the source description says estimates are "
                "regularly revised as more price information becomes available."
            ),
            "data_description": table.get("description"),
        },
        "source_fields": source_fields,
        "profiling_outcome": "success",
        **data_profile,
        "model_details": details,
        "commodity_tickers": tickers,
    }


def _profile_market_csv(reader: csv.DictReader[str]) -> JsonObject:
    fields = list(reader.fieldnames or [])
    if len(fields) != len(set(fields)):
        raise SubnationalProfileError("RTFP CSV contains duplicate column names")
    missing_fields = sorted(set(REQUIRED_FIELDS) - set(fields))
    if missing_fields:
        raise SubnationalProfileError(
            f"RTFP CSV is missing required fields: {', '.join(missing_fields)}"
        )

    blank_counts: Counter[str] = Counter()
    invalid_numeric_counts: Counter[str] = Counter()
    numeric_possible = {field: True for field in fields}
    nonblank_counts: Counter[str] = Counter()
    price_names = {
        field
        for field in fields
        if field in {"beans", "maize_fao", "livestock_goat_s_fao"}
        or field.startswith(("o_", "h_", "l_", "c_"))
    }
    price_values: dict[str, list[float]] = {field: [] for field in price_names}
    periods: set[str] = set()
    calendar_months: set[tuple[int, int]] = set()
    market_keys: set[str] = set()
    business_keys: Counter[tuple[str, ...]] = Counter()
    raw_area_markets: dict[str, set[str]] = defaultdict(set)
    raw_admin1_markets: dict[str, set[str]] = defaultdict(set)
    market_labels: set[str] = set()
    market_average_ids: set[str] = set()
    currencies: Counter[str] = Counter()
    interpolation_flags: Counter[str] = Counter()
    examples: list[JsonObject] = []
    row_count = 0
    invalid_dates: list[str] = []
    non_kenya_rows = 0

    for row in reader:
        row_count += 1
        if None in row or any(value is None for value in row.values()):
            raise SubnationalProfileError(
                f"RTFP CSV row {row_count + 1} has a column-count mismatch"
            )
        for field in fields:
            value = row.get(field) or ""
            if not value:
                blank_counts[field] += 1
                continue
            nonblank_counts[field] += 1
            try:
                number = float(value)
            except ValueError:
                numeric_possible[field] = False
                if field in price_values:
                    invalid_numeric_counts[field] += 1
                continue
            if not math.isfinite(number):
                numeric_possible[field] = False
                if field in price_values:
                    invalid_numeric_counts[field] += 1
                continue
            if field in price_values:
                price_values[field].append(number)

        if row.get("ISO3") != "KEN" or row.get("country") != "Kenya":
            non_kenya_rows += 1
        market_id = row.get("geo_id") or ""
        date_text = row.get("price_date") or ""
        if market_id:
            market_keys.add(market_id)
        market_name = row.get("mkt_name") or ""
        if market_name:
            market_labels.add(market_name)
        if market_name == "Market Average" and market_id:
            market_average_ids.add(market_id)
        if date_text:
            try:
                parsed_date = date.fromisoformat(date_text)
                periods.add(date_text)
                calendar_months.add((parsed_date.year, parsed_date.month))
            except ValueError:
                invalid_dates.append(date_text)
        business_keys[tuple(row.get(field, "") or "" for field in BUSINESS_KEY)] += 1
        raw_area_markets[row.get("adm2_name") or "(missing)"].add(market_id)
        raw_admin1_markets[row.get("adm1_name") or "(missing)"].add(market_id)
        currencies[row.get("currency") or "(missing)"] += 1
        interpolation_flags[row.get("spatially_interpolated") or "(missing)"] += 1
        if len(examples) < 3:
            examples.append(
                {
                    field: row.get(field)
                    for field in (
                        "ISO3",
                        "adm1_name",
                        "adm2_name",
                        "mkt_name",
                        "geo_id",
                        "price_date",
                        "currency",
                        "beans",
                        "maize_fao",
                        "o_beans",
                        "o_maize_fao",
                        "spatially_interpolated",
                    )
                    if field in fields
                }
            )

    if row_count == 0:
        raise SubnationalProfileError("RTFP market CSV contains no observations")
    duplicate_rows = sum(count - 1 for count in business_keys.values() if count > 1)
    field_schema = {
        field: {
            "inferred_csv_type": (
                "numeric"
                if numeric_possible[field] and nonblank_counts[field]
                else "string"
            ),
            "blank_count": blank_counts[field],
            "nonblank_count": nonblank_counts[field],
        }
        for field in fields
    }
    expected_month_count = 0
    missing_month_count = 0
    if calendar_months:
        first_month = min(calendar_months)
        last_month = max(calendar_months)
        expected_month_count = (
            (last_month[0] - first_month[0]) * 12 + last_month[1] - first_month[1] + 1
        )
        missing_month_count = expected_month_count - len(calendar_months)
    price_statistics = {}
    for field, values in sorted(price_values.items()):
        quartiles = _quartiles(values)
        lower_fence = None
        upper_fence = None
        outlier_count = 0
        if quartiles is not None:
            first_quartile, third_quartile = quartiles
            spread = third_quartile - first_quartile
            lower_fence = first_quartile - 1.5 * spread
            upper_fence = third_quartile + 1.5 * spread
            outlier_count = sum(
                value < lower_fence or value > upper_fence for value in values
            )
        price_statistics[field] = {
            "non_null_count": len(values),
            "missing_count": blank_counts[field],
            "invalid_numeric_count": invalid_numeric_counts[field],
            "minimum": min(values) if values else None,
            "maximum": max(values) if values else None,
            "negative_count": sum(value < 0 for value in values),
            "zero_count": sum(value == 0 for value in values),
            "tukey_1_5_iqr_candidate_count": outlier_count,
            "tukey_1_5_iqr_bounds": (
                [lower_fence, upper_fence]
                if lower_fence is not None and upper_fence is not None
                else None
            ),
        }

    return {
        "observation_count": row_count,
        "actual_csv_fields": fields,
        "field_schema": field_schema,
        "business_key": list(BUSINESS_KEY),
        "business_key_duplicate_count": duplicate_rows,
        "business_key_unique_count": len(business_keys),
        "invalid_date_count": len(invalid_dates),
        "invalid_date_examples": invalid_dates[:5],
        "non_kenya_row_count": non_kenya_rows,
        "reference_period": {
            "minimum": min(periods) if periods else None,
            "maximum": max(periods) if periods else None,
            "distinct_months": len(periods),
            "expected_calendar_months": expected_month_count,
            "missing_calendar_months": missing_month_count,
        },
        "geography": {
            "unique_geo_ids": len(market_keys),
            "unique_market_labels": len(market_labels),
            "market_average_geo_ids": len(market_average_ids),
            "catalog_markets_excluding_aggregate": len(market_keys)
            - len(market_average_ids),
            "raw_admin1_market_counts": {
                key: len(value) for key, value in sorted(raw_admin1_markets.items())
            },
            "raw_admin2_market_counts": {
                key: len(value) for key, value in sorted(raw_area_markets.items())
            },
            "market_average_label_seen": bool(market_average_ids),
            "defensibly_mapped_current_counties": 0,
            "current_county_total": 47,
            "mapping_status": (
                "No defensible 47-county crosswalk: source supplies names but no "
                "county codes; raw admin-2 labels mix counties, localities and "
                "the Market Average aggregate."
            ),
        },
        "currencies": dict(currencies),
        "spatially_interpolated_flags": dict(interpolation_flags),
        "price_field_statistics": price_statistics,
        "representative_records": examples,
    }


def _read_single_record(downloaded: DownloadedRTFPResource, member: str) -> JsonObject:
    with ZipFile(downloaded.path) as archive:
        validate_zip(downloaded.path, member)
        with archive.open(member) as raw:
            reader = csv.DictReader(
                io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            )
            row = next(reader, None)
            if row is None:
                raise SubnationalProfileError(
                    f"RTFP metadata CSV is empty: {downloaded.path.name}"
                )
            if None in row or any(value is None for value in row.values()):
                raise SubnationalProfileError(
                    f"RTFP metadata CSV row has a column-count mismatch: "
                    f"{downloaded.path.name}"
                )
            return dict(row)


def _read_tickers(downloaded: DownloadedRTFPResource, member: str) -> list[JsonObject]:
    with ZipFile(downloaded.path) as archive:
        validate_zip(downloaded.path, member)
        with archive.open(member) as raw:
            reader = csv.DictReader(
                io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            )
            if reader.fieldnames is None:
                raise SubnationalProfileError(
                    f"RTFP ticker CSV has no header: {downloaded.path.name}"
                )
            missing = sorted(set(TICKER_FIELDS) - set(reader.fieldnames))
            if missing:
                raise SubnationalProfileError(
                    f"RTFP ticker CSV is missing fields: {', '.join(missing)}"
                )
            rows = []
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise SubnationalProfileError(
                        f"RTFP ticker CSV has a column-count mismatch: "
                        f"{downloaded.path.name}"
                    )
                rows.append(dict(row))
            return rows


def _quartiles(values: list[float]) -> tuple[float, float] | None:
    if len(values) < 4:
        return None
    ordered = sorted(values)
    return _percentile(ordered, 0.25), _percentile(ordered, 0.75)


def _percentile(values: list[float], percentile: float) -> float:
    position = (len(values) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] + (values[upper] - values[lower]) * weight
