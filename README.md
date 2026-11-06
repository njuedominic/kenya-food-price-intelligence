# Kenya Food Price & Inflation Intelligence Platform

A locally hosted foundation for collecting and analyzing Kenyan food-price,
agricultural, and inflation data. It includes standalone World Bank CPI and
KNBS monthly CPI PDF Bronze-ingestion commands; Silver/Gold ETL, streaming
applications, warehouse models, and dashboards are not included.

## Requirements

- Python 3.12, [uv](https://docs.astral.sh/uv/), and Git.
- Docker Engine with the Compose plugin (Docker Desktop on macOS/Windows, or
  Docker Engine on Linux).
- For Windows, use WSL2 and run these commands from the WSL filesystem.

## Local setup

1. Copy `.env.example` to `.env` only if you do not already have a local `.env`.
   Set distinct URL-safe hexadecimal passwords for PostgreSQL, the Airflow
   metadata role, the warehouse role, the Airflow admin, Grafana, and the
   webserver secret. Generate a new password with `openssl rand -hex 32`.
   Generate the Airflow Fernet key with:

   ```sh
   python -c "import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
   ```

   Use that output for `AIRFLOW_FERNET_KEY`. Never commit `.env`. Airflow and
   warehouse roles are separate, non-superuser PostgreSQL logins; the bootstrap
   `POSTGRES_USER` remains a local administrative account.
2. Install and test the Python package:

   ```sh
   uv sync --locked --group dev
   uv run ruff check .
   uv run ruff format --check .
   uv run pytest
   ```
3. Validate Compose configuration before starting services:

   ```sh
   docker compose --env-file .env config --quiet
   docker compose --env-file .env -f docker-compose.yml -f docker-compose.streaming.yml config --quiet
   ```
4. Start only the services you need, for example:

   ```sh
   docker compose --env-file .env up -d postgres grafana
   ```

   To start the batch foundation, use `docker compose --env-file .env up -d`.
   Airflow migrates its metadata database and creates the configured local
   administrator through the idempotent `airflow-init` service. Airflow is at
   <http://localhost:8081>, Spark Master UI at <http://localhost:8082>, and
   Grafana at <http://localhost:3000>. PostgreSQL is published on host port
   5433. Credentials are local values from `.env`.
5. To start Kafka, include the streaming Compose file. Kafka remains opt-in
   because the base Compose file does not include that service:

   ```sh
   docker compose --env-file .env -f docker-compose.yml -f docker-compose.streaming.yml up -d kafka
   ```

   Kafka listens internally at `kafka:9092`; host-based local clients use
   `localhost:9094`. The controller listener is internal only. The broker is
   infrastructure only; no producer or consumer is implemented. Stop it with
   the same Compose files and `stop kafka`. `down` retains named volumes; do not
   use `down --volumes` unless intentionally deleting local state.

The Makefile provides `make install`, `make lint`, `make format`, `make test`,
`make compose-config`, `make up`, `make down`, and `make logs`. Compose commands
use `.env` and fail with a clear message if it is missing. See
[the local setup runbook](docs/runbooks/local-setup.md) for operational details.

## Repository map

- `src/kenya_food_intelligence/`: importable Python package.
- `config/`: development, test, logging, and quality policy examples.
- `airflow/`, `warehouse/`, `infrastructure/`: deployment and future pipeline
  boundaries.
- `data/`: local landing and lake zones; generated data is not tracked.
- `docs/`: requirements, architecture, ADRs, governance, and runbooks.
- `tests/`: unit, integration, contract, and quality test boundaries.

## Source profiling

The Milestone 2 source profilers inspect official source metadata and bounded
samples; they are not production ingestion workflows. Install the project and
its profiling dependencies from the repository root with:

```sh
uv sync --locked --group dev
```

Each command needs network access to its documented source. Reports are written
under `reports/source_profiling/`; downloaded PDFs and archives remain under
Git-ignored `data/landing/` directories.

### World Bank national CPI

The Milestone 2 profiling utility fetches Kenya's `FP.CPI.TOTL` and
`FP.CPI.TOTL.ZG` series from the World Bank Indicators API. It validates the
two-element JSON envelope and every paginated observation, then writes aggregate
statistics and observed field/type summaries (not raw observations) to
`reports/source_profiling/`.

Run it from the repository root with:

```sh
uv run python scripts/profile_world_bank.py
```

HTTP settings can be overridden with `--timeout`, `--max-retries`,
`--backoff-factor`, and `--per-page`; use `--output-dir` to choose another report
directory. The default timeout is 20 seconds, with three retries and exponential
backoff beginning at 0.5 seconds. The API's indicator metadata reports source
attribution; confirm the underlying source's specific license before reuse.
These profiles are source inspection only, not a production ingestion workflow.

### KNBS monthly CPI and national retail prices

Discover and profile the default August–October 2026 releases from the official
KNBS publication listing:

```sh
uv run python scripts/profile_knbs.py
```

The script uses the project's `pdfplumber`, `pypdf`, and shared HTTP client. It
records PDF metadata, source text, extracted table columns, normalized sample
rows, and alignment/data-quality checks. Review the source PDF whenever a
layout or validation check changes; no OCR or production ETL is implemented.
Use `--help` to select months, tune HTTP retries, or change local download and
report directories.

### FAOSTAT producer prices

Profile Kenya rows in the official Producer Prices domain ZIP:

```sh
uv run python scripts/profile_faostat.py
```

The download is streamed with a size bound and read from the ZIP without
extracting the large CSV to disk. The profiler distinguishes annual and
monthly records, measures historical gaps per item/element/unit/frequency
series, and preserves statistical flags. Use `--help` for timeout, retry, and
local directory options.

### World Bank subnational market-price feasibility

Profile the Kenya-only Real Time Food Prices catalog files and emit the bounded
market-data feasibility report:

```sh
uv run python scripts/profile_subnational_prices.py
```

Only the Kenya market file and its small same-version model and ticker metadata
archives are downloaded; the global archive is not fetched. The output
distinguishes model estimates from direct observations and does not infer a
county mapping. WFP and NDMA reconnaissance evidence is a dated snapshot; the
command does not download WFP data or NDMA bulletins. Use `--help` to configure
timeouts, retries, and local directories.

## World Bank Bronze ingestion

The M3.1 command preserves the World Bank Indicators API response bytes as an
immutable local Bronze snapshot. It does not normalize observations, convert
values, or require Docker. Python 3.12 and the repository's locked `uv`
environment are required:

```sh
uv sync --locked --group dev
uv run python scripts/ingest_world_bank.py --help
uv run python scripts/ingest_world_bank.py --indicator FP.CPI.TOTL
uv run python scripts/ingest_world_bank.py --indicator FP.CPI.TOTL.ZG
```

The CLI accepts only those two Kenya CPI indicators. `--timeout`,
`--max-retries`, `--backoff-factor`, and `--per-page` configure the API
requests; `--output-root` defaults to `data/`. Resource safeguards default to
100 pages, 5 MiB per response, and 25 MiB total raw response bytes. Override them
with `--max-pages`, `--max-page-bytes`, and `--max-total-bytes`; the values must
be positive integers, and the total limit cannot be below the per-page limit.

Snapshots are stored at
`data/bronze/world_bank/indicator=<indicator>/snapshots/<snapshot-id>/`.
Each directory contains the exact `page-NNNN.json` response bytes, a canonical
content descriptor, and a snapshot manifest. Append-only per-run records are
stored under `data/manifests/world_bank/runs/`; all runtime data remains
Git-ignored. Inspect a snapshot manifest with:

```sh
snapshot_id="sha256-<64-hex-digest>"
python -m json.tool \
  "data/bronze/world_bank/indicator=FP.CPI.TOTL/snapshots/$snapshot_id/manifest.json"
```

`created` means a new complete snapshot was atomically published; `unchanged`
means matching bytes were found and the existing snapshot passed integrity
verification; `failed` means retrieval or validation failed and a failure run
record was written where storage remained available. Rerunning the same
indicator and parameters is safe: identical raw page bytes reuse the same
snapshot ID, while revised bytes create another immutable snapshot. The
ingestion rejects excessive page counts, oversized responses, and aggregate
payloads beyond the configured limits; it never truncates or publishes partial
pages. All pages are buffered before publication, so these limits bound raw
payload bytes, not total Python process memory or decoded-object overhead. The
manifest and run-record formats, recovery behavior, locking, and filesystem
limitations are documented in
[the Bronze ingestion contract](docs/data-dictionary/bronze-ingestion.md).

## KNBS monthly CPI Bronze ingestion

The M3.2 command discovers one monthly CPI publication from the official
[KNBS CPI and inflation listing](https://www.knbs.or.ke/cpi-and-inflation-rates/),
streams the original PDF bytes, validates the PDF structure, and publishes an
immutable local Bronze snapshot. It does not extract statistical tables or
require Docker:

```sh
uv run python scripts/ingest_knbs.py --help
uv run python scripts/ingest_knbs.py --month 2026-10
```

Only one bounded reference month is accepted per invocation. Use separate
commands for backfills; malformed months such as `2026-13` are rejected. The
command defaults to a 25-second request timeout, two retries with exponential
backoff, and a 40,000,000-byte PDF limit. The official listing response is
limited to 5,000,000 bytes. It rejects missing or ambiguous listing matches and
does not guess a PDF URL. A listing with multiple distinct PDFs for one month
fails for operator review rather than selecting a filename or version
arbitrarily.

Snapshots are stored at
`data/bronze/knbs_cpi_monthly/publication=<YYYY-MM>/snapshots/<snapshot-id>/`,
with `report.pdf`, a canonical content descriptor, and a manifest. Separate
append-only run records are stored at
`data/manifests/knbs_cpi_monthly/runs/<run-id>.json`. Identical bytes for the
same publication reuse the existing snapshot (`unchanged`); revised bytes
create a new snapshot without replacing prior versions. A changed source URL
or filename with identical bytes reuses the content snapshot; the current URL
and filename are recorded in that invocation's run record. Failed acquisition
or PDF validation creates a failed run record and no success snapshot.
Publication dates are included only when verified from official listing
metadata. PDFs are preserved whole; Bronze does not claim county-level
inflation coverage or perform extraction/analysis.
Existing schema-version-1 KNBS snapshots are retained unchanged; the first
post-upgrade ingestion can create a version-2 snapshot rather than migrating
their filename-dependent IDs.

The full identity, validation, attribution, recovery, and filesystem limits are
specified in
[the Bronze ingestion contract](docs/data-dictionary/bronze-ingestion.md).

## Infrastructure notes

Service image tags are pinned: PostgreSQL 16.6 Alpine, Airflow 2.10.5 on Python
3.12, Spark 3.5.5, Grafana 11.4.0, and Kafka 3.9.0. The images used in the
validated Apple Silicon environment are ARM64-compatible. Spark Worker advertises
2 CPU cores and 2 GiB and is container-limited to 2 CPUs and 2 GiB. Other
services share Docker Desktop's configured memory and can consume several GiB;
start only the components needed on smaller machines.

See the [local setup runbook](docs/runbooks/local-setup.md) and
[infrastructure validation report](docs/milestones/infrastructure-validation.md)
for health checks, PostgreSQL role separation, persistence, observed test
results, and troubleshooting.


## License

See [LICENSE](LICENSE).
