"""Profile Kenya observations in the normalized FAOSTAT Producer Prices ZIP."""

from __future__ import annotations

import csv
import io
import math
import re
import statistics
from collections import Counter, defaultdict
from typing import Any
from zipfile import ZipFile

from kenya_food_intelligence.sources.faostat import (
    DATA_CSV_MEMBER,
    MAX_MEMBER_BYTES,
    DownloadedDataset,
    validate_archive,
)

type JsonObject = dict[str, Any]

AREA = "Kenya"
REQUIRED_FIELDS = (
    "Area Code",
    "Area Code (M49)",
    "Area",
    "Item Code",
    "Item Code (CPC)",
    "Item",
    "Element Code",
    "Element",
    "Year Code",
    "Year",
    "Months Code",
    "Months",
    "Unit",
    "Value",
    "Flag",
)
LOOKUP_MEMBERS = {
    "area": ("Prices_E_AreaCodes.csv", ("Area Code", "M49 Code", "Area")),
    "item": ("Prices_E_ItemCodes.csv", ("Item Code", "CPC Code", "Item")),
    "element": ("Prices_E_Elements.csv", ("Element Code", "Element")),
    "flag": ("Prices_E_Flags.csv", ("Flag", " Description")),
}
EXPECTED_UNITS = {
    "5530": "LCU",
    "5531": "SLC",
    "5532": "USD",
    "5539": "",
}
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
BUSINESS_KEY = (
    "Area Code",
    "Item Code",
    "Element Code",
    "Year Code",
    "Months Code",
)
COMPARABLE_ITEMS = {"Maize (corn)", "Beans, dry", "Potatoes", "Tomatoes"}


class FaostatProfileError(Exception):
    """The FAOSTAT archive or Kenya source rows violate the profile contract."""


def profile_dataset(downloaded: DownloadedDataset) -> JsonObject:
    """Validate and profile the Kenya rows without extracting the ZIP to disk."""
    members = validate_archive(downloaded.path, downloaded.source.data_member)
    if DATA_CSV_MEMBER not in members:
        raise FaostatProfileError(f"Missing normalized data member {DATA_CSV_MEMBER}")
    try:
        with ZipFile(downloaded.path) as archive:
            lookups = _read_lookups(archive, members)
            member_info = archive.getinfo(DATA_CSV_MEMBER)
            if member_info.file_size > MAX_MEMBER_BYTES:
                raise FaostatProfileError(
                    f"Normalized CSV exceeds the size limit: {member_info.file_size}"
                )
            with archive.open(DATA_CSV_MEMBER) as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                result = _profile_csv(text, lookups)
    except (OSError, UnicodeError, csv.Error) as error:
        raise FaostatProfileError(f"Unable to parse FAOSTAT CSV: {error}") from error

    return {
        "source_id": "FAOSTAT_PP",
        "dataset_id": downloaded.source.dataset_id,
        "dataset_name": "Producer Prices",
        "dataset_url": downloaded.source.dataset_url,
        "domain_url": downloaded.source.domain_url,
        "download_url": downloaded.source.download_url,
        "retrieved_at_utc": downloaded.retrieved_at_utc,
        "http": {
            "status": downloaded.http_status,
            "content_type": downloaded.content_type,
            "last_modified": downloaded.last_modified,
            "etag": downloaded.etag,
        },
        "archive": {
            "filename": downloaded.path.name,
            "size_bytes": downloaded.file_size_bytes,
            "checksum_sha256": downloaded.checksum_sha256,
            "integrity_check": "ZIP CRC check passed",
            "extracted_to_disk": False,
            "members": members,
            "data_member_uncompressed_bytes": member_info.file_size,
        },
        "dataset_update_metadata": {
            "faostat_ui_last_update": "2026-10-09",
            "catalog_data_revision": "2025-12-04",
            "verified_on": "2026-11-06",
            "note": (
                "The FAOSTAT Producer Prices page and the linked catalog report "
                "different update/revision dates; retain both source claims."
            ),
        },
        "license_attribution": {
            "license": "Creative Commons Attribution 4.0 International (CC BY 4.0)",
            "attribution_template": (
                "FAO. 2026. FAOSTAT: Producer Prices. Accessed 06 November 2026. "
                "https://www.fao.org/faostat/en/#data/PP Licence: CC-BY-4.0."
            ),
            "terms_url": ("https://www.fao.org/contact-us/terms/db-terms-of-use/en/"),
            "third_party_caveat": (
                "FAO terms warn that third-party data may have additional "
                "restrictions; check dataset metadata before redistribution."
            ),
        },
        "profiling_outcome": "success",
        **result,
    }


