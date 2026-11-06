"""Text and table structure profiling for KNBS CPI publication PDFs."""

from __future__ import annotations

import re
from collections import Counter
from datetime import date
from typing import Any

import pdfplumber
from pdfminer.pdfparser import PDFSyntaxError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from kenya_food_intelligence.sources.knbs import MONTH_NAMES, DownloadedReport

type JsonObject = dict[str, Any]

TOPIC_PATTERNS = {
    "cpi": re.compile(r"\bconsumer price index|\bconsumer price indices|\bCPI\b", re.I),
    "inflation": re.compile(r"\binflation\b", re.I),
    "food_and_non_alcoholic_beverages": re.compile(
        r"food and non[- ]alcoholic beverages", re.I
    ),
    "national_average_retail_prices": re.compile(
        r"national average retail prices|average retail prices", re.I
    ),
}
ALLOWED_UNITS = {
    "kg",
    "kilogram",
    "kilograms",
    "g",
    "gram",
    "grams",
    "l",
    "litre",
    "litres",
    "liter",
    "liters",
    "ml",
    "each",
    "piece",
    "pieces",
    "bunch",
    "packet",
    "sachet",
    "tin",
    "loaf",
    "no.",
    "number",
    "millilitre",
    "millilitres",
    "kwh",
    "ticket",
    "service",
    "per minute",
    "month",
}
UNIT_PATTERN = re.compile(
    r"^\d+(?:\.\d+)?\s?(?:kg|g|l|ml|litres?|liters?|pieces?|packets?|sachets?)$"
)
NUMBER_PATTERN = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?$")


class PDFProfileError(Exception):
    """The downloaded file could not be profiled as a readable PDF."""


def profile_report(downloaded: DownloadedReport) -> JsonObject:
    """Describe PDF pages, extracted tables, and data-quality observations."""
    try:
        reader = PdfReader(str(downloaded.path), strict=True)
        if reader.is_encrypted:
            raise PDFProfileError("PDF is encrypted and cannot be inspected")
        page_count = len(reader.pages)
        pdf_title = reader.metadata.title if reader.metadata else None
        with pdfplumber.open(downloaded.path) as pdf:
            page_texts = [page.extract_text() or "" for page in pdf.pages]
            page_tables = [
                _extract_page_tables(page)
                if re.search(r"(?m)^\s*Table\s+\d+\s*:", text)
                and not re.search(
                    r"One and Twelve-Month Percentage Changes|"
                    r"Key Drivers of Inflation|National Average Retail Prices|"
                    r"Core and Non-Core Index and Inflation|"
                    r"Core and Non-Core Contributions",
                    text,
                    re.I,
                )
                else []
                for page, text in zip(pdf.pages, page_texts, strict=True)
            ]
    except PDFProfileError:
        raise
    except (OSError, ValueError, PdfReadError, PDFSyntaxError) as error:
        raise PDFProfileError(f"Unable to parse PDF: {error}") from error

    if page_count != len(page_texts):
        raise PDFProfileError(
            "PDF page count differs between parsers "
            f"({page_count} and {len(page_texts)})"
        )

    topic_pages = {
        topic: [
            page_number
            for page_number, text in enumerate(page_texts, start=1)
            if pattern.search(text)
        ]
        for topic, pattern in TOPIC_PATTERNS.items()
    }
    tables = _profile_document_tables(
        page_texts, page_tables, downloaded.report.reference_month
    )
    checks = _validate_profile(
        downloaded.report.reference_month,
        downloaded.report.publication_date,
        page_texts,
        topic_pages,
        tables,
    )
    extracted_text = "\n".join(page_texts)
    publication_date = downloaded.report.publication_date or _date_from_text(
        extracted_text
    )
    publication_date_basis = downloaded.report.publication_date_basis
    if downloaded.report.publication_date is None and publication_date is not None:
        publication_date_basis = "PDF extracted text"
    checks["invalid_publication_date"] = publication_date is not None and not (
        _valid_iso_date(publication_date)
    )
    if publication_date is None:
        checks["publication_date_not_found"] = True
    else:
        checks["publication_date_not_found"] = False

    return {
        "source_id": "KNBS_CPI_MONTHLY",
        "document_url": downloaded.report.document_url,
        "publication_page_url": downloaded.report.publication_page_url,
        "listing_url": "https://www.knbs.or.ke/cpi-and-inflation-rates/",
        "document_title": downloaded.report.title,
        "pdf_metadata_title": pdf_title,
        "reference_month": downloaded.report.reference_month,
        "publication_date": publication_date,
        "publication_date_basis": publication_date_basis,
        "retrieved_at_utc": downloaded.downloaded_at_utc,
        "http": {
            "status": downloaded.http_status,
            "content_type": downloaded.content_type,
            "last_modified": downloaded.last_modified,
            "etag": downloaded.etag,
        },
        "file": {
            "filename": downloaded.path.name,
            "size_bytes": downloaded.file_size_bytes,
            "checksum_sha256": downloaded.checksum_sha256,
        },
        "pdf": {
            "page_count": page_count,
            "text_extractable": any(text.strip() for text in page_texts),
            "pages_with_extractable_text": sum(
                bool(text.strip()) for text in page_texts
            ),
            "pages_with_no_extractable_text": [
                page_number
                for page_number, text in enumerate(page_texts, start=1)
                if not text.strip()
            ],
            "extraction_method": (
                "pypdf page count; pdfplumber text and table extraction"
            ),
        },
        "topic_pages": topic_pages,
        "tables": tables,
        "data_quality_checks": checks,
        "license_attribution": {
            "license": (
                "No explicit reuse license was identified in the publication or "
                "official listing during this profile. Confirm permissions with "
                "KNBS before redistribution."
            ),
            "attribution": (
                f"Kenya National Bureau of Statistics (KNBS), "
                f"{downloaded.report.title}, {downloaded.report.document_url}"
            ),
        },
        "access_restrictions": {
            "robots_txt": "Retrieved; permits public crawling except /wp-admin/.",
            "observed_restriction": None,
        },
        "profiling_outcome": "success",
    }


