from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, datetime
from email.message import Message
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from kenya_food_intelligence.profiling.faostat import (
    DATA_CSV_MEMBER,
    REQUIRED_FIELDS,
    FaostatProfileError,
    profile_csv,
    profile_dataset,
)
from kenya_food_intelligence.sources.faostat import (
    DATASET_ID,
    DOWNLOAD_URL,
    DownloadedDataset,
    FaostatArchiveError,
    FAOSTATClient,
    FaostatHTTPError,
    discover_dataset,
    validate_archive,
)
from kenya_food_intelligence.sources.http import (
    HTTPClient,
)

LOOKUPS = {
    "area": {
        "114": {
            "Area Code": "114",
            "M49 Code": "'404",
            "Area": "Kenya",
        },
        "1": {"Area Code": "1", "M49 Code": "'008", "Area": "Albania"},
    },
    "item": {
        "56": {"Item Code": "56", "Item": "Maize (corn)"},
        "388": {"Item Code": "388", "Item": "Tomatoes"},
    },
    "element": {
        "5530": {"Element Code": "5530", "Element": "Producer Price (LCU/tonne)"},
        "5531": {"Element Code": "5531", "Element": "Producer Price (SLC/tonne)"},
        "5532": {"Element Code": "5532", "Element": "Producer Price (USD/tonne)"},
        "5539": {
            "Element Code": "5539",
            "Element": "Producer Price Index (2014-2016 = 100)",
        },
    },
    "flag": {
        "A": {"Flag": "A", " Description": "Official value"},
        "E": {"Flag": "E", " Description": "Estimated value"},
        "X": {"Flag": "X", " Description": "Value from external organization"},
    },
}


def _row(
    *,
    area_code: str = "114",
    area_m49: str = "'404",
    area: str = "Kenya",
    item_code: str = "56",
    item: str = "Maize (corn)",
    element_code: str = "5530",
    element: str = "Producer Price (LCU/tonne)",
    year: str = "1991",
    month_code: str = "7021",
    month: str = "Annual value",
    unit: str = "LCU",
    value: str = "100.25",
    flag: str = "A",
) -> dict[str, str]:
    return {
        "Area Code": area_code,
        "Area Code (M49)": area_m49,
        "Area": area,
        "Item Code": item_code,
        "Item Code (CPC)": "'011",
        "Item": item,
        "Element Code": element_code,
        "Element": element,
        "Year Code": year,
        "Year": year,
        "Months Code": month_code,
        "Months": month,
        "Unit": unit,
        "Value": value,
        "Flag": flag,
    }


def _csv_bytes(rows: list[dict[str, str]]) -> bytes:
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=REQUIRED_FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def _archive_bytes(
    rows: list[dict[str, str]], *, unsafe_member: str | None = None
) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(DATA_CSV_MEMBER, _csv_bytes(rows))
        archive.writestr(
            "Prices_E_AreaCodes.csv",
            "Area Code,M49 Code,Area\n114,'404,Kenya\n1,'008,Albania\n",
        )
        archive.writestr(
            "Prices_E_ItemCodes.csv",
            "Item Code,CPC Code,Item\n56,'011,Maize (corn)\n388,'01332,Tomatoes\n",
        )
        archive.writestr(
            "Prices_E_Elements.csv",
            "Element Code,Element\n"
            "5530,Producer Price (LCU/tonne)\n"
            "5531,Producer Price (SLC/tonne)\n"
            "5532,Producer Price (USD/tonne)\n"
            "5539,Producer Price Index (2014-2016 = 100)\n",
        )
        archive.writestr(
            "Prices_E_Flags.csv",
            "Flag, Description\n"
            "A,Official value\n"
            "E,Estimated value\n"
            "X,Value from external organization\n",
        )
        if unsafe_member is not None:
            archive.writestr(unsafe_member, "not extracted")
    return output.getvalue()


def _downloaded_dataset(path: Path, body: bytes) -> DownloadedDataset:
    path.write_bytes(body)
    source = discover_dataset()
    return DownloadedDataset(
        source=source,
        path=path,
        retrieved_at_utc=datetime.now(UTC).isoformat(),
        http_status=200,
        content_type="application/zip",
        file_size_bytes=len(body),
        checksum_sha256=hashlib.sha256(body).hexdigest(),
        last_modified="Fri, 09 Oct 2026 10:00:00 GMT",
        etag='"test"',
    )