def profile_csv(
    text: io.TextIOBase, lookups: dict[str, dict[str, dict[str, str]]]
) -> JsonObject:
    """Profile a CSV text stream, exposed for controlled fixture tests."""
    return _profile_csv(text, lookups)


def _read_lookups(
    archive: ZipFile, members: list[str]
) -> dict[str, dict[str, dict[str, str]]]:
    result: dict[str, dict[str, dict[str, str]]] = {}
    member_set = set(members)
    for kind, (member, required_columns) in LOOKUP_MEMBERS.items():
        if member not in member_set:
            raise FaostatProfileError(f"Missing source code table {member!r}")
        with archive.open(member) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            reader = csv.DictReader(text)
            _validate_columns(reader.fieldnames, required_columns, member)
            rows = {
                (row.get(required_columns[0]) or ""): row
                for row in reader
                if row.get(required_columns[0])
            }
        result[kind] = rows
    return result


def _profile_csv(
    text: io.TextIOBase, lookups: dict[str, dict[str, dict[str, str]]]
) -> JsonObject:
    reader = csv.DictReader(text)
    _validate_columns(reader.fieldnames, REQUIRED_FIELDS, DATA_CSV_MEMBER)
    fieldnames = list(reader.fieldnames or [])

    null_counts: Counter[str] = Counter()
    value_type_counts: dict[str, Counter[str]] = {
        field: Counter() for field in fieldnames
    }
    frequency_counts: Counter[str] = Counter()
    item_counts: Counter[tuple[str, str]] = Counter()
    element_counts: Counter[tuple[str, str]] = Counter()
    element_frequency_counts: Counter[tuple[str, str, str]] = Counter()
    unit_counts: Counter[tuple[str, str]] = Counter()
    unit_frequency_counts: Counter[tuple[str, str, str]] = Counter()
    flag_counts: Counter[str] = Counter()
    year_sets: dict[str, set[int]] = defaultdict(set)
    month_sets: dict[str, set[str]] = defaultdict(set)
    business_keys: Counter[tuple[str, ...]] = Counter()
    duplicate_examples: list[JsonObject] = []
    unknown_items: list[JsonObject] = []
    unknown_item_count = 0
    unknown_elements: list[JsonObject] = []
    unknown_element_count = 0
    invalid_country_codes: list[JsonObject] = []
    invalid_country_code_count = 0
    unknown_flags: Counter[str] = Counter()
    unexpected_units: list[JsonObject] = []
    unexpected_unit_count = 0
    unexpected_months: Counter[str] = Counter()
    invalid_years: list[JsonObject] = []
    invalid_year_count = 0
    invalid_values: list[JsonObject] = []
    invalid_value_count = 0
    negative_values: list[JsonObject] = []
    negative_value_count = 0
    zero_values: list[JsonObject] = []
    zero_value_count = 0
    samples_by_element: dict[str, list[JsonObject]] = defaultdict(list)
    comparable_samples: dict[str, dict[str, JsonObject]] = defaultdict(dict)
    series_values: dict[
        tuple[str, str, str, str], list[tuple[int, float, JsonObject]]
    ] = defaultdict(list)
    periods_by_series: dict[tuple[str, str, str, str], set[int]] = defaultdict(set)
    area_code_seen: set[tuple[str, str]] = set()
    total_rows = 0
    kenya_rows = 0

    area_lookup = lookups["area"]
    item_lookup = lookups["item"]
    element_lookup = lookups["element"]
    flag_lookup = lookups["flag"]
    expected_country_codes = {
        code for code, row in area_lookup.items() if row.get("Area") == AREA
    }
    expected_m49_codes = {
        row.get("M49 Code", "")
        for row in area_lookup.values()
        if row.get("Area") == AREA
    }
    for row_number, row in enumerate(reader, start=2):
        total_rows += 1
        if None in row:
            raise FaostatProfileError(
                f"CSV row {row_number} has more fields than the header"
            )
        if row.get("Area") != AREA:
            continue
        kenya_rows += 1
        normalized = {field: row.get(field) for field in fieldnames}
        for field in fieldnames:
            value = row.get(field)
            if value is None or not value.strip():
                null_counts[field] += 1
                value_type_counts[field]["null"] += 1
            else:
                value_type_counts[field][_infer_type(value)] += 1

        area_code = row.get("Area Code", "")
        m49_code = row.get("Area Code (M49)", "")
        area_code_seen.add((area_code, m49_code))
        area_reference = area_lookup.get(area_code)
        if (
            area_reference is None
            or area_reference.get("Area") != AREA
            or m49_code not in expected_m49_codes
            or area_code not in expected_country_codes
        ):
            invalid_country_code_count += 1
            _append_limited(
                invalid_country_codes,
                {
                    "row_number": row_number,
                    "area_code": area_code,
                    "m49_code": m49_code,
                },
            )

        item_code = row.get("Item Code", "")
        element_code = row.get("Element Code", "")
        item = row.get("Item", "")
        element = row.get("Element", "")
        unit = row.get("Unit", "") or ""
        flag = row.get("Flag", "") or ""
        item_counts[(item_code, item)] += 1
        element_counts[(element_code, element)] += 1
        unit_counts[(element, unit)] += 1
        flag_counts[flag or "(missing)"] += 1

        if item_code not in item_lookup:
            unknown_item_count += 1
            _append_limited(
                unknown_items,
                {"row_number": row_number, "item_code": item_code, "item": item},
            )
        if element_code not in element_lookup:
            unknown_element_count += 1
            _append_limited(
                unknown_elements,
                {
                    "row_number": row_number,
                    "element_code": element_code,
                    "element": element,
                },
            )
        if flag and flag not in flag_lookup:
            unknown_flags[flag] += 1
        if element_code in EXPECTED_UNITS and unit != EXPECTED_UNITS[element_code]:
            unexpected_unit_count += 1
            _append_limited(
                unexpected_units,
                {
                    "row_number": row_number,
                    "element_code": element_code,
                    "element": element,
                    "unit": unit,
                    "expected_unit": EXPECTED_UNITS[element_code],
                },
            )

        year_value = _parse_year(row.get("Year"))
        if year_value is None or row.get("Year Code") != str(year_value):
            invalid_year_count += 1
            _append_limited(
                invalid_years,
                {
                    "row_number": row_number,
                    "year": row.get("Year"),
                    "year_code": row.get("Year Code"),
                },
            )
            continue
        months = row.get("Months", "")
        frequency = "annual" if months == "Annual value" else "monthly"
        if frequency == "monthly" and months not in MONTHS:
            unexpected_months[months or "(missing)"] += 1
        year_sets[frequency].add(year_value)
        if frequency == "monthly":
            month_sets[frequency].add(months)
        frequency_counts[frequency] += 1
        element_frequency_counts[(element_code, element, frequency)] += 1
        unit_frequency_counts[(element, unit, frequency)] += 1

        key = tuple(row.get(field) or "" for field in BUSINESS_KEY)
        business_keys[key] += 1
        if business_keys[key] > 1:
            _append_limited(
                duplicate_examples,
                {
                    "row_number": row_number,
                    "business_key": dict(zip(BUSINESS_KEY, key, strict=True)),
                    "duplicate_count": business_keys[key],
                },
            )

        value_text = row.get("Value")
        value = _parse_number(value_text)
        if value_text and value is None:
            invalid_value_count += 1
            _append_limited(
                invalid_values,
                {
                    "row_number": row_number,
                    "element_code": element_code,
                    "value": value_text,
                },
            )
        if value is None:
            continue
        if value < 0:
            negative_value_count += 1
            _append_limited(
                negative_values,
                {
                    "row_number": row_number,
                    "item": item,
                    "element": element,
                    "value": value,
                },
            )
        if value == 0:
            zero_value_count += 1
            _append_limited(
                zero_values,
                {
                    "row_number": row_number,
                    "item": item,
                    "element": element,
                    "value": value,
                },
            )

        period = (
            year_value * 12 + MONTHS.index(months)
            if frequency == "monthly" and months in MONTHS
            else year_value
        )
        series_key = (item_code, element_code, unit, frequency)
        series_row = {
            "row_number": row_number,
            "item_code": item_code,
            "item": item,
            "element_code": element_code,
            "element": element,
            "year": year_value,
            "month": months,
            "unit": unit,
            "value": value,
            "flag": flag or None,
        }
        series_values[series_key].append((period, value, series_row))
        periods_by_series[series_key].add(period)
        if len(samples_by_element[element_code]) < 3:
            samples_by_element[element_code].append(normalized)
        if item in COMPARABLE_ITEMS and element_code not in comparable_samples[item]:
            comparable_samples[item][element_code] = {
                key: series_row[key]
                for key in (
                    "item_code",
                    "item",
                    "element_code",
                    "element",
                    "year",
                    "month",
                    "unit",
                    "value",
                    "flag",
                )
            }

    duplicate_count = sum(count - 1 for count in business_keys.values() if count > 1)
    if kenya_rows == 0:
        return {
            "source_row_count": total_rows,
            "country": AREA,
            "country_code_values": [],
            "observation_count": 0,
            "frequency_counts": {},
            "actual_columns": _column_profiles(fieldnames, value_type_counts),
            "null_counts_by_field": {field: null_counts[field] for field in fieldnames},
            "distinct_items": [],
            "measurement_elements": [],
            "measurement_elements_by_frequency": [],
            "units_by_element": [],
            "units_by_element_and_frequency": [],
            "years_by_frequency": {},
            "months_available": [],
            "flags": [],
            "business_key": list(BUSINESS_KEY),
            "quality_checks": {"empty_country_result": True},
            "representative_records_by_element": {},
            "comparable_commodity_samples": {},
        }

    gap_profiles = _profile_gaps(periods_by_series)
    outlier_profiles = _profile_outliers(series_values)
    missing_years = {
        frequency: _missing_years(years) for frequency, years in year_sets.items()
    }
    quality_checks = {
        "missing_price_values": null_counts["Value"],
        "negative_values": {
            "count": negative_value_count,
            "examples": negative_values[:10],
        },
        "zero_values": {
            "count": zero_value_count,
            "examples": zero_values[:10],
        },
        "duplicate_business_key_rows": duplicate_count,
        "duplicate_business_key_examples": duplicate_examples,
        "missing_years_between_global_bounds": missing_years,
        "discontinuous_series": gap_profiles,
        "unexpected_units": {
            "count": unexpected_unit_count,
            "examples": unexpected_units[:20],
        },
        "unknown_item_codes": {
            "count": unknown_item_count,
            "examples": unknown_items[:20],
        },
        "unknown_element_codes": {
            "count": unknown_element_count,
            "examples": unknown_elements[:20],
        },
        "invalid_country_codes": {
            "count": invalid_country_code_count,
            "examples": invalid_country_codes[:20],
        },
        "unrecognized_flags": dict(sorted(unknown_flags.items())),
        "missing_flags": flag_counts.get("(missing)", 0),
        "unexpected_month_values": dict(sorted(unexpected_months.items())),
        "invalid_year_values": {
            "count": invalid_year_count,
            "examples": invalid_years[:20],
        },
        "non_numeric_values": {
            "count": invalid_value_count,
            "examples": invalid_values[:20],
        },
        "outlier_values_tukey_1_5_iqr": outlier_profiles,
    }
    return {
        "source_row_count": total_rows,
        "country": AREA,
        "country_code_values": [
            {"area_code": area_code, "m49_code": m49_code}
            for area_code, m49_code in sorted(area_code_seen)
        ],
        "observation_count": kenya_rows,
        "frequency_counts": dict(sorted(frequency_counts.items())),
        "actual_columns": _column_profiles(fieldnames, value_type_counts),
        "null_counts_by_field": {field: null_counts[field] for field in fieldnames},
        "distinct_items": [
            {
                "item_code": code,
                "item": label,
                "observation_count": count,
            }
            for (code, label), count in sorted(
                item_counts.items(), key=lambda pair: pair[0][1].casefold()
            )
        ],
        "measurement_elements": [
            {
                "element_code": code,
                "element": label,
                "observation_count": count,
            }
            for (code, label), count in sorted(element_counts.items())
        ],
        "measurement_elements_by_frequency": [
            {
                "element_code": code,
                "element": label,
                "frequency": frequency,
                "observation_count": count,
            }
            for (code, label, frequency), count in sorted(
                element_frequency_counts.items()
            )
        ],
        "units_by_element": [
            {"element": element, "unit": unit or None, "observation_count": count}
            for (element, unit), count in sorted(unit_counts.items())
        ],
        "units_by_element_and_frequency": [
            {
                "element": element,
                "unit": unit or None,
                "frequency": frequency,
                "observation_count": count,
            }
            for (element, unit, frequency), count in sorted(
                unit_frequency_counts.items()
            )
        ],
        "years_by_frequency": {
            frequency: {
                "minimum": min(years),
                "maximum": max(years),
                "distinct_years": sorted(years),
                "missing_between_bounds": missing_years[frequency],
            }
            for frequency, years in sorted(year_sets.items())
        },
        "months_available": sorted(month_sets.get("monthly", set()), key=MONTHS.index),
        "flags": [
            {
                "flag": flag,
                "description": (
                    flag_lookup.get(flag, {}).get(" Description") if flag else None
                ),
                "observation_count": count,
                "recognized": flag in flag_lookup if flag else False,
            }
            for flag, count in sorted(flag_counts.items())
        ],
        "business_key": list(BUSINESS_KEY),
        "quality_checks": quality_checks,
        "representative_records_by_element": dict(sorted(samples_by_element.items())),
        "comparable_commodity_samples": {
            item: [rows[code] for code in sorted(rows)]
            for item, rows in sorted(comparable_samples.items())
        },
    }