def compare_layouts(profiles: list[JsonObject]) -> JsonObject:
    """Compare page counts, topic locations, and extracted table signatures."""
    comparison: dict[str, JsonObject] = {}
    for profile in profiles:
        tables = profile.get("tables", [])
        signatures = [
            {
                "page": table["page"],
                "title_context": table.get(
                    "title_context", [table.get("table_title", "")]
                ),
                "column_headers": table["column_headers"],
                "extraction_method": table["extraction_method"],
                "table_extraction_reliable": table.get(
                    "table_extraction_reliable", True
                ),
            }
            for table in tables
        ]
        comparison[str(profile["reference_month"])] = {
            "page_count": profile["pdf"]["page_count"],
            "table_count": len(tables),
            "topic_pages": profile["topic_pages"],
            "table_signatures": signatures,
        }
    return comparison


def parse_numeric_source(value: str | None) -> float | None:
    """Parse a simple printed number, retaining the original text elsewhere."""
    if value is None:
        return None
    cleaned = " ".join(value.replace("\u2212", "-").split())
    if not cleaned or cleaned in {"-", "\u2014", "\u2026", "N/A", "n/a"}:
        return None
    if not NUMBER_PATTERN.fullmatch(cleaned):
        return None
    return float(cleaned.rstrip("%").replace(",", ""))


def _extract_page_tables(page: Any) -> list[tuple[list[list[str | None]], str]]:
    extracted = page.extract_tables()
    if extracted:
        return [(table, "pdfplumber lines/default") for table in extracted]
    text_tables = page.extract_tables(
        table_settings={
            "vertical_strategy": "text",
            "horizontal_strategy": "text",
            "intersection_tolerance": 5,
        }
    )
    return [(table, "pdfplumber text-strategy fallback") for table in text_tables]