class FakeResponse(BytesIO):
    def __init__(self, body: bytes, url: str) -> None:
        super().__init__(body)
        self.status = 200
        self.headers = Message()
        self.headers["Content-Type"] = "application/zip"
        self.headers["Content-Length"] = str(len(body))
        self.headers["Last-Modified"] = "Fri, 09 Oct 2026 10:00:00 GMT"
        self.headers["ETag"] = '"test"'
        self.url = url

    def geturl(self) -> str:
        return self.url

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def test_discovery_uses_catalogued_dataset_and_official_download() -> None:
    source = discover_dataset()

    assert source.dataset_id == DATASET_ID
    assert source.dataset_url.endswith(DATASET_ID)
    assert source.download_url == DOWNLOAD_URL
    assert source.data_member == DATA_CSV_MEMBER


def test_download_streams_and_records_checksum_and_metadata(tmp_path: Path) -> None:
    body = _archive_bytes([_row()])
    client = FAOSTATClient(max_retries=0)

    with patch(
        "kenya_food_intelligence.sources.http.urlopen",
        return_value=FakeResponse(body, DOWNLOAD_URL),
    ):
        downloaded = client.download_dataset(tmp_path)

    assert downloaded.path.read_bytes() == body
    assert downloaded.http_status == 200
    assert downloaded.content_type == "application/zip"
    assert downloaded.file_size_bytes == len(body)
    assert downloaded.checksum_sha256 == hashlib.sha256(body).hexdigest()
    assert downloaded.last_modified == "Fri, 09 Oct 2026 10:00:00 GMT"
    assert downloaded.etag == '"test"'
    assert validate_archive(downloaded.path) == [
        DATA_CSV_MEMBER,
        "Prices_E_AreaCodes.csv",
        "Prices_E_ItemCodes.csv",
        "Prices_E_Elements.csv",
        "Prices_E_Flags.csv",
    ]


def test_download_retries_transient_http_error(tmp_path: Path) -> None:
    body = _archive_bytes([_row()])
    failure = HTTPError(DOWNLOAD_URL, 503, "Unavailable", {}, BytesIO())
    client = FAOSTATClient(max_retries=1, backoff_factor=0)

    with (
        patch(
            "kenya_food_intelligence.sources.http.urlopen",
            side_effect=[failure, FakeResponse(body, DOWNLOAD_URL)],
        ) as open_url,
        patch("kenya_food_intelligence.sources.http.time.sleep"),
    ):
        downloaded = client.download_dataset(tmp_path)

    assert downloaded.file_size_bytes == len(body)
    assert open_url.call_count == 2


def test_download_timeout_is_reported(tmp_path: Path) -> None:
    client = FAOSTATClient(max_retries=0)
    with patch(
        "kenya_food_intelligence.sources.http.urlopen",
        side_effect=TimeoutError("timed out"),
    ):
        with pytest.raises(FaostatHTTPError, match="GET failed"):
            client.download_dataset(tmp_path)


def test_download_http_failure_is_reported_without_retry(tmp_path: Path) -> None:
    client = FAOSTATClient(max_retries=0)
    failure = HTTPError(DOWNLOAD_URL, 403, "Forbidden", {}, BytesIO())
    with patch(
        "kenya_food_intelligence.sources.http.urlopen",
        side_effect=failure,
    ):
        with pytest.raises(FaostatHTTPError, match="HTTP 403"):
            client.download_dataset(tmp_path)


