"""Client for the World Bank Indicators API."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

type JsonObject = dict[str, object]

API_ROOT = "https://api.worldbank.org/v2"


class WorldBankError(Exception):
    """Base exception for World Bank API and response failures."""


class WorldBankRequestError(WorldBankError):
    """The API request failed after the configured retry policy."""


class WorldBankHTTPError(WorldBankError):
    """The API returned a non-retryable HTTP status."""


class WorldBankResponseError(WorldBankError):
    """The API response did not match the documented JSON structure."""


@dataclass(frozen=True)
class PageMetadata:
    page: int
    pages: int
    per_page: int
    total: int


@dataclass(frozen=True)
class IndicatorDefinition:
    indicator_id: str
    description: str
    source_id: str | None
    source_name: str | None
    source_note: str | None
    source_organization: str | None


@dataclass(frozen=True)
class IndicatorObservations:
    metadata: PageMetadata
    observations: list[JsonObject]


class WorldBankClient:
    """Fetch and validate paginated World Bank indicator responses."""

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if backoff_factor < 0:
            raise ValueError("backoff_factor cannot be negative")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor

    @staticmethod
    def data_url(country_code: str, indicator_code: str, per_page: int) -> str:
        query = urlencode({"format": "json", "per_page": per_page, "page": 1})
        return f"{API_ROOT}/country/{country_code}/indicator/{indicator_code}?{query}"

    @staticmethod
    def definition_url(indicator_code: str) -> str:
        return f"{API_ROOT}/indicator/{indicator_code}?format=json"

    def get_indicator_definition(self, indicator_code: str) -> IndicatorDefinition:
        payload = self._request_json(self.definition_url(indicator_code))
        if not isinstance(payload, list) or len(payload) != 2:
            raise WorldBankResponseError(
                f"Indicator definition for {indicator_code} must be a "
                "two-element JSON array"
            )
        metadata, definitions = payload
        if not isinstance(metadata, dict) or not isinstance(definitions, list):
            raise WorldBankResponseError(
                f"Indicator definition for {indicator_code} has invalid "
                "metadata or data"
            )
        if not definitions or not isinstance(definitions[0], dict):
            raise WorldBankResponseError(
                f"Indicator definition for {indicator_code} is empty or malformed"
            )
        definition = definitions[0]
        if definition.get("id") != indicator_code:
            raise WorldBankResponseError(
                "Indicator definition id does not match requested indicator "
                f"{indicator_code}"
            )
        description = definition.get("name")
        if not isinstance(description, str) or not description:
            raise WorldBankResponseError(
                f"Indicator definition for {indicator_code} has no description"
            )

        source = definition.get("source")
        if source is not None and not isinstance(source, dict):
            raise WorldBankResponseError(
                f"Indicator definition for {indicator_code} has an invalid source"
            )

        return IndicatorDefinition(
            indicator_id=indicator_code,
            description=description,
            source_id=_optional_string(source, "id"),
            source_name=_optional_string(source, "value"),
            source_note=_optional_string(definition, "sourceNote"),
            source_organization=_optional_string(definition, "sourceOrganization"),
        )

    def get_indicator_observations(
        self,
        country_code: str,
        indicator_code: str,
        *,
        per_page: int = 100,
    ) -> IndicatorObservations:
        if not 1 <= per_page <= 1000:
            raise ValueError("per_page must be between 1 and 1000")

        first_url = self.data_url(country_code, indicator_code, per_page)
        first_page = self._parse_observation_page(
            self._request_json(first_url),
            expected_page=1,
            indicator_code=indicator_code,
        )
        observations = list(first_page.observations)
        reference_metadata = first_page.metadata

        for page_number in range(2, reference_metadata.pages + 1):
            query = urlencode(
                {"format": "json", "per_page": per_page, "page": page_number}
            )
            page_url = (
                f"{API_ROOT}/country/{country_code}/indicator/{indicator_code}?{query}"
            )
            page = self._parse_observation_page(
                self._request_json(page_url),
                expected_page=page_number,
                indicator_code=indicator_code,
            )
            if (
                page.metadata.pages != reference_metadata.pages
                or page.metadata.total != reference_metadata.total
                or page.metadata.per_page != reference_metadata.per_page
            ):
                raise WorldBankResponseError(
                    f"Pagination metadata changed while fetching {indicator_code}"
                )
            observations.extend(page.observations)

        if len(observations) != reference_metadata.total:
            raise WorldBankResponseError(
                f"Expected {reference_metadata.total} observations for "
                f"{indicator_code}, "
                f"received {len(observations)}"
            )
        return IndicatorObservations(reference_metadata, observations)

    def _request_json(self, url: str) -> object:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            request = Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "kenya-food-price-intelligence-source-profiler/0.1",
                },
            )
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read())
            except HTTPError as error:
                if error.code not in {408, 429} and error.code < 500:
                    raise WorldBankHTTPError(
                        f"World Bank API returned HTTP {error.code} for {url}"
                    ) from error
                last_error = error
            except (URLError, TimeoutError, OSError) as error:
                last_error = error
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise WorldBankResponseError(
                    f"World Bank API returned invalid JSON for {url}"
                ) from error

            if attempt < self.max_retries:
                time.sleep(self.backoff_factor * (2**attempt))

        if isinstance(last_error, HTTPError):
            raise WorldBankRequestError(
                f"World Bank API request failed after {self.max_retries + 1} attempts "
                f"with HTTP {last_error.code}: {url}"
            ) from last_error
        raise WorldBankRequestError(
            f"World Bank API request failed after {self.max_retries + 1} "
            f"attempts: {url}"
        ) from last_error

    @staticmethod
    def _parse_observation_page(
        payload: object, *, expected_page: int, indicator_code: str
    ) -> IndicatorObservations:
        if not isinstance(payload, list) or len(payload) != 2:
            raise WorldBankResponseError(
                f"Response for {indicator_code} page {expected_page} "
                "must be a two-element JSON array"
            )
        raw_metadata, raw_observations = payload
        if not isinstance(raw_metadata, dict):
            raise WorldBankResponseError(
                f"Response metadata for {indicator_code} page {expected_page} "
                "must be an object"
            )
        metadata = _parse_page_metadata(raw_metadata, indicator_code)
        if metadata.page != expected_page:
            raise WorldBankResponseError(
                f"Requested page {expected_page} for {indicator_code}, "
                f"received page {metadata.page}"
            )

        if raw_observations is None:
            if metadata.total != 0:
                raise WorldBankResponseError(
                    f"Response for {indicator_code} reports observations "
                    "but has null data"
                )
            observations: list[object] = []
        elif isinstance(raw_observations, list):
            observations = raw_observations
        else:
            raise WorldBankResponseError(
                f"Observation data for {indicator_code} page {expected_page} "
                "must be an array or null"
            )

        validated_observations = [
            _validate_observation(item, indicator_code, expected_page)
            for item in observations
        ]
        if metadata.total == 0 and validated_observations:
            raise WorldBankResponseError(
                f"Response for {indicator_code} has observations but reports total zero"
            )
        return IndicatorObservations(metadata, validated_observations)


def _parse_page_metadata(
    metadata: dict[str, object], indicator_code: str
) -> PageMetadata:
    values: dict[str, int] = {}
    for field in ("page", "pages", "per_page", "total"):
        value = metadata.get(field)
        if isinstance(value, bool):
            raise WorldBankResponseError(
                f"Pagination field {field} for {indicator_code} must be an integer"
            )
        if isinstance(value, int):
            parsed = value
        elif isinstance(value, str) and value.isdigit():
            parsed = int(value)
        else:
            raise WorldBankResponseError(
                f"Pagination field {field} for {indicator_code} must be an integer"
            )
        values[field] = parsed

    if (
        values["page"] < 1
        or values["pages"] < 0
        or values["per_page"] < 1
        or values["total"] < 0
        or (values["total"] > 0 and values["pages"] < 1)
    ):
        raise WorldBankResponseError(
            f"Pagination fields for {indicator_code} are outside valid ranges"
        )
    return PageMetadata(**values)


def _validate_observation(
    observation: object, indicator_code: str, page: int
) -> JsonObject:
    if not isinstance(observation, dict):
        raise WorldBankResponseError(
            f"Observation for {indicator_code} page {page} must be an object"
        )
    required_fields = {
        "indicator",
        "country",
        "countryiso3code",
        "date",
        "value",
        "unit",
        "obs_status",
        "decimal",
    }
    missing_fields = required_fields - observation.keys()
    if missing_fields:
        raise WorldBankResponseError(
            f"Observation for {indicator_code} page {page} is missing fields: "
            f"{', '.join(sorted(missing_fields))}"
        )

    for field in ("indicator", "country"):
        value = observation[field]
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("id"), str)
            or not isinstance(value.get("value"), str)
        ):
            raise WorldBankResponseError(
                f"Observation field {field} for {indicator_code} page {page} "
                "must contain string id and value fields"
            )
    if observation["indicator"]["id"] != indicator_code:
        raise WorldBankResponseError(
            f"Observation indicator id does not match {indicator_code}"
        )
    if not isinstance(observation["countryiso3code"], str):
        raise WorldBankResponseError(
            f"Observation countryiso3code for {indicator_code} must be a string"
        )
    date = observation["date"]
    if not isinstance(date, str) or len(date) != 4 or not date.isdigit():
        raise WorldBankResponseError(
            f"Observation date for {indicator_code} must be a four-digit year"
        )

    value = observation["value"]
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int | float)
    ):
        raise WorldBankResponseError(
            f"Observation value for {indicator_code} must be numeric or null"
        )
    for field in ("unit", "obs_status"):
        if observation[field] is not None and not isinstance(observation[field], str):
            raise WorldBankResponseError(
                f"Observation field {field} for {indicator_code} must be "
                "a string or null"
            )
    decimal = observation["decimal"]
    if decimal is not None and (
        isinstance(decimal, bool) or not isinstance(decimal, int)
    ):
        raise WorldBankResponseError(
            f"Observation decimal for {indicator_code} must be an integer or null"
        )
    return observation


def _optional_string(value: object, field: str) -> str | None:
    if not isinstance(value, dict):
        return None
    item = value.get(field)
    return item if isinstance(item, str) else None