def _profile_document_tables(
    page_texts: list[str],
    page_tables: list[list[tuple[list[list[str | None]], str]]],
    reference_month: str,
) -> list[JsonObject]:
    tables: list[JsonObject] = []
    for page_number, (text, extracted_tables) in enumerate(
        zip(page_texts, page_tables, strict=True), start=1
    ):
        if re.search(r"\bTable of Contents\b", text[:500], re.I):
            continue
        title_match = re.search(r"(?m)^\s*(Table\s+\d+\s*:[^\n]+)", text)
        if title_match is None:
            continue
        title = " ".join(title_match.group(1).split())
        if re.search(r"One and Twelve-Month Percentage Changes", title, re.I):
            tables.append(_profile_cpi_division_table(page_number, title, text))
        elif re.search(r"Key Drivers of Inflation", title, re.I):
            tables.append(_profile_infographic_table(page_number, title, text))
        elif re.search(r"National Average Retail Prices", title, re.I):
            tables.append(
                _profile_retail_price_table(page_number, title, text, reference_month)
            )
        elif re.search(r"Core and Non-Core Index and Inflation", title, re.I):
            tables.append(_profile_core_index_table(page_number, title, text))
        elif re.search(r"Core and Non-Core Contributions", title, re.I):
            tables.append(_profile_contributions_table(page_number, title, text))
        else:
            tables.extend(
                _profile_table(page_number, text, table, method)
                for table, method in extracted_tables
            )
    return tables


def _profile_cpi_division_table(page_number: int, title: str, text: str) -> JsonObject:
    headers = [
        "13 COICOP Divisions",
        "Weight %",
        "% Change on last month",
        "% Change on same month of previous year",
    ]
    rows = []
    normalized_rows = []
    unparsed_lines = []
    first_data_line = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or parse_numeric_source(parts[-1]) is None:
            continue
        if any(parse_numeric_source(value) is None for value in parts[-3:]):
            unparsed_lines.append(line)
            continue
        category = " ".join(parts[:-3])
        if not category:
            continue
        row = [
            category,
            *parts[-3:],
        ]
        first_data_line = first_data_line or line
        rows.append(row)
        normalized_rows.append(
            {
                "source_cells": row,
                "normalized_values": {
                    "cpi_category": row[0],
                    "weight_pct": parse_numeric_source(row[1]),
                    "monthly_change_pct": parse_numeric_source(row[2]),
                    "annual_change_pct": parse_numeric_source(row[3]),
                },
            }
        )
    return _structured_text_table(
        page_number,
        title,
        headers,
        rows,
        normalized_rows,
        "pdfplumber text lines; trailing numeric columns validated",
        candidate_cpi=True,
        unparsed_source_lines=unparsed_lines,
        source_header_text=_source_header_lines(text, title, first_data_line),
    )


def _profile_retail_price_table(
    page_number: int, title: str, text: str, reference_month: str
) -> JsonObject:
    current_month = date.fromisoformat(f"{reference_month}-01")
    previous_month = date(
        current_month.year - (current_month.month == 1),
        (current_month.month - 2) % 12 + 1,
        1,
    )
    previous_year = date(current_month.year - 1, current_month.month, 1)
    headers = [
        "Commodity Name",
        "Quantity",
        "Unit of Measure",
        f"Average Price {previous_year.strftime('%B %Y')}",
        f"Average Price {previous_month.strftime('%B %Y')}",
        f"Average Price {current_month.strftime('%B %Y')}",
        "% Change over last month",
        "% Change over last year",
    ]
    rows = []
    normalized_rows = []
    first_data_line = None
    pending_name_lines: list[str] = []
    unparsed_lines: list[str] = []
    has_data = False
    for line in text.splitlines():
        parsed_row = _parse_retail_row(line)
        if parsed_row is None:
            if has_data:
                if line.strip().isdigit():
                    continue
                if re.search(r"\d", line):
                    unparsed_lines.append(line)
                elif _looks_like_wrapped_commodity(line):
                    pending_name_lines.append(" ".join(line.split()))
            continue
        commodity, quantity, unit, numeric_values = parsed_row
        if pending_name_lines:
            commodity = " ".join([*pending_name_lines, commodity]).strip()
            pending_name_lines.clear()
        if not commodity or commodity.lower() in {"quantity", "commodity name"}:
            unparsed_lines.append(line)
            continue
        row = [commodity, quantity, unit, *numeric_values]
        first_data_line = first_data_line or line
        has_data = True
        rows.append(row)
        normalized_rows.append(
            {
                "source_cells": row,
                "normalized_values": {
                    "commodity_name": commodity,
                    "quantity": parse_numeric_source(quantity),
                    "unit_of_measure": unit,
                    "average_price_previous_year_kes": parse_numeric_source(
                        numeric_values[0]
                    ),
                    "average_price_previous_month_kes": parse_numeric_source(
                        numeric_values[1]
                    ),
                    "average_price_current_month_kes": parse_numeric_source(
                        numeric_values[2]
                    ),
                    "monthly_change_pct": parse_numeric_source(numeric_values[3]),
                    "annual_change_pct": parse_numeric_source(numeric_values[4]),
                },
            }
        )
    unparsed_lines.extend(pending_name_lines)
    return _structured_text_table(
        page_number,
        title,
        headers,
        rows,
        normalized_rows,
        "pdfplumber text lines; commodity and trailing numeric fields aligned",
        candidate_price=True,
        unparsed_source_lines=unparsed_lines,
        source_header_text=_source_header_lines(text, title, first_data_line),
    )