def test_invalid_zip_and_missing_member_are_rejected(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.zip"
    invalid.write_bytes(b"not a zip")
    with pytest.raises(FaostatArchiveError, match="ZIP signature"):
        validate_archive(invalid)

    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("not-the-data.csv", "col\n1\n")
    missing = tmp_path / "missing.zip"
    missing.write_bytes(output.getvalue())
    with pytest.raises(FaostatArchiveError, match="Expected CSV member"):
        validate_archive(missing)


def test_unsafe_zip_member_is_rejected_without_extraction(tmp_path: Path) -> None:
    path = tmp_path / "unsafe.zip"
    path.write_bytes(_archive_bytes([_row()], unsafe_member="../escape.txt"))

    with pytest.raises(FaostatArchiveError, match="Unsafe ZIP member"):
        validate_archive(path)
    assert not (tmp_path.parent / "escape.txt").exists()


def test_archive_crc_corruption_is_detected(tmp_path: Path) -> None:
    damaged = bytearray(_archive_bytes([_row()]))
    with ZipFile(BytesIO(damaged)) as archive:
        info = archive.getinfo(DATA_CSV_MEMBER)
    filename_length = int.from_bytes(
        damaged[info.header_offset + 26 : info.header_offset + 28], "little"
    )
    extra_length = int.from_bytes(
        damaged[info.header_offset + 28 : info.header_offset + 30], "little"
    )
    data_offset = info.header_offset + 30 + filename_length + extra_length
    damaged[data_offset] ^= 0xFF
    path = tmp_path / "corrupt.zip"
    path.write_bytes(damaged)

    with pytest.raises(FaostatArchiveError):
        validate_archive(path)


def test_profile_filters_kenya_and_reports_schema_quality_and_periods(
    tmp_path: Path,
) -> None:
    rows = [
        _row(year="1991", value="100.25", flag="X"),
        _row(year="1992", value="0"),
        _row(
            element_code="5531",
            element="Producer Price (SLC/tonne)",
            year="1991",
            unit="SLC",
            value="110",
        ),
        _row(
            element_code="5532",
            element="Producer Price (USD/tonne)",
            year="1991",
            unit="USD",
            value="12",
        ),
        _row(
            element_code="5539",
            element="Producer Price Index (2014-2016 = 100)",
            year="1991",
            unit="",
            value="90",
            flag="E",
        ),
        _row(
            year="2010",
            month_code="7001",
            month="January",
            value="",
            flag="",
        ),
        _row(
            item_code="388",
            item="Tomatoes",
            element_code="5530",
            year="2010",
            month_code="7003",
            month="March",
            value="35",
            flag="Z",
        ),
        _row(area_code="1", area_m49="'008", area="Albania"),
    ]
    rows.append(rows[0].copy())
    body = _archive_bytes(rows)
    dataset = _downloaded_dataset(tmp_path / "producer-prices.zip", body)

    profile = profile_dataset(dataset)

    assert profile["profiling_outcome"] == "success"
    assert profile["observation_count"] == 8
    assert profile["frequency_counts"] == {"annual": 6, "monthly": 2}
    assert profile["country_code_values"] == [{"area_code": "114", "m49_code": "'404"}]
    assert {item["item"] for item in profile["distinct_items"]} == {
        "Maize (corn)",
        "Tomatoes",
    }
    assert {entry["element_code"] for entry in profile["measurement_elements"]} == {
        "5530",
        "5531",
        "5532",
        "5539",
    }
    assert {
        (entry["element_code"], entry["frequency"])
        for entry in profile["measurement_elements_by_frequency"]
    } == {
        ("5530", "annual"),
        ("5530", "monthly"),
        ("5531", "annual"),
        ("5532", "annual"),
        ("5539", "annual"),
    }
    assert {"LCU", "SLC", "USD"} <= {
        entry["unit"] for entry in profile["units_by_element"]
    }
    assert profile["null_counts_by_field"]["Value"] == 1
    assert profile["null_counts_by_field"]["Unit"] == 1
    assert profile["years_by_frequency"]["annual"]["distinct_years"] == [1991, 1992]
    assert profile["months_available"] == ["January", "March"]
    assert profile["quality_checks"]["duplicate_business_key_rows"] == 1
    assert profile["quality_checks"]["missing_price_values"] == 1
    assert profile["quality_checks"]["zero_values"]["count"] == 1
    assert profile["quality_checks"]["unrecognized_flags"] == {"Z": 1}
    assert profile["quality_checks"]["missing_flags"] == 1
    flags = {entry["flag"]: entry for entry in profile["flags"]}
    assert flags["X"]["recognized"] is True
    assert flags["X"]["description"] == "Value from external organization"
    assert profile["quality_checks"]["invalid_country_codes"]["count"] == 0
    assert profile["quality_checks"]["unknown_item_codes"]["count"] == 0
    assert profile["actual_columns"][0]["csv_source_type"] == "string"
    assert profile["actual_columns"][0]["observed_value_types"] == {"integer": 8}
    assert profile["archive"]["integrity_check"] == "ZIP CRC check passed"
    json.dumps(profile)


def test_profile_reports_empty_kenya_result() -> None:
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=REQUIRED_FIELDS)
    writer.writeheader()
    writer.writerow(_row(area="Albania", area_code="1", area_m49="'008"))

    profile = profile_csv(StringIO(output.getvalue()), LOOKUPS)

    assert profile["observation_count"] == 0
    assert profile["quality_checks"]["empty_country_result"] is True


