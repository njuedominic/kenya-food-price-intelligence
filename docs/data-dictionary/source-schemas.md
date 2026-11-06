# Source schemas and provenance

No upstream schema is asserted until its source, license, access method, and
current schema have been inspected and recorded. KNBS, World Bank Open Data,
FAOSTAT, and the World Bank RTFP market-level dataset have been investigated.
The RTFP study did not establish a validated county-level crosswalk; do not
infer county inflation or food-price observations from national aggregates or
ambiguous market labels.

Every observation should preserve:

| Field | Meaning |
|---|---|
| `source_system` | Identifiable originating provider or explicit synthetic generator |
| `dataset_name` | Provider's dataset/series identifier, not an invented label |
| `source_url` | Retrieval endpoint or source document |
| `ingestion_timestamp` | UTC time this version entered the platform |
| `observation_date` | Source period/date, retaining the source's precision |
| `pipeline_run_id` | Reproducible execution identifier |
| `schema_version` | Version of the platform's validated source contract |
| `record_hash` | Deterministic content identity for revision/deduplication |

Record units, currency, geography, license, retrieval parameters, and source
revision metadata where applicable. Keep official observations, derived
statistics, and synthetic events explicitly classified.

## Source catalog and decisions

The decisions below concern source suitability for the next ingestion-design
stage, not an assertion that any production pipeline is implemented or
production-ready. Keep these measurement concepts separate: national CPI
indices/rates, national consumer retail prices, producer prices, and modeled
market-price estimates.

| Source | Measurement and granularity | Geographic level | Decision | License and principal control |
|---|---|---|---|---|
| World Bank Indicators (`FP.CPI.TOTL`, `FP.CPI.TOTL.ZG`) | National CPI index and annual percentage change; annual | Kenya (national) | **Approved** | API does not provide an explicit license; verify underlying IMF/source terms before redistribution. Validate pagination and preserve nulls. |
| KNBS monthly CPI publications | National CPI rates/index measures and selected retail prices; monthly | Kenya (national) | **Approved with PDF extraction validation controls** | No explicit reuse license identified; obtain permission before redistribution. Validate table titles, page text, alignment, dates, numeric values, and duplicates for every release. |
| FAOSTAT Producer Prices (`PP`, dataset `47c17894-8ca1-4bd0-ba4d-6078e919e9b1`) | Producer price per tonne / producer price index; annual and sparse monthly | Kenya (national producer-price area) | **Approved with historical-coverage and statistical-flag controls** | Catalog lists CC BY 4.0, subject to FAO terms and third-party restrictions. Retain flags and source revisions; do not fill annual/monthly gaps. |
| World Bank RTFP (`KEN_2021_RTFP_v02_M`) | Modeled monthly market food-price estimates | Market locations, with unresolved admin labels | **Restricted market-level pilot** | No explicit reuse license observed in the catalog metadata. Keep estimate labels, model/coverage metadata, units, and revision checksums; no county aggregation without a validated crosswalk. |
| WFP Food Prices via HDX/HAPI | Candidate market-level price observations; mainly monthly, weekly release cadence | Market / admin names and p-codes | **Deferred from production** | HDX declares CC BY-IGO, but Kenya CSV access returned HTTP 202 with an empty body in this environment. Actual Kenya schema, coverage, flags, and observation status remain unvalidated. |
| NDMA County Drought Bulletins | Narrative and charted county-average retail prices; monthly bulletin | County aggregate, not named markets | **Deferred from production** | Bulletin states © NDMA, All Rights Reserved. One report and month were inspected; no open reuse license or reliable recurring structured feed was established. |

Source profiles and tests establish feasibility evidence only. They do not
implement ingestion, analytical transformations, or production quality gates.

## World Bank national CPI: profiled contract

The profiling utility currently requests Kenya (`KEN`) at:

```text
https://api.worldbank.org/v2/country/KEN/indicator/{indicator}?format=json&per_page={n}&page={n}
```

It profiles `FP.CPI.TOTL` and `FP.CPI.TOTL.ZG`. The API response is a two-element
JSON array: element 0 is pagination metadata (`page`, `pages`, `per_page`,
`total`, and sometimes `sourceid`/`lastupdated`); element 1 is the observation
array. For a valid empty result, element 1 may be `null` when the metadata total
is zero. `per_page` may be returned as either a JSON integer or a digit string.
The utility requests each page indicated by `pages`, checks that pagination
metadata stays consistent, and verifies the fetched count against `total`.