def _parse_retail_row(
    line: str,
) -> tuple[str, str, str, list[str]] | None:
    parts = _merge_split_thousands(line.split())
    if len(parts) < 8:
        return None
    numeric_values = parts[-5:]
    if sum(parse_numeric_source(value) is not None for value in numeric_values) < 4:
        return None
    prefix = parts[:-5]
    quantity_index = next(
        (
            index
            for index in range(len(prefix) - 1, -1, -1)
            if parse_numeric_source(prefix[index]) is not None
        ),
        None,
    )
    if quantity_index is None:
        return None
    commodity = " ".join(prefix[:quantity_index])
    quantity = prefix[quantity_index]
    unit = " ".join(prefix[quantity_index + 1 :])
    return commodity, quantity, unit, numeric_values


def _merge_split_thousands(parts: list[str]) -> list[str]:
    merged: list[str] = []
    for part in parts:
        if (
            part.startswith(",")
            and merged
            and merged[-1].isdigit()
            and parse_numeric_source(part[1:]) is not None
        ):
            merged[-1] += part
        else:
            merged.append(part)
    return merged


def _looks_like_wrapped_commodity(line: str) -> bool:
    return bool(line.strip()) and re.search(r"[A-Za-z]", line) is not None


def _profile_core_index_table(page_number: int, title: str, text: str) -> JsonObject:
    headers = [
        "Period",
        "Core Index",
        "Core Inflation",
        "Non-Core Index",
        "Non-Core Inflation",
    ]
    pattern = re.compile(
        r"^(?P<period>[A-Z][a-z]{2}-\d{2})\s+(?P<core_index>\S+)\s+"
        r"(?P<core_inflation>\S+)\s+(?P<non_core_index>\S+)\s+"
        r"(?P<non_core_inflation>\S+)$"
    )
    rows = []
    normalized_rows = []
    first_data_line = None
    for line in text.splitlines():
        match = pattern.match(" ".join(line.split()))
        if match is None:
            continue
        row = [match.group(name) for name in pattern.groupindex]
        first_data_line = first_data_line or line
        rows.append(row)
        normalized_rows.append(
            {
                "source_cells": row,
                "normalized_values": {
                    "period": row[0],
                    "core_index": parse_numeric_source(row[1]),
                    "core_inflation_pct": parse_numeric_source(row[2]),
                    "non_core_index": parse_numeric_source(row[3]),
                    "non_core_inflation_pct": parse_numeric_source(row[4]),
                },
            }
        )
    return _structured_text_table(
        page_number,
        title,
        headers,
        rows,
        normalized_rows,
        "pdfplumber text lines; five aligned source columns",
        candidate_cpi=True,
        source_header_text=_source_header_lines(text, title, first_data_line),
    )


def _profile_contributions_table(page_number: int, title: str, text: str) -> JsonObject:
    headers = ["Broad Categories", "Core", "Non Core", "Total"]
    pattern = re.compile(
        r"^(?P<category>.+?)\s+(?P<core>[\d,.-]+)\s+"
        r"(?P<non_core>[\d,.-]+)\s+(?P<total>[\d,.-]+)$"
    )
    rows = []
    normalized_rows = []
    first_data_line = None
    header_seen = False
    for line in text.splitlines():
        if re.search(r"^\s*Broad Categories\b", line, re.I):
            header_seen = True
            continue
        if not header_seen:
            continue
        match = pattern.match(" ".join(line.split()))
        if match is None:
            continue
        row = [match.group(name) for name in pattern.groupindex]
        first_data_line = first_data_line or line
        rows.append(row)
        normalized_rows.append(
            {
                "source_cells": row,
                "normalized_values": {
                    "category": row[0],
                    "core_contribution_pct_points": parse_numeric_source(row[1]),
                    "non_core_contribution_pct_points": parse_numeric_source(row[2]),
                    "total_contribution_pct_points": parse_numeric_source(row[3]),
                },
            }
        )
    return _structured_text_table(
        page_number,
        title,
        headers,
        rows,
        normalized_rows,
        "pdfplumber text lines; trailing contribution fields aligned",
        candidate_cpi=True,
        source_header_text=_source_header_lines(text, title, first_data_line),
    )