def test_profile_rejects_missing_required_columns() -> None:
    with pytest.raises(FaostatProfileError, match="missing required columns"):
        profile_csv(StringIO("Area,Value\nKenya,3\n"), LOOKUPS)


def test_profile_marks_unknown_item_and_invalid_country_codes() -> None:
    rows = [
        _row(item_code="9999", item="Unknown crop"),
        _row(area_code="999", area_m49="'999", item_code="56"),
    ]
    profile = profile_csv(
        StringIO(_csv_bytes(rows).decode("utf-8-sig")),
        LOOKUPS,
    )

    assert profile["quality_checks"]["unknown_item_codes"]["count"] == 1
    assert profile["quality_checks"]["invalid_country_codes"]["count"] == 1


def test_profile_reports_missing_years_and_months_without_filling_them() -> None:
    rows = [
        _row(year="1991", value="100"),
        _row(year="1993", value="110"),
        _row(year="2010", month_code="7001", month="January", value="120"),
        _row(year="2010", month_code="7003", month="March", value="130"),
    ]

    profile = profile_csv(
        StringIO(_csv_bytes(rows).decode("utf-8-sig")),
        LOOKUPS,
    )

    assert profile["years_by_frequency"]["annual"]["missing_between_bounds"] == [1992]
    assert profile["quality_checks"]["discontinuous_series"]["annual"] == {
        "affected_series": 1,
        "missing_periods": 1,
        "examples": [
            {
                "item_code": "56",
                "element_code": "5530",
                "unit": "LCU",
                "missing_period_count": 1,
                "sample_missing_periods": ["1992"],
            }
        ],
    }
    assert (
        profile["quality_checks"]["discontinuous_series"]["monthly"]["missing_periods"]
        == 1
    )
    assert profile["quality_checks"]["discontinuous_series"]["monthly"] == {
        "affected_series": 1,
        "missing_periods": 1,
        "examples": [
            {
                "item_code": "56",
                "element_code": "5530",
                "unit": "LCU",
                "missing_period_count": 1,
                "sample_missing_periods": ["2010-02"],
            }
        ],
    }
    assert profile["observation_count"] == 4


def test_profile_flags_outliers_without_removing_observations() -> None:
    rows = [_row(year=str(year), value="100") for year in range(1991, 1999)]
    rows.append(_row(year="1999", value="1000"))

    profile = profile_csv(
        StringIO(_csv_bytes(rows).decode("utf-8-sig")),
        LOOKUPS,
    )

    outliers = profile["quality_checks"]["outlier_values_tukey_1_5_iqr"]
    assert outliers["outlier_count"] == 1
    assert outliers["by_element_code"]["5530"]["outlier_count"] == 1
    assert outliers["examples"][0]["year"] == 1999
    assert profile["observation_count"] == 9


def test_http_download_to_retries_and_writes_atomically(tmp_path: Path) -> None:
    body = b"streamed-response"
    failure = HTTPError("https://example.test/data", 503, "Unavailable", {}, BytesIO())
    client = HTTPClient(
        timeout=1,
        max_retries=1,
        backoff_factor=0,
        user_agent="unit-test",
    )
    target = tmp_path / "result.zip"

    with (
        patch(
            "kenya_food_intelligence.sources.http.urlopen",
            side_effect=[
                failure,
                FakeResponse(body, "https://example.test/data"),
            ],
        ) as open_url,
        patch("kenya_food_intelligence.sources.http.time.sleep"),
    ):
        response = client.download_to(
            "https://example.test/data",
            target,
            max_bytes=1024,
            chunk_size=4,
        )

    assert target.read_bytes() == body
    assert response.size_bytes == len(body)
    assert response.checksum_sha256 == hashlib.sha256(body).hexdigest()
    assert open_url.call_count == 2
    assert list(tmp_path.iterdir()) == [target]


def test_http_download_to_rejects_oversize_response_and_cleans_partial_file(
    tmp_path: Path,
) -> None:
    from kenya_food_intelligence.sources.http import HTTPResponseTooLarge

    client = HTTPClient(
        timeout=1,
        max_retries=0,
        backoff_factor=0,
        user_agent="unit-test",
    )
    target = tmp_path / "result.zip"
    with patch(
        "kenya_food_intelligence.sources.http.urlopen",
        return_value=FakeResponse(b"too large", "https://example.test/data"),
    ):
        with pytest.raises(HTTPResponseTooLarge):
            client.download_to(
                "https://example.test/data",
                target,
                max_bytes=4,
                chunk_size=2,
            )

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []
