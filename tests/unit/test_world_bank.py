from __future__ import annotations

import io
import json
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest

from kenya_food_intelligence.profiling.world_bank import profile_indicator
from kenya_food_intelligence.sources.world_bank import (
    WorldBankClient,
    WorldBankHTTPError,
    WorldBankRequestError,
    WorldBankResponseError,
)

INDICATOR = "FP.CPI.TOTL"


class JsonResponse(io.BytesIO):
    def __init__(self, value: object) -> None:
        super().__init__(json.dumps(value).encode())
        self.status = 200
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"

    def geturl(self) -> str:
        return "https://api.worldbank.org/v2/test"


def observation(
    year: str,
    value: int | float | None,
    *,
    indicator_id: str = INDICATOR,
) -> dict[str, object]:
    return {
        "indicator": {"id": indicator_id, "value": "Consumer price index"},
        "country": {"id": "KE", "value": "Kenya"},
        "countryiso3code": "KEN",
        "date": year,
        "value": value,
        "unit": "",
        "obs_status": "",
        "decimal": 1,
    }


def page(
    observations: list[dict[str, object]] | None,
    *,
    page_number: int = 1,
    pages: int = 1,
    total: int = 1,
    per_page: int = 100,
) -> list[object]:
    return [
        {
            "page": page_number,
            "pages": pages,
            "per_page": per_page,
            "total": total,
        },
        observations,
    ]


def definition(code: str = INDICATOR) -> list[object]:
    return [
        {"page": 1, "pages": 1, "per_page": "50", "total": 1},
        [
            {
                "id": code,
                "name": "Consumer price index (2010 = 100)",
                "source": {"id": "2", "value": "World Development Indicators"},
                "sourceNote": "Example source note",
                "sourceOrganization": "International Monetary Fund (IMF)",
            }
        ],
    ]


def response(value: object) -> JsonResponse:
    return JsonResponse(value)


def test_fetches_all_pages_and_checks_pagination_metadata() -> None:
    client = WorldBankClient()
    first = page(
        [observation("2024", 100), observation("2023", 95)],
        pages=2,
        total=3,
        per_page=2,
    )
    second = page(
        [observation("2022", 90)],
        page_number=2,
        pages=2,
        total=3,
        per_page=2,
    )
    with patch(
        "kenya_food_intelligence.sources.http.urlopen",
        side_effect=[response(first), response(second)],
    ) as open_url:
        result = client.get_indicator_observations("KEN", INDICATOR, per_page=2)

    assert [item["date"] for item in result.observations] == ["2024", "2023", "2022"]
    assert result.metadata.pages == 2
    requested_pages = [
        parse_qs(urlparse(call.args[0].full_url).query)["page"][0]
        for call in open_url.call_args_list
    ]
    assert requested_pages == ["1", "2"]
    assert all(call.kwargs["timeout"] == 20.0 for call in open_url.call_args_list)


def test_empty_response_with_null_data_is_valid() -> None:
    client = WorldBankClient()
    with patch(
        "kenya_food_intelligence.sources.http.urlopen",
        return_value=response(page(None, pages=0, total=0)),
    ):
        result = client.get_indicator_observations("KEN", INDICATOR)

    assert result.observations == []
    assert result.metadata.total == 0


def test_null_observations_are_counted_as_missing_not_api_errors() -> None:
    client = WorldBankClient()
    with patch(
        "kenya_food_intelligence.sources.http.urlopen",
        side_effect=[
            response(definition()),
            response(
                page([observation("2024", None), observation("2023", 99.5)], total=2)
            ),
        ],
    ):
        profile = profile_indicator(client, INDICATOR)

    assert profile["profiling_outcome"] == "success"
    assert profile["statistics"] == {
        "total_observations": 2,
        "non_null_observations": 1,
        "missing_values": 1,
        "minimum_year": 2023,
        "maximum_year": 2024,
        "duplicate_indicator_year_combinations": {
            "count": 0,
            "duplicates": [],
        },
    }
    fields = profile["observed_fields"]
    assert fields["value"] == ["null", "number"]
    assert fields["indicator"] == ["object"]
    assert fields["indicator.id"] == ["string"]
    assert fields["country.value"] == ["string"]
    assert profile["indicator_description"] == "Consumer price index (2010 = 100)"
    assert (
        "International Monetary Fund" in profile["license_attribution"]["attribution"]
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"error": "not an array"},
        [{"page": 1}, []],
        [{"page": 1, "pages": 1, "total": 1, "per_page": 10}, None],
        [
            {"page": 1, "pages": 1, "total": 1, "per_page": 10},
            [{"date": "2024"}],
        ],
        page([observation("2024", "not-a-number")]),
        page([observation("24", 1)]),
    ],
)
def test_malformed_observation_responses_raise_meaningful_errors(
    payload: object,
) -> None:
    client = WorldBankClient(max_retries=0)
    with patch(
        "kenya_food_intelligence.sources.http.urlopen",
        return_value=response(payload),
    ):
        with pytest.raises(WorldBankResponseError):
            client.get_indicator_observations("KEN", INDICATOR)


def test_changed_pagination_metadata_is_rejected() -> None:
    client = WorldBankClient()
    with patch(
        "kenya_food_intelligence.sources.http.urlopen",
        side_effect=[
            response(
                page(
                    [observation("2024", 100)],
                    pages=2,
                    total=2,
                    per_page=1,
                )
            ),
            response(
                page(
                    [observation("2023", 95)],
                    page_number=2,
                    pages=3,
                    total=2,
                    per_page=1,
                )
            ),
        ],
    ):
        with pytest.raises(WorldBankResponseError, match="Pagination metadata changed"):
            client.get_indicator_observations("KEN", INDICATOR, per_page=1)


def test_transient_http_error_retries_with_exponential_backoff() -> None:
    client = WorldBankClient(max_retries=2, backoff_factor=0.25)
    service_unavailable = HTTPError(
        "https://api.worldbank.org/test", 503, "Unavailable", {}, io.BytesIO()
    )
    with (
        patch(
            "kenya_food_intelligence.sources.http.urlopen",
            side_effect=[service_unavailable, response(page([], total=0, pages=0))],
        ) as open_url,
        patch("kenya_food_intelligence.sources.http.time.sleep") as sleep,
    ):
        result = client.get_indicator_observations("KEN", INDICATOR)

    assert result.observations == []
    assert open_url.call_count == 2
    sleep.assert_called_once_with(0.25)


def test_non_retryable_http_error_fails_without_retry() -> None:
    client = WorldBankClient(max_retries=3)
    not_found = HTTPError(
        "https://api.worldbank.org/test", 404, "Not Found", {}, io.BytesIO()
    )
    with (
        patch(
            "kenya_food_intelligence.sources.http.urlopen",
            side_effect=not_found,
        ) as open_url,
        patch("kenya_food_intelligence.sources.http.time.sleep") as sleep,
    ):
        with pytest.raises(WorldBankHTTPError, match="HTTP 404"):
            client.get_indicator_observations("KEN", INDICATOR)

    assert open_url.call_count == 1
    sleep.assert_not_called()


def test_exhausted_transient_http_retries_raise_request_error() -> None:
    client = WorldBankClient(max_retries=2, backoff_factor=0.1)
    unavailable = HTTPError(
        "https://api.worldbank.org/test", 503, "Unavailable", {}, io.BytesIO()
    )
    with (
        patch(
            "kenya_food_intelligence.sources.http.urlopen",
            side_effect=[unavailable, unavailable, unavailable],
        ) as open_url,
        patch("kenya_food_intelligence.sources.http.time.sleep") as sleep,
    ):
        with pytest.raises(WorldBankRequestError, match="3 attempts"):
            client.get_indicator_observations("KEN", INDICATOR)

    assert open_url.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [0.1, 0.2]


def test_network_failures_are_retried_and_include_configured_timeout() -> None:
    from urllib.error import URLError

    client = WorldBankClient(timeout=4.5, max_retries=1, backoff_factor=0)
    with (
        patch(
            "kenya_food_intelligence.sources.http.urlopen",
            side_effect=[
                URLError("temporary network failure"),
                response(page([], total=0, pages=0)),
            ],
        ) as open_url,
        patch("kenya_food_intelligence.sources.http.time.sleep") as sleep,
    ):
        result = client.get_indicator_observations("KEN", INDICATOR)

    assert result.observations == []
    assert [call.kwargs["timeout"] for call in open_url.call_args_list] == [4.5, 4.5]
    sleep.assert_called_once_with(0)


def test_client_rejects_invalid_retry_and_page_configuration() -> None:
    with pytest.raises(ValueError, match="timeout"):
        WorldBankClient(timeout=0)
    with pytest.raises(ValueError, match="max_retries"):
        WorldBankClient(max_retries=-1)
    with pytest.raises(ValueError, match="backoff_factor"):
        WorldBankClient(backoff_factor=-1)
    with pytest.raises(ValueError, match="per_page"):
        WorldBankClient().get_indicator_observations("KEN", INDICATOR, per_page=1001)


def test_duplicate_indicator_year_combinations_are_reported() -> None:
    client = WorldBankClient()
    with patch(
        "kenya_food_intelligence.sources.http.urlopen",
        side_effect=[
            response(definition()),
            response(
                page(
                    [
                        observation("2024", 100),
                        observation("2024", 101),
                        observation("2023", 95),
                    ],
                    total=3,
                )
            ),
        ],
    ):
        profile = profile_indicator(client, INDICATOR)

    duplicates = profile["statistics"]["duplicate_indicator_year_combinations"]
    assert duplicates == {
        "count": 1,
        "duplicates": [{"indicator": INDICATOR, "year": "2024", "count": 2}],
    }