Observed first-page response fields from the live API on 2026-11-06:

| Field | Observed JSON type | Meaning |
|---|---|---|
| `indicator` | object | Indicator id and display value |
| `indicator.id` | string | Indicator code |
| `indicator.value` | string | Indicator description |
| `country` | object | Country id and display value |
| `country.id` | string | World Bank country code |
| `country.value` | string | Country name |
| `countryiso3code` | string | ISO3 country code |
| `date` | string | Four-digit observation year |
| `value` | number or null | Observation; null is a missing observation, not an API failure |
| `unit` | string | Unit label (empty for these indicators in the observed response) |
| `obs_status` | string | Observation status (empty in the observed response) |
| `decimal` | integer | Display decimal precision |

`source`, `sourceNote`, and `sourceOrganization` are returned by the indicator
definition endpoint rather than by each observation. The profiled definitions
identified source id `2`, source `World Development Indicators`, and source
organization `International Financial Statistics database, International
Monetary Fund (IMF)`. The CPI series has an underlying IMF source. World Bank
public-license guidance describes CC BY 4.0 as the default for World Bank
datasets it produces and distributes as open data, while also noting that some
datasets use other licenses. The indicator API response does not return a
license. Verify the specific underlying IMF/source terms before redistribution;
do not infer a license solely from the API host.

The utility writes a timestamped aggregate JSON profile under
`reports/source_profiling/`; it does not save raw response bodies. Reports
include source and definition URLs, retrieved time, attribution/license note,
pagination metadata, observation/missing-value/year/duplicate statistics, and
the field/type union observed across all response pages.

## KNBS national CPI and retail prices: profiled contract

The Kenya National Bureau of Statistics (KNBS) CPI and inflation publication
listing is the discovery source:

<https://www.knbs.or.ke/cpi-and-inflation-rates/>

The August, September, and October 2026 PDF URLs below were discovered from the
official listing and verified as HTTP 200 `application/pdf` on 2026-11-06.
Each downloaded document was about 1.1–1.3 MB, contained 13 pages, and had
extractable text on all 13 pages.

| Reference month | Official PDF | Publication date |
|---|---|---|
| 2026-08 | <https://www.knbs.or.ke/wp-content/uploads/2026/08/Kenya-Consumer-Price-Indices-and-Inflation-Rates-August-2026.pdf> | 2026-08-31 |
| 2026-09 | <https://www.knbs.or.ke/wp-content/uploads/2026/09/Kenya-Consumer-Price-Indices-and-Inflation-Rates-September-2026.pdf> | 2026-09-30 |
| 2026-10 | <https://www.knbs.or.ke/wp-content/uploads/2026/10/Kenya-Consumer-Price-Indices-and-Inflation-Rates-October-2026.pdf> | 2026-10-30 |

These are monthly national publications. The profile confirms the three
requested reference months only; it does not establish the complete historical
coverage or the revision policy of the full KNBS archive. The observations are
Kenya-wide, not county-level. Do not derive county records from these national
aggregates.

### Actual report structures observed

The page numbers below were stable across these three reports, but downstream
code must identify tables by their printed titles rather than assume fixed
positions. The first number in each table description is the number of parsed
data rows for August, September, and October respectively.

| Printed table and page | Observed source columns / layout | Parsed rows (Aug/Sep/Oct) |
|---|---|---:|
| Table 1, “One and Twelve-Month Percentage Changes in the Consumer Price Indices”, p. 5 | `13 COICOP Divisions`; `Weight %`; `% Change on last month`; `% Change on same month of previous year` | 14 / 14 / 14 |
| Table 2, “Key Drivers of Inflation for each of the 13 Divisions”, p. 8 | Monthly and yearly infographic text with narrative commodity examples; no stable aligned tabular columns | Not parsed |
| Table 3, “National Average Retail Prices of Selected Commodities”, p. 10 | `Commodity Name`; `Quantity`; `Unit of Measure`; `Average Price {prior-year month}`; `Average Price {prior month}`; `Average Price {current month}`; `% Change over last month`; `% Change over last year` | 26 / 24 / 20 |
| Table 4, “Core and Non-Core Index and Inflation”, p. 12 | `Period`; `Core Index`; `Core Inflation`; `Non-Core Index`; `Non-Core Inflation` | 13 / 13 / 13 |
| Table 5, “Core and Non-Core Contributions”, p. 13 | `Broad Categories`; `Core`; `Non Core`; `Total` | 14 / 14 / 14 |

