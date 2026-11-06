"""Aggregate profiles for World Bank indicator observations."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime

from kenya_food_intelligence.sources.world_bank import (
    API_ROOT,
    WorldBankClient,
)

type Profile = dict[str, object]
INDICATORS = ("FP.CPI.TOTL", "FP.CPI.TOTL.ZG")
COUNTRY_CODE = "KEN"
LICENSE_URL = "https://datacatalog.worldbank.org/public-licenses"


def profile_indicator(
    client: WorldBankClient,
    indicator_code: str,
    *,
    country_code: str = COUNTRY_CODE,
    per_page: int = 100,
) -> Profile:
    """Fetch an indicator and return aggregate statistics without raw records."""
    definition = client.get_indicator_definition(indicator_code)
    result = client.get_indicator_observations(
        country_code, indicator_code, per_page=per_page
    )
    observations = result.observations

    field_types: dict[str, set[str]] = defaultdict(set)
    year_counts: Counter[tuple[str, str]] = Counter()
    years: list[int] = []
    non_null_count = 0
    for observation in observations:
        _collect_observed_types(field_types, observation)
        year = str(observation["date"])
        years.append(int(year))
        indicator_id = observation["indicator"]["id"]
        year_counts[(indicator_id, year)] += 1
        if observation["value"] is not None:
            non_null_count += 1

    duplicate_keys = sorted(key for key, count in year_counts.items() if count > 1)
    duplicates = [
        {"indicator": indicator, "year": year, "count": year_counts[(indicator, year)]}
        for indicator, year in duplicate_keys
    ]
    source_url = client.data_url(country_code, indicator_code, per_page)
    attribution = _attribution(definition.source_name, definition.source_organization)

    return {
        "source_system": "World Bank Indicators API",
        "country_code": country_code,
        "indicator_code": indicator_code,
        "indicator_description": definition.description,
        "source_url": source_url,
        "indicator_metadata_url": client.definition_url(indicator_code),
        "source_id": definition.source_id,
        "source_name": definition.source_name,
        "source_note": definition.source_note,
        "source_organization": definition.source_organization,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "license_attribution": {
            "license": (
                "The World Bank states CC BY 4.0 is its default for datasets it "
                "produces and distributes as open data; this API response does not "
                "specify a license for this indicator. Verify the underlying "
                "source-specific terms before reuse or redistribution."
            ),
            "license_url": LICENSE_URL,
            "attribution": attribution,
        },
        "profiling_outcome": "success",
        "api_pagination": {
            "page_count": result.metadata.pages,
            "per_page": result.metadata.per_page,
            "api_total_observations": result.metadata.total,
        },
        "statistics": {
            "total_observations": len(observations),
            "non_null_observations": non_null_count,
            "missing_values": len(observations) - non_null_count,
            "minimum_year": min(years) if years else None,
            "maximum_year": max(years) if years else None,
            "duplicate_indicator_year_combinations": {
                "count": len(duplicates),
                "duplicates": duplicates,
            },
        },
        "observed_fields": {
            field: sorted(types) for field, types in sorted(field_types.items())
        },
    }


def failed_profile(
    indicator_code: str,
    error: Exception,
    *,
    country_code: str = COUNTRY_CODE,
    per_page: int = 100,
) -> Profile:
    """Build a report entry for an indicator that could not be profiled."""
    source_url = (
        f"{API_ROOT}/country/{country_code}/indicator/{indicator_code}"
        f"?format=json&per_page={per_page}&page=1"
    )
    return {
        "source_system": "World Bank Indicators API",
        "country_code": country_code,
        "indicator_code": indicator_code,
        "indicator_description": None,
        "source_url": source_url,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "license_attribution": {
            "license": (
                "The World Bank's default open-data license is CC BY 4.0 for "
                "datasets it produces; verify the indicator's underlying "
                "source-specific terms."
            ),
            "license_url": LICENSE_URL,
            "attribution": (
                "World Bank Open Data; attribution could not be confirmed from "
                "API metadata."
            ),
        },
        "profiling_outcome": "failed",
        "error": {"type": type(error).__name__, "message": str(error)},
    }


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def _collect_observed_types(
    field_types: dict[str, set[str]], value: object, prefix: str = ""
) -> None:
    if isinstance(value, dict):
        for field, nested_value in value.items():
            path = f"{prefix}.{field}" if prefix else field
            field_types[path].add(_json_type(nested_value))
            if isinstance(nested_value, dict):
                _collect_observed_types(field_types, nested_value, path)


def _attribution(source_name: str | None, source_organization: str | None) -> str:
    parts = ["World Bank"]
    if source_name:
        parts.append(source_name)
    if source_organization:
        parts.append(f"source organization: {source_organization}")
    return "; ".join(parts)