def _profile_infographic_table(page_number: int, title: str, text: str) -> JsonObject:
    return {
        "page": page_number,
        "table_title": title,
        "extraction_method": (
            "pdfplumber text extraction; infographic does not form aligned rows"
        ),
        "column_headers": [],
        "source_header_text": _source_header_lines(text, title, None)[:5],
        "data_row_count": 0,
        "sample_source_rows": [],
        "source_rows": [],
        "normalized_sample_rows": [],
        "row_width_counts": {},
        "misaligned_rows": [],
        "ambiguous_header": True,
        "candidate_cpi_table": True,
        "candidate_price_table": False,
        "table_extraction_reliable": False,
    }


def _structured_text_table(
    page_number: int,
    title: str,
    headers: list[str],
    rows: list[list[str]],
    normalized_rows: list[JsonObject],
    method: str,
    *,
    candidate_cpi: bool = False,
    candidate_price: bool = False,
    unparsed_source_lines: list[str] | None = None,
    source_header_text: list[str] | None = None,
) -> JsonObject:
    return {
        "page": page_number,
        "table_title": title,
        "extraction_method": method,
        "column_headers": headers,
        "source_header_text": source_header_text or headers,
        "data_row_count": len(rows),
        "sample_source_rows": rows[:8],
        "source_rows": rows,
        "unparsed_source_lines": unparsed_source_lines or [],
        "normalized_sample_rows": normalized_rows[:8],
        "row_width_counts": {str(len(headers)): len(rows)},
        "misaligned_rows": [
            {
                "row_number": index + 2,
                "expected_columns": len(headers),
                "actual_columns": len(row),
            }
            for index, row in enumerate(rows)
            if len(row) != len(headers)
        ],
        "ambiguous_header": not rows,
        "candidate_cpi_table": candidate_cpi,
        "candidate_price_table": candidate_price,
        "table_extraction_reliable": bool(rows) and not unparsed_source_lines,
    }


def _source_header_lines(
    text: str, title: str, first_data_line: str | None
) -> list[str]:
    lines = text.splitlines()
    title_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip().startswith(title.split(":", 1)[0])
        ),
        None,
    )
    if title_index is None:
        return []
    if first_data_line is None:
        return [line for line in lines[title_index + 1 :] if line.strip()]
    data_index = next(
        (
            index
            for index in range(title_index + 1, len(lines))
            if lines[index] == first_data_line
        ),
        len(lines),
    )
    return [line for line in lines[title_index + 1 : data_index] if line.strip()]