Table 1 supplies division weights and monthly/year-over-year percentage
changes. Its `Weight %` must not be interpreted as a category `index_value`.
Table 4 supplies core and non-core index series, not a complete set of index
values for all CPI categories. Table 5's source headers do not specify units;
preserve the printed values and source labels rather than assign an unverified
unit. Table 3 contains national average prices and changes; for example, the
October 2026 row for sugar reports quantity `1`, unit `Kg`, prior-year price
`184.35`, prior-month price `165.79`, current-month price `163.84`, monthly
change `-1.2`, and annual change `-11.1`. These values are source text parsed
from the downloaded PDF, not county observations.

The regression suite includes a small manually reviewed October Table 3 text
fixture and places it on page 10 of a controlled PDF. The test runs the actual
`pdfplumber` text extractor and checks parsed cells, numeric alignment, and the
page location. It is an extraction regression fixture, not a copy of the
official PDF; production releases still require visual/source-PDF validation.

### Proposed normalized datasets and source mapping

These are target fields, not source-provided schemas. Retain the exact source
row, printed column labels, PDF URL, reference month, and publication date
alongside normalized values.

| Proposed dataset | Supported source fields and limitations |
|---|---|
| Monthly CPI | Table 1 supports `cpi_category` (from `13 COICOP Divisions`), `monthly_change_pct`, and `annual_change_pct`; `Weight %` is a separate field. It does not provide `index_value` per category. Table 4 supports `period`, `core_index`, `core_inflation_pct`, `non_core_index`, and `non_core_inflation_pct` for the core/non-core series only. |
| National retail commodity prices | Table 3 supports `commodity_name`, `quantity`, `unit_of_measure`, prior-year/prior-month/current-month average prices in KES, `monthly_change_pct`, and `annual_change_pct`. Quantity and unit are part of observation identity: electricity is listed separately for 50 kWh and 200 kWh. |
| Publication metadata | The profiler records `source_id`, `document_url`, `publication_page_url`, `reference_month`, `publication_date`, `retrieved_at_utc`, HTTP status/content type, file size, SHA-256 checksum, page count, extraction status, and license/attribution notes. |

Source dates and prices are present as printed strings before normalization.
Blank or dash values remain missing (`null`) in normalized fields; no missing
price is imputed. Currency for Table 3 is Kenyan shillings (KSh/KES), as stated
by the publication context. CPI index and rate fields are numeric source values;
percentage changes are percentages, while index values are index points. Keep
these measures and source text distinct.

### Retrieval, attribution, revisions, and limitations

`scripts/profile_knbs.py` discovers official PDF links from the listing, then
downloads to the Git-ignored `data/landing/knbs/` directory. It uses bounded
timeouts/retries and validates HTTP status, PDF content type/signature, and
PDF readability. It records retrieval time in UTC, response metadata, byte
size, and SHA-256 so a changed document can be distinguished from an earlier
download. The checked listing and `robots.txt` permitted public access to these
pages during profiling; this is not a blanket grant to republish document
contents.

Run the three-month profile from the repository root with:

```sh
uv run python scripts/profile_knbs.py --timeout 15 --max-retries 1
```

The CLI defaults to August–October 2026 and writes a timestamped JSON report
under `reports/source_profiling/`. Timeout, retry/backoff, month, download
directory, and report directory are configurable CLI options.

Attribute the source to the Kenya National Bureau of Statistics and retain the
publication URL. No explicit reuse license was identified in the publication
or official listing during this profile. Obtain/verify permission before
redistributing PDFs or extracted data. No revision identifier or explicit
revision policy was observed; preserve each retrieved checksum and timestamp
instead of overwriting provenance. The source may replace a document at the
same URL.