def _validate_columns(
    fieldnames: list[str] | None, required: tuple[str, ...], source_name: str
) -> None:
    if fieldnames is None:
        raise FaostatProfileError(f"{source_name} is empty")
    missing = [field for field in required if field not in fieldnames]
    if missing:
        raise FaostatProfileError(
            f"{source_name} is missing required columns: {', '.join(missing)}"
        )


def _infer_type(value: str) -> str:
    if re.fullmatch(r"[+-]?\d+", value):
        return "integer"
    if _parse_number(value) is not None:
        return "number"
    return "string"


def _column_profiles(
    fields: list[str], type_counts: dict[str, Counter[str]]
) -> list[JsonObject]:
    return [
        {
            "name": field,
            "csv_source_type": "string",
            "observed_value_types": dict(sorted(type_counts[field].items())),
        }
        for field in fields
    ]


def _parse_year(value: str | None) -> int | None:
    if value is None or not re.fullmatch(r"\d{4}", value.strip()):
        return None
    year = int(value)
    return year if 1900 <= year <= 2100 else None


def _parse_number(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _append_limited(items: list[JsonObject], item: JsonObject, limit: int = 20) -> None:
    if len(items) < limit:
        items.append(item)


def _missing_years(years: set[int]) -> list[int]:
    if not years:
        return []
    return sorted(set(range(min(years), max(years) + 1)) - years)


def _profile_gaps(
    series_periods: dict[tuple[str, str, str, str], set[int]],
) -> JsonObject:
    summary: dict[str, dict[str, Any]] = {
        "annual": {"affected_series": 0, "missing_periods": 0, "examples": []},
        "monthly": {"affected_series": 0, "missing_periods": 0, "examples": []},
    }
    for (item_code, element_code, unit, frequency), periods in series_periods.items():
        if not periods:
            continue
        start, end = min(periods), max(periods)
        missing = sorted(set(range(start, end + 1)) - periods)
        if not missing:
            continue
        group = summary[frequency]
        group["affected_series"] += 1
        group["missing_periods"] += len(missing)
        if len(group["examples"]) < 20:
            group["examples"].append(
                {
                    "item_code": item_code,
                    "element_code": element_code,
                    "unit": unit or None,
                    "missing_period_count": len(missing),
                    "sample_missing_periods": [
                        _period_label(period, frequency) for period in missing[:12]
                    ],
                }
            )
    return summary


def _period_label(period: int, frequency: str) -> str:
    if frequency == "annual":
        return str(period)
    year, month = divmod(period, 12)
    return f"{year:04d}-{month + 1:02d}"


def _profile_outliers(
    series_values: dict[tuple[str, str, str, str], list[tuple[int, float, JsonObject]]],
) -> JsonObject:
    count = 0
    examples: list[JsonObject] = []
    eligible_series = 0
    by_element: dict[str, dict[str, int]] = defaultdict(
        lambda: {"eligible_series": 0, "outlier_count": 0}
    )
    for key, observations in series_values.items():
        values = [value for _, value, _ in observations]
        if len(values) < 8:
            continue
        eligible_series += 1
        by_element[key[1]]["eligible_series"] += 1
        sorted_values = sorted(values)
        lower_quartile, upper_quartile = statistics.quantiles(
            sorted_values, n=4, method="inclusive"
        )[0::2]
        spread = upper_quartile - lower_quartile
        lower_bound = lower_quartile - 1.5 * spread
        upper_bound = upper_quartile + 1.5 * spread
        for _, value, row in observations:
            if value < lower_bound or value > upper_bound:
                count += 1
                by_element[key[1]]["outlier_count"] += 1
                if len(examples) < 20:
                    examples.append(
                        {
                            **row,
                            "series_key": {
                                "item_code": key[0],
                                "element_code": key[1],
                                "unit": key[2] or None,
                                "frequency": key[3],
                            },
                            "lower_fence": lower_bound,
                            "upper_fence": upper_bound,
                        }
                    )
    return {
        "method": (
            "Tukey 1.5*IQR within item/element/unit/frequency series; not removed"
        ),
        "eligible_series": eligible_series,
        "outlier_count": count,
        "by_element_code": dict(sorted(by_element.items())),
        "examples": examples,
    }