def _profile_table(
    page_number: int,
    page_text: str,
    raw_table: list[list[str | None]],
    method: str,
) -> JsonObject:
    indexed_rows = [
        (index, [_clean_cell(cell) for cell in row])
        for index, row in enumerate(raw_table)
        if any(cell and cell.strip() for cell in row)
    ]
    rows = [row for _, row in indexed_rows]
    header_candidates = rows[: min(len(rows), 5)]
    header_index = (
        max(
            range(len(header_candidates)),
            key=lambda index: sum(
                cell is not None for cell in header_candidates[index]
            ),
        )
        if header_candidates
        else 0
    )
    headers = rows[header_index] if rows else []
    data_rows = rows[header_index + 1 :] if len(rows) > header_index + 1 else []
    raw_header_index = indexed_rows[header_index][0] if indexed_rows else 0
    raw_headers = (
        raw_table[raw_header_index] if raw_header_index < len(raw_table) else []
    )
    nonempty_headers = [header for header in headers if header]
    header_ambiguous = (
        not headers
        or len(nonempty_headers) != len(headers)
        or any("\n" in header for header in raw_headers if header)
    )
    row_widths = Counter(len(row) for row in data_rows)
    misaligned_rows = [
        {
            "row_number": index + 2,
            "expected_columns": len(headers),
            "actual_columns": len(row),
        }
        for index, row in enumerate(data_rows)
        if len(row) != len(headers)
    ]
    headers_joined = " ".join(header or "" for header in headers)
    is_price_table = bool(re.search(r"price|commodity|retail", headers_joined, re.I))
    is_cpi_table = bool(
        re.search(r"index|inflation|CPI|rate|annual|monthly", headers_joined, re.I)
    )
    heading_lines = [
        " ".join(line.split())
        for line in page_text.splitlines()
        if re.search(
            r"consumer price|CPI|inflation|food|retail|commodity|price", line, re.I
        )
    ]
    return {
        "page": page_number,
        "title_context": heading_lines[:6],
        "extraction_method": method,
        "column_headers": headers,
        "source_header_text": raw_headers,
        "data_row_count": len(data_rows),
        "sample_source_rows": data_rows[:8],
        "source_rows": data_rows,
        "normalized_sample_rows": [
            {
                "source_cells": raw_table[indexed_rows[header_index + index + 1][0]],
                "numeric_values": {
                    headers[column]: parse_numeric_source(value)
                    for column, value in enumerate(row)
                    if value is not None
                    and column < len(headers)
                    and parse_numeric_source(value) is not None
                },
            }
            for index, row in enumerate(data_rows[:8])
        ],
        "row_width_counts": dict(sorted(row_widths.items())),
        "misaligned_rows": misaligned_rows,
        "ambiguous_header": header_ambiguous,
        "candidate_cpi_table": is_cpi_table,
        "candidate_price_table": is_price_table,
    }