Text extraction with `pdfplumber` and `pypdf` successfully read all pages in
these three files. The structured tables are parsed from PDF text lines because
default geometric table extraction omitted or misaligned columns in visually
checked pages. The profiler checks parsed column counts, missing/non-numeric
values, expected titles, dates, duplicate commodity/quantity/unit keys, and
unexpected units. It found no missing pages, missing numeric values,
non-numeric prices, misaligned rows, unparsed table lines, or unexpected units
in these reports. The August and October reports each list distinct 50 kWh and
200 kWh electricity records; they are not duplicates. Table 2 remains
unstructured and is explicitly flagged as ambiguous. Table layouts and row
counts vary between months.

Table 3 units observed across these releases include `Gram`, `Kg`, `Litre`,
`Millilitre`, `Month`, `Per minute`, `Service`, `Ticket`, and `kWh`; the set
varies by month. The PDF parser also emitted warnings about superfluous
whitespace in PDF object headers, although both PDF readers opened the files
and extracted all page text. Keep this parser warning under observation for
future source revisions.

The CPI Explorer homepage
(<https://cpiexplorer.knbs.or.ke/>) was probed as a possible structured
alternative but was unavailable during this bounded check. No undocumented
endpoints were inspected. Automated monthly extraction from the PDF is
promising for Tables 1, 3, 4, and 5 but should remain under validation: compare
new layouts and extracted values with the source PDF, retain quality flags,
and fail or quarantine records when alignment is uncertain. Table 2 is not
suitable for row-based ingestion without a separately verified extraction
method.

The sanitized aggregate profile is written as a timestamped JSON file under
`reports/source_profiling/`. Downloaded PDFs and generated profiles are local
profiling artifacts, not committed raw data or warehouse outputs.

## FAOSTAT producer prices: profiled contract

The profiled domain is **Producer Prices** (`PP`), dataset identifier
`47c17894-8ca1-4bd0-ba4d-6078e919e9b1`.

- FAOSTAT domain: <https://www.fao.org/faostat/en/#data/PP>
- FAOSTAT catalog: <https://data.fao.org/catalog/dataset/47c17894-8ca1-4bd0-ba4d-6078e919e9b1>
- Download selected from the domain page's Bulk Downloads: <https://bulks-faostat.fao.org/production/Prices_E_All_Data_(Normalized).zip>
- Catalog-published schema: <https://data.apps.fao.org/catalog/dataset/ab62e545-a3ce-44d7-ab0d-9728cb638cc2/resource/477cbd8b-e7e8-4bbf-a0cd-ddc6162b6809/download/prices-pp-schema.json>
- Catalog-published query SQL: <https://data.apps.fao.org/catalog/dataset/ab62e545-a3ce-44d7-ab0d-9728cb638cc2/resource/d30487a2-82f2-4f97-a711-935d64eab444/download/prices-pp-producer-prices-query.sql>
- FAOSTAT database terms: <https://www.fao.org/contact-us/terms/db-terms-of-use/en/>

The official page offered an `All Data Normalized` Producer Prices ZIP (11.41
MB as shown by the page). The live download was 11,684,078 bytes; the ZIP held
a 214,286,652-byte CSV plus four small lookup files. The profiler streams the
download to `data/landing/faostat/`, checks its SHA-256 and every ZIP member's
CRC, validates member paths, and reads the CSV directly from the archive. It
does not extract any files or download the whole FAOSTAT database. The shared
HTTP client enforces a timeout, bounded exponential retries, a 50 MB archive
limit, and atomic file replacement.

The catalog also links a query service and SQL containing `item_code` and
`frequency` parameters. A test request using the catalog's maize code `56` and
the SQL's `annual`/`monthly` values returned HTTP 200 `text/csv`. That query
returns the selected item across areas, so the normalized domain ZIP was used
for one-pass Kenya filtering and full commodity/period coverage.

The catalog describes annual data from 1991 and monthly data from January
2010, for 180 countries and 212 products. In the actual Kenya rows from the
archive retrieved on 2026-11-06, annual observations span **1991–2025** with
no missing calendar years at the country-wide level. Monthly records occur in
**2010, 2011, 2016, 2017, 2020, and 2021** only; no monthly records for
2012–2015 or 2018–2019 were present, and no Kenya monthly data later than 2021
was observed. All twelve month names occur in the Kenya rows, but monthly
series have gaps. These are source availability gaps, not synthesized values.

The FAOSTAT domain page showed “Last Update” as **9 October 2026**, matching
the downloaded archive's HTTP `Last-Modified` date. The linked catalog metadata
reports a “Data revision” date of **4 December 2025**. Keep both claims in
provenance; the catalog revision date and current domain-page update date
differ.

### Actual source schema and observed values

The normalized CSV has the following exact source columns. CSV cells are text;
the profile records the observed inferred value types as well as the original
column names.

| Source column | Observed meaning/type |
|---|---|
| `Area Code` | Integer-like source code; Kenya is `114` |
| `Area Code (M49)` | Text M49 code; Kenya is represented as `"'404"` in the file |
| `Area` | Area name; filtered exactly to `Kenya` |
| `Item Code` | Integer-like FAOSTAT item code |
| `Item Code (CPC)` | Text classification code |
| `Item` | Commodity/item label |
| `Element Code` | Integer-like measure code |
| `Element` | Measure label |
| `Year Code` | Integer-like period code |
| `Year` | Integer-like observation year |
| `Months Code` | Integer-like month/annual-period code |
| `Months` | Month name or `Annual value` |
| `Unit` | Text unit; blank for the index rows in this extract |
| `Value` | Numeric text (decimal); nullability is profiled |
| `Flag` | Statistical flag code |

The actual source business key used for duplicate checks is
(`Area Code`, `Item Code`, `Element Code`, `Year Code`, `Months Code`). A
commodity/year pair is insufficient because distinct elements, units, and
month periods are recorded separately. The profiler checks the key over all
Kenya rows and preserves the source key fields.

The live profile contained **8,720** Kenya observations: **8,087 annual** and
**633 monthly**, across **124 distinct item codes/names**. Examples of items
with records include maize (`56`, “Maize (corn)”), dry beans (`176`, “Beans,
dry”), potatoes (`116`), and tomatoes (`388`). The complete observed item
list and counts are in the timestamped machine-readable profile.

Four measurement elements were observed:

| Element code | Actual element label | Annual rows | Monthly rows | Actual `Unit` values |
|---|---|---:|---:|---|
| `5530` | `Producer Price (LCU/tonne)` | 1,284 | 633 | `LCU` |
| `5531` | `Producer Price (SLC/tonne)` | 1,284 | 0 | `SLC` |
| `5532` | `Producer Price (USD/tonne)` | 1,284 | 0 | `USD` |
| `5539` | `Producer Price Index (2014-2016 = 100)` | 4,235 | 0 | blank |

The catalog describes the units as Local Currency Unit per tonne, Standard
Local Currency per tonne, US dollars per tonne, and index (2014–2016 = 100),
respectively. The source CSV encodes the first three with `Unit` labels
`LCU`, `SLC`, and `USD`; it leaves `Unit` blank for the index element. Do not
combine values across elements or treat the index's blank unit as a missing
price. `SLC` is kept distinct from `LCU`: this profile did not verify that
standard local currency is equivalent to Kenyan shillings. Confirm the
currency metadata/table before applying any conversion.

The source `Prices_E_Flags.csv` code table supplies flag descriptions. Kenya
rows included:

| Flag | Source description | Kenya observations |
|---|---|---:|
| `A` | Official value | 4,479 |
| `E` | Estimated value | 4,235 |
| `X` | Value from external organization | 6 |

The source code table also lists `B` (time series break) and `I` (value imputed
by a receiving agency), neither of which occurred in the profiled Kenya rows.
Preserve flags as source attributes; `E` and `X` describe provenance/status,
not invalid numeric values. Missing flag values should be counted separately
from unknown codes.

### Profile quality results and proposed canonical mapping

The profile found no null `Value` cells, negative or zero values, duplicate
business keys, unknown item/element codes, invalid Kenya area codes,
unexpected units, missing flag values, or unrecognized flag codes. `Unit` was
blank for all 4,235 producer-price-index rows, as observed in the source
schema. It found no missing annual calendar years across the country-level
bounds. Across item/measure series, 225 annual series had 1,836 gaps between
their first and last observed years; 24 monthly series had 1,113 missing
months within their observed bounds. Sparse coverage and these gaps are
reported as discontinuities, not imputed or silently removed.

The profile flagged 305 statistical outlier candidates across 342 eligible
item/element/unit/frequency series using Tukey 1.5×IQR fences. These include
80 LCU/t, 69 SLC/t, 54 USD/t, and 102 index values. Long-term trends can
trigger this rule, so candidates are screening flags, not proof of errors;
they are retained and reported, not corrected or dropped. The profile includes
counts by measure and a bounded sample of outlier rows and discontinuities.

Examples from the source records for comparable commodity names are annual
producer-price values in 1991, `LCU` per tonne: maize (`56`) 2,870; potatoes
(`116`) 1,363; and tomatoes (`388`) 2,500. Dry beans (`176`) has a 1997 value
of 25,100 `LCU` per tonne. These are source examples, not KNBS retail prices.

Proposed canonical fields for a future source model are:
`area_code`, `area_m49_code`, `area_name`, `item_code`, `item_name`,
`element_code`, `element_name`, `year`, `month`/frequency, `unit`,
`value`, `statistical_flag`, `source_url`, `retrieved_at_utc`,
`source_checksum_sha256`, and source-revision metadata. These are normalized
target names; they do not replace the exact source fields.

Keep producer prices separate from KNBS consumer retail prices. FAOSTAT prices
are farm-gate observations per tonne or producer-price indices; KNBS reports
national-average retail prices for selected quantities/units. Commodity names
such as maize, beans, potatoes, and tomatoes are potential comparison
candidates, not safe join keys by themselves. A future model must map source
commodity classifications explicitly, resolve monthly-versus-annual periods,
verify the SLC currency interpretation, and apply documented quantity/currency
conversions only when definitions align.

### License, attribution, and revisions

The FAOSTAT catalog lists CC BY 4.0, and FAO’s Statistical Database Terms of
Use state that corporate statistical databases are generally available under
CC BY 4.0 unless dataset metadata says otherwise. Attribute as specified by
FAO, for example: “FAO. 2026. FAOSTAT: Producer Prices. Accessed 06 November
2026. https://www.fao.org/faostat/en/#data/PP Licence: CC-BY-4.0.” The terms
also warn that third-party data may carry additional restrictions; verify
those before redistribution, and do not imply FAO endorsement.

The profiler records retrieval time, HTTP status/content type, `Last-Modified`,
ETag, archive size, and SHA-256. Retain the checksum with each future source
revision rather than overwriting provenance. The generated profile contains
metadata, schema/type summaries, Kenya statistics, quality checks, and small
representative samples only; the raw archive stays local and ignored by Git.

Run the profiler from the repository root with:

```sh
uv run python scripts/profile_faostat.py --timeout 20 --max-retries 1
```

The implementation uses only Python’s standard CSV/ZIP/HTTP libraries and the
project’s existing `truststore`/shared HTTP support; no additional package,
Spark, Airflow, or Kafka dependency is required.

The sanitized report is written as a timestamped JSON file under
`reports/source_profiling/`; it contains the Kenya-specific profile rather than
the downloaded global domain file.

## Kenya subnational market-price feasibility

This is a source-feasibility assessment, not an ingestion contract or evidence
of official county inflation. It compares World Bank model estimates, WFP
market-price source metadata, and one NDMA county bulletin. Keep their distinct
methods, update cycles, units, and geographic aggregation separate.

### Primary: World Bank Real Time Food Prices

- Kenya catalog: <https://microdata.worldbank.org/catalog/6167>
- Catalog file listing: <https://microdata.worldbank.org/api/downloads/KEN_2021_RTFP_v02_M/files?type=data>
- Table metadata and data dictionary:
  <https://microdata.worldbank.org/api/tables/info/fcv/wld_2021_rtfp_v02_m?data_dictionary=true>
- Dataset: `Monthly food price estimates by product and market`; author Bo
  Pieter Johannes Andrée, World Bank Development Data Group.
- The latest profiled Kenya archives were version `2026-11-02`, retrieved
  2026-11-06. Only the Kenya market-data ZIP and its small model-details and
  ticker-information ZIPs were downloaded. Archives are integrity-checked and
  inspected in place without extracting files.
- The catalog reports 233 markets and dates from January 2007 through
  November 2026. The downloaded wide table contained 55,926 market-month rows,
  234 distinct `geo_id` values and 239 monthly dates from `2007-01-01` through
  `2026-11-01`. The additional identifier is the `Market Average` aggregate;
  exclude it from market counts. The model-details file reports a last survey
  point of September 2026 even though modeled rows continue through November.
- All downloaded rows used `KES`. The ticker file names Beans (dry), Maize
  (white), Goat (small local quality), an indexed Food Price Index, and an
  unofficial exchange-rate component. Unit labels are `KG`/`Kg` for beans and
  maize, `1 unit` for goat, and `LCU, Indexed, January 2018 = 1` for the index.
  The source does not establish a retail-versus-wholesale classification in
  the profiled fields.
- The catalog describes this as **modeled monthly food-price estimates**.
  The source dictionary labels open/high/low/close item fields as estimates;
  it describes sparse base item fields only by the item name. Do not equate
  base values with direct market transactions without a separate methodology
  confirmation. The model-details archive names the `cubist (Andree, 2021)`
  imputation method for beans and maize. Dataset metadata says estimates are
  regularly revised as new price information becomes available, and previous
  files are archived.
- Source access is marked `open`, but the inspected catalog and table metadata
  did not expose an explicit reuse license. Confirm the applicable terms and
  attribution before redistribution. The separate HDX mirror has its own
  CC-BY license metadata; that does not establish the World Bank catalog
  license.

The actual 46 wide-table CSV fields are:

| Group | Actual source fields |
|---|---|
| Geography and identity | `ISO3`, `country`, `adm1_name`, `adm2_name`, `mkt_name`, `lat`, `lon`, `geo_id` |
| Time and currency | `price_date`, `year`, `month`, `currency` |
| Model/data coverage | `components`, `start_dense_data`, `last_survey_point`, `data_coverage`, `data_coverage_recent`, `index_confidence_score`, `spatially_interpolated` |
| Item value fields | `beans`, `livestock_goat_s_fao`, `maize_fao`, `o_beans`, `h_beans`, `l_beans`, `c_beans`, `inflation_beans`, `trust_beans`, `o_livestock_goat_s_fao`, `h_livestock_goat_s_fao`, `l_livestock_goat_s_fao`, `c_livestock_goat_s_fao`, `inflation_livestock_goat_s_fao`, `trust_livestock_goat_s_fao`, `o_maize_fao`, `h_maize_fao`, `l_maize_fao`, `c_maize_fao`, `inflation_maize_fao`, `trust_maize_fao`, `o_food_price_index`, `h_food_price_index`, `l_food_price_index`, `c_food_price_index`, `inflation_food_price_index`, `trust_food_price_index` |

The World Bank table dictionary describes all 46 fields as strings. In the
downloaded CSV, identifiers and labels are strings while date, coordinates,
coverage scores, and price/estimate columns are numeric-like strings. Parse
them only after validating their source formats. The proposed market-month
key for this wide table is `(ISO3, geo_id, price_date)`; it had no duplicate
rows in the profiled version.

The file contained 1,839 nonblank `beans`, 2,617 `livestock_goat_s_fao`, and
3,061 `maize_fao` values; the rest of each market-month field was blank.
Their observed ranges were respectively 23–215, 785–8,809, and 6–132 in
source units. These sparse item fields and the model-details observation
counts do not fully agree (the details file lists 1,941 beans and 3,055 maize
observations); retain the discrepancy for source-owner clarification. The
`o_`, `h_`, `l_`, and `c_` estimates are populated even where input item
values are absent. A Tukey 1.5×IQR screen is reported as an outlier-candidate
count only; those candidates are retained and are not asserted to be errors.
For the sparse base values, the screen marked 84 beans, 64 goat, and 85 maize
values as candidates. These cross-market/time fences can flag legitimate
differences and trends; no values are removed. Latitude and longitude are
blank on the 239 monthly `Market Average` aggregate rows, not on the market
records in this profile.

### Geographic interpretation and coverage

The source provides market names, coordinates, a `geo_id`, and raw
`adm1_name`/`adm2_name` labels but no county code. The profile observed 16
distinct admin-1 labels and 36 admin-2 labels. Some lower-level names resemble
current counties, others are localities/subcounties, and the set includes
`Market Average`. The admin levels are not a defensible 47-county crosswalk.
Accordingly, **0 of 47 counties are currently certified as mapped**; this is
not a claim that no markets are located in those counties. Require a reviewed,
versioned market-to-county mapping before any county aggregation. Market
estimates are not automatically representative of their wider county.

### Secondary: WFP / HDX

- WFP DataViz: <https://dataviz.vam.wfp.org/eastern-africa/kenya/economic/prices>
- Official HDX Kenya catalog: <https://data.humdata.org/dataset/wfp-food-prices-for-kenya>
- HAPI food-price documentation:
  <https://hdx-hapi.readthedocs.io/en/latest/data_usage_guides/food_security_nutrition_and_poverty/>
- HDX catalog metadata identified a Kenya WFP Food Prices CSV, updated
  2026-11-04, and declared **Creative Commons Attribution for
  Intergovernmental Organisations (CC BY-IGO)**. Its metadata describes a
  weekly release cadence with mostly monthly observations.
- HDX exposes a dataset/resource modified timestamp rather than an immutable
  Kenya data version. A production consumer should retain retrieval time and
  a content checksum for each snapshot; whether prior WFP values are revised
  in place was not established.
- In the local access probe, the catalog's CSV download URLs returned HTTP
  202 with an empty HTML response. No bypass was attempted; therefore no
  Kenya WFP data rows, coverage counts, or actual CSV schema were validated.
  The documented HAPI API returns fields including
  `commodity_code`, `commodity_name`, `price_flag`, `price_type`, `price`,
  `unit`, `currency_code`, `reference_period_start/end`, admin codes/names,
  `market_code`, `market_name`, `lat`, and `lon`. Its documented request
  requires an application identifier containing an application name and
  contact email. No user email was provided, so none was fabricated or sent.
- HAPI documentation says some market names are p-coded from admin names and
  admin-2 labels can represent mixed administrative levels. Verify the actual
  Kenya market crosswalk and `price_flag`/`price_type` before using it as a
  direct observation series.

### Cross-check: NDMA

The official listing exposed a public preview/download for **Kilifi County
Drought Early Warning Bulletin, September 2026**, “as at 30 September 2026”.
The 20-page PDF returned HTTP 200 on 2026-11-06; its SHA-256 and retrieval
metadata are in the machine-readable feasibility report. Text extraction with
`pypdf` (no OCR) found the crop-price discussion on page 9 and its Beans chart
on page 10:

| Commodity | Reference month | Geographic level | Reported value | Unit | Qualification |
|---|---|---|---:|---|---|
| Maize | September 2026 | Kilifi County average | KES 48.8 | per kg | Narrative says it fell from KES 50.30. |
| Beans | September 2026 | Kilifi County average | KES 144.0 | per kg | Page 10 chart text yields KES 144.1; preserve the 0.1 discrepancy. |

These are county-bulletin averages, not named-market observations. The PDF
footer states © 2026 NDMA, All Rights Reserved; no open reuse license was
identified. One month and county do not establish a consistent monthly series
or a 47-county market mapping.

### Feasibility decision

**B. Approve only a restricted pilot for selected markets.** The World Bank
provides broad monthly market coverage and a reproducible versioned download,
but values are modeled estimates, direct-observation status is not explicit
for every item field, county mapping is unresolved, and model estimates extend
past the last survey point. Keep source-estimate and observed-price series
separate, preserve price type/currency/unit, visibly label estimates and
uncertainty, and do not aggregate into county dashboards until a defensible
crosswalk and representativeness method are reviewed. WFP is a promising
independent source but remains unvalidated here because its download was not
accessible. NDMA is useful documentary context, not a substitute for a
market-level time series.

The repeatable primary-source profile is generated with:

```sh
uv run python scripts/profile_subnational_prices.py
```

The script uses the official Kenya-only file listing, bounded retries and
streamed downloads under `data/landing/subnational/`; it does not fetch the
global RTFP archive or extract ZIP contents to disk. The JSON report includes
only three representative rows, not the full observations. A timestamped JSON
snapshot is written under `reports/source_profiling/`; it is a local profiling
artifact, not a checked-in dataset.
World Bank evidence is refreshed on each CLI run; WFP and NDMA evidence in
that report is a dated, bounded source-reconnaissance snapshot from
2026-11-06 and should be rechecked before a later feasibility decision.
