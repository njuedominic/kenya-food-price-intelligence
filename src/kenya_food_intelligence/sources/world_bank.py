"""Client for the World Bank Indicators API."""

from __future__ import annotations

import json
import logging
import ssl
from dataclasses import dataclass
from urllib.parse import urlencode

from kenya_food_intelligence.sources.http import (
    HTTPClient,
    HTTPRequestError,
    HTTPResponse,
    HTTPResponseTooLarge,
    HTTPStatusError,
)

type JsonObject = dict[str, object]

API_ROOT = "https://api.worldbank.org/v2"
DEFAULT_MAX_PAGES = 100
DEFAULT_MAX_PAGE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 25 * 1024 * 1024
logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class RawIndicatorPage:
    metadata: PageMetadata
    observations: list[JsonObject]
    request_url: str
    content_type: str
    body: bytes


@dataclass(frozen=True)
class IndicatorObservationPages:
    metadata: PageMetadata
    pages: tuple[RawIndicatorPage, ...]

    @property
    def observations(self) -> list[JsonObject]:
        return [observation for page in self.pages for observation in page.observations]


class WorldBankClient:
    """Fetch and validate paginated World Bank indicator responses."""

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_page_bytes: int = DEFAULT_MAX_PAGE_BYTES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if backoff_factor < 0:
            raise ValueError("backoff_factor cannot be negative")
        for name, value in (
            ("max_pages", max_pages),
            ("max_page_bytes", max_page_bytes),
            ("max_total_bytes", max_total_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if max_total_bytes < max_page_bytes:
            raise ValueError("max_total_bytes must be at least max_page_bytes")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.max_pages = max_pages
        self.max_page_bytes = max_page_bytes
        self.max_total_bytes = max_total_bytes
        self.http = HTTPClient(
            timeout=timeout,
            max_retries=max_retries,
            backoff_factor=backoff_factor,
            user_agent="kenya-food-price-intelligence-source-profiler/0.1",
            ssl_context=ssl.create_default_context(),
        )

    @staticmethod
    def data_url(
        country_code: str, indicator_code: str, per_page: int, page: int = 1
    ) -> str:
        query = urlencode({"format": "json", "per_page": per_page, "page": page})
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
        result = self.get_indicator_observation_pages(
            country_code, indicator_code, per_page=per_page
        )
        return IndicatorObservations(result.metadata, result.observations)

    def get_indicator_observation_pages(
        self,
        country_code: str,
        indicator_code: str,
        *,
        per_page: int = 100,
    ) -> IndicatorObservationPages:
        """Fetch and validate every page while retaining each original body."""
        if not 1 <= per_page <= 1000:
            raise ValueError("per_page must be between 1 and 1000")

        first_url = self.data_url(country_code, indicator_code, per_page, page=1)
        first_page = self._request_observation_page(
            first_url,
            expected_page=1,
            per_page=per_page,
            country_code=country_code,
            indicator_code=indicator_code,
            max_bytes=min(self.max_page_bytes, self.max_total_bytes),
            size_limit_name="page response",
        )
        reference_metadata = first_page.metadata
        if reference_metadata.pages > self.max_pages:
            raise WorldBankResponseError(
                f"World Bank API reported {reference_metadata.pages} pages for "
                f"{indicator_code}, exceeding the configured maximum of "
                f"{self.max_pages}"
            )
        pages = [first_page]
        total_bytes = len(first_page.body)
        logger.info(
            "Retrieved World Bank indicator page",
            extra={
                "indicator_code": indicator_code,
                "page": first_page.metadata.page,
                "pages": first_page.metadata.pages,
                "observation_count": len(first_page.observations),
            },
        )

        for page_number in range(2, reference_metadata.pages + 1):
            remaining_bytes = self.max_total_bytes - total_bytes
            if remaining_bytes <= 0:
                raise WorldBankResponseError(
                    f"World Bank response pages for {indicator_code} exceed the "
                    f"configured total limit of {self.max_total_bytes} bytes"
                )
            page_url = self.data_url(
                country_code, indicator_code, per_page, page=page_number
            )
            page_limit = min(self.max_page_bytes, remaining_bytes)
            page = self._request_observation_page(
                page_url,
                expected_page=page_number,
                per_page=per_page,
                country_code=country_code,
                indicator_code=indicator_code,
                max_bytes=page_limit,
                size_limit_name=(
                    "total response"
                    if remaining_bytes < self.max_page_bytes
                    else "page response"
                ),
            )
            if (
                page.metadata.pages != reference_metadata.pages
                or page.metadata.total != reference_metadata.total
                or page.metadata.per_page != reference_metadata.per_page
            ):
                raise WorldBankResponseError(
                    f"Pagination metadata changed while fetching {indicator_code}"
                )
            if len(page.body) > remaining_bytes:
                raise WorldBankResponseError(
                    f"World Bank response pages for {indicator_code} exceed the "
                    f"configured total limit of {self.max_total_bytes} bytes"
                )
            total_bytes += len(page.body)
            pages.append(page)
            logger.info(
                "Retrieved World Bank indicator page",
                extra={
                    "indicator_code": indicator_code,
                    "page": page.metadata.page,
                    "pages": page.metadata.pages,
                    "observation_count": len(page.observations),
                },
            )

        observation_count = sum(len(page.observations) for page in pages)
        if observation_count != reference_metadata.total:
            raise WorldBankResponseError(
                f"Expected {reference_metadata.total} observations for "
                f"{indicator_code}, "
                f"received {observation_count}"
            )
        return IndicatorObservationPages(reference_metadata, tuple(pages))

    def _request_observation_page(
        self,
        url: str,
        *,
        expected_page: int,
        per_page: int,
        country_code: str,
        indicator_code: str,
        max_bytes: int,
        size_limit_name: str,
    ) -> RawIndicatorPage:
        response = self._request_response(
            url,
            max_bytes=max_bytes,
            size_limit_name=size_limit_name,
        )
        if response.status != 200:
            raise WorldBankHTTPError(
                f"World Bank API returned HTTP {response.status} for {url}"
            )
        content_type = (response.content_type or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise WorldBankResponseError(
                f"World Bank API returned unexpected content type "
                f"{response.content_type!r} for {url}"
            )
        try:
            payload = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise WorldBankResponseError(
                f"World Bank API returned invalid JSON for {url}"
            ) from error
        parsed = self._parse_observation_page(
            payload,
            expected_page=expected_page,
            per_page=per_page,
            country_code=country_code,
            indicator_code=indicator_code,
        )
        return RawIndicatorPage(
            metadata=parsed.metadata,
            observations=parsed.observations,
            request_url=url,
            content_type=response.content_type or "",
            body=response.body,
        )

    def _request_json(self, url: str) -> object:
        response = self._request_response(url)
        try:
            return json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise WorldBankResponseError(
                f"World Bank API returned invalid JSON for {url}"
            ) from error

    def _request_response(
        self,
        url: str,
        *,
        max_bytes: int | None = None,
        size_limit_name: str = "response",
    ) -> HTTPResponse:
        try:
            return self.http.get(
                url,
                headers={"Accept": "application/json"},
                max_bytes=max_bytes,
            )
        except HTTPResponseTooLarge as error:
            raise WorldBankResponseError(
                f"World Bank API {size_limit_name} for {url} exceeds the "
                f"configured limit of {max_bytes} bytes"
            ) from error
        except HTTPStatusError as error:
            raise WorldBankHTTPError(
                f"World Bank API returned HTTP {error.status} for {url}"
            ) from error
        except HTTPRequestError as error:
            if error.status is not None:
                raise WorldBankRequestError(
                    f"World Bank API request failed after {error.attempts} attempts "
                    f"with HTTP {error.status}: {url}"
                ) from error
            raise WorldBankRequestError(
                f"World Bank API request failed after {error.attempts} attempts: {url}"
            ) from error

    @staticmethod
    def _parse_observation_page(
        payload: object,
        *,
        expected_page: int,
        per_page: int,
        country_code: str,
        indicator_code: str,
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
        if metadata.per_page != per_page:
            raise WorldBankResponseError(
                f"Response page size for {indicator_code} does not match "
                f"requested page size {per_page}"
            )
        if metadata.total == 0 and metadata.pages not in {0, 1}:
            raise WorldBankResponseError(
                f"Empty response for {indicator_code} has inconsistent page count"
            )
        if (
            metadata.total > 0
            and metadata.pages
            != (metadata.total + metadata.per_page - 1) // metadata.per_page
        ):
            raise WorldBankResponseError(
                f"Page count for {indicator_code} contradicts the reported total"
            )
        if metadata.total > 0 and metadata.page > metadata.pages:
            raise WorldBankResponseError(
                f"Page {metadata.page} is outside the reported page count for "
                f"{indicator_code}"
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
            _validate_observation(item, country_code, indicator_code, expected_page)
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
        if field == "pages":
            if not isinstance(value, int) or isinstance(value, bool):
                raise WorldBankResponseError(
                    f"Pagination field pages for {indicator_code} must be an integer"
                )
            parsed = value
        elif isinstance(value, int):
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
        or values["pages"] < 1
        or values["per_page"] < 1
        or values["total"] < 0
    ):
        raise WorldBankResponseError(
            f"Pagination fields for {indicator_code} are outside valid ranges"
        )
    return PageMetadata(**values)


def _validate_observation(
    observation: object, country_code: str, indicator_code: str, page: int
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
    if observation["countryiso3code"] != country_code:
        raise WorldBankResponseError(
            f"Observation country code does not match requested country {country_code}"
        )
    expected_country = {"KEN": ("KE", "Kenya")}.get(country_code)
    if expected_country is not None and (
        observation["country"]["id"] != expected_country[0]
        or observation["country"]["value"] != expected_country[1]
    ):
        raise WorldBankResponseError(
            f"Observation country identity does not match requested country "
            f"{country_code}"
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