def _validate_profile(
    reference_month: str,
    publication_date: str | None,
    page_texts: list[str],
    topic_pages: dict[str, list[int]],
    tables: list[JsonObject],
) -> JsonObject:
    checks: JsonObject = {
        "invalid_reference_month": not _valid_reference_month(reference_month),
        "missing_pages": (
            [index + 1 for index, text in enumerate(page_texts) if not text.strip()]
            if page_texts
            else ["PDF contains no pages"]
        ),
        "missing_expected_topics": [
            topic for topic, pages in topic_pages.items() if not pages
        ],
        "missing_expected_table_titles": [
            title
            for title in (
                "Table 1: One and Twelve-Month Percentage Changes",
                "Table 2: Key Drivers of Inflation",
                "Table 3: National Average Retail Prices",
                "Table 4: Core and Non-Core Index and Inflation",
                "Table 5: Core and Non-Core Contributions",
            )
            if not any(
                title.lower() in str(table.get("table_title", "")).lower()
                for table in tables
            )
        ],
        "invalid_publication_date": (
            publication_date is not None and not _valid_iso_date(publication_date)
        ),
    }
    non_numeric_cpi: list[JsonObject] = []
    missing_numeric_values: list[JsonObject] = []
    missing_prices: list[JsonObject] = []
    unexpected_units: list[JsonObject] = []
    ambiguous_headers: list[int] = []
    misaligned_tables: list[int] = []
    commodity_keys: Counter[tuple[str, str, str]] = Counter()
    duplicate_commodities: list[JsonObject] = []

    for table in tables:
        headers = table["column_headers"]
        rows = table["source_rows"]
        if table["ambiguous_header"]:
            ambiguous_headers.append(table["page"])
        if table["misaligned_rows"] or table.get("unparsed_source_lines"):
            misaligned_tables.append(table["page"])
        normalized_headers = [_header_key(value) for value in headers]
        price_columns = [
            index
            for index, header in enumerate(normalized_headers)
            if "price" in header or "kes" in header or "ksh" in header
        ]
        unit_columns = [
            index for index, header in enumerate(normalized_headers) if "unit" in header
        ]
        quantity_columns = [
            index
            for index, header in enumerate(normalized_headers)
            if "quantity" in header
        ]
        commodity_columns = [
            index
            for index, header in enumerate(normalized_headers)
            if "commodity" in header or "item" in header or "product" in header
        ]
        numeric_columns = [
            index
            for index, header in enumerate(normalized_headers)
            if re.search(
                r"index|inflation|rate|annual|monthly|change|weight|CPI",
                header,
                re.I,
            )
        ]
        for offset, row in enumerate(rows):
            for column in numeric_columns:
                value = row[column] if column < len(row) else None
                if value is None or value.strip() in {"", "-", "\u2014"}:
                    missing_numeric_values.append(
                        {
                            "page": table["page"],
                            "row_number": offset + 2,
                            "column": headers[column],
                            "source_value": value,
                        }
                    )
                elif parse_numeric_source(value) is None:
                    non_numeric_cpi.append(
                        {
                            "page": table["page"],
                            "row_number": offset + 2,
                            "column": headers[column],
                            "source_value": value,
                        }
                    )
            for column in price_columns:
                value = row[column] if column < len(row) else None
                if value is None or value.strip() in {"", "-", "\u2014"}:
                    missing_prices.append(
                        {
                            "page": table["page"],
                            "row_number": offset + 2,
                            "column": headers[column],
                        }
                    )
                elif parse_numeric_source(value) is None:
                    non_numeric_cpi.append(
                        {
                            "page": table["page"],
                            "row_number": offset + 2,
                            "column": headers[column],
                            "source_value": value,
                            "reason": "Non-numeric price cell",
                        }
                    )
            for column in unit_columns:
                value = row[column] if column < len(row) else None
                if value:
                    unit = value.strip().lower()
                    if unit not in ALLOWED_UNITS and not UNIT_PATTERN.fullmatch(unit):
                        unexpected_units.append(
                            {
                                "page": table["page"],
                                "row_number": offset + 2,
                                "source_unit": value,
                            }
                        )
            if table["candidate_price_table"] and row:
                index = commodity_columns[0] if commodity_columns else 0
                if index < len(row) and row[index]:
                    commodity = " ".join(row[index].casefold().split())
                    if commodity in {"commodity", "item", "product"}:
                        continue
                    quantity = (
                        " ".join(row[quantity_columns[0]].casefold().split())
                        if quantity_columns and quantity_columns[0] < len(row)
                        else ""
                    )
                    unit = (
                        " ".join(row[unit_columns[0]].casefold().split())
                        if unit_columns and unit_columns[0] < len(row)
                        else ""
                    )
                    duplicate_key = (commodity, quantity, unit)
                    commodity_keys[duplicate_key] += 1
                    if commodity_keys[duplicate_key] > 1:
                        duplicate_commodities.append(
                            {
                                "reference_month": reference_month,
                                "commodity_source_text": row[index],
                                "quantity_source_text": (
                                    row[quantity_columns[0]]
                                    if quantity_columns
                                    and quantity_columns[0] < len(row)
                                    else None
                                ),
                                "unit_source_text": (
                                    row[unit_columns[0]]
                                    if unit_columns and unit_columns[0] < len(row)
                                    else None
                                ),
                                "count": commodity_keys[duplicate_key],
                            }
                        )

    checks.update(
        {
            "non_numeric_cpi_or_price_values": non_numeric_cpi,
            "missing_numeric_values": missing_numeric_values,
            "missing_prices": missing_prices,
            "duplicate_commodity_month_observations": duplicate_commodities,
            "unexpected_units": unexpected_units,
            "ambiguous_multi_line_or_blank_headers": sorted(set(ambiguous_headers)),
            "misaligned_table_pages": sorted(set(misaligned_tables)),
            "unparsed_table_lines": [
                {"page": table["page"], "lines": table["unparsed_source_lines"]}
                for table in tables
                if table.get("unparsed_source_lines")
            ],
        }
    )
    return checks


def _clean_cell(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.replace("\u00a0", " ").split())
    return cleaned or None


def _header_key(value: str | None) -> str:
    return " ".join((value or "").replace("\n", " ").lower().split())


def _valid_reference_month(reference_month: str) -> bool:
    try:
        date.fromisoformat(f"{reference_month}-01")
    except ValueError:
        return False
    return True


def _valid_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _date_from_text(text: str) -> str | None:
    match = re.search(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(MONTH_NAMES) + r")\s+(\d{4})\b",
        text,
        re.I,
    )
    if match is None:
        return None
    month_number = next(
        index
        for index, month_name in enumerate(
            MONTH_NAMES,
            start=1,
        )
        if month_name.lower() == match.group(2).lower()
    )
    try:
        return date(int(match.group(3)), month_number, int(match.group(1))).isoformat()
    except ValueError:
        return None
