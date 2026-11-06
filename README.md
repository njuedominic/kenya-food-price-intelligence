# Kenya Food Price & Inflation Intelligence Platform

A locally hosted foundation for collecting and analyzing Kenyan food-price,
agricultural, and inflation data. This repository currently implements **Milestone
1: engineering foundation and repository scaffolding only**. It does not contain
source connectors, production data pipelines, a dimensional warehouse, streaming
applications, or dashboards.

## Principles

- Official observations, derived statistics, and synthetic events must remain
  distinguishable.
- Source availability, licensing, schema, and update frequency must be verified
  before a connector is implemented. No county-level inflation observations or
  unverified APIs are assumed.
- Application and infrastructure services run locally in Docker Compose. Public
  data providers are external sources, not hosting dependencies.
- Local filesystems hold the lake; PostgreSQL is the planned analytical warehouse.

## Requirements

- Python 3.12, [uv](https://docs.astral.sh/uv/), and Git.
- Docker Engine with the Compose plugin (Docker Desktop on macOS/Windows, or
  Docker Engine on Linux).
- For Windows, use WSL2 and run these commands from the WSL filesystem.
- About 8 GB of available memory is recommended for the full local stack; reduce
  concurrent services on smaller machines.

## Local setup

1. Copy `.env.example` to `.env`. Replace every `replace-me` value with local
   values; run `openssl rand -hex 32` separately for each password and secret
   (PostgreSQL, Airflow admin, Grafana admin, and webserver). Generate the Airflow
   Fernet key with:

   ```sh
   python -c "import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
   ```

   Use that output for `AIRFLOW_FERNET_KEY`. Never commit `.env`.
2. Install and test the Python package:

   ```sh
   uv sync --locked --group dev
   uv run ruff check .
   uv run ruff format --check .
   uv run pytest
   ```
3. Validate Compose configuration before starting services:

   ```sh
   docker compose --env-file .env config
   docker compose --env-file .env -f docker-compose.yml -f docker-compose.streaming.yml --profile streaming config
   ```
4. Start only the services you need, for example:

   ```sh
   docker compose --env-file .env up -d postgres grafana
   ```

   To start the batch foundation, use `docker compose --env-file .env up -d`.
   Airflow initializes its metadata database and local administrator on first
   start. Airflow is at <http://localhost:8081>; Grafana is at
   <http://localhost:3000>. Credentials are the local values in `.env`.
5. To include the isolated Kafka broker, add the streaming file and profile:

   ```sh
   docker compose --env-file .env -f docker-compose.yml -f docker-compose.streaming.yml --profile streaming up -d
   ```

   The broker is infrastructure only; no producer or consumer is implemented.
   Stop services with the same Compose files and `down`. Persistent named volumes
   are retained unless explicitly removed with `down --volumes`.

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

## Data governance

Every ingested record is expected to retain `source_system`, `dataset_name`,
`source_url`, `ingestion_timestamp`, `observation_date`, `pipeline_run_id`,
`schema_version`, and `record_hash`. Preserve raw source responses in immutable
Bronze storage; document transformations into Silver and Gold. Do not silently
drop missing or invalid values: validate, record the reason, and quarantine
invalid records. Deduplication, units/currency normalization, late data, source
revisions, and freshness thresholds must be source-aware and auditable.
Simulated records must be labeled synthetic at creation and never presented as
official observations. See [data governance](docs/data-dictionary/source-schemas.md).

## Development and delivery

Use GitHub Flow: create a `feature/*`, `fix/*`, `docs/*`, `chore/*`, or `test/*`
branch; open a pull request to `main`; pass CI; and squash merge. Repository files
cannot enforce branch protection. Configure required reviews and the CI status
check in GitHub repository settings. See [CONTRIBUTING.md](CONTRIBUTING.md).

GitHub Actions runs tests and static checks, validates Compose configuration,
and builds Dockerfiles only when real Dockerfiles are added. The release workflow
is manual validation only. Local deployment is performed by pulling an approved
release and following the Compose runbook; CI does not deploy to a developer
machine.

## Current limitations

This scaffold does not verify or implement KNBS, World Bank, FAOSTAT, or county
data connectors. The PostgreSQL, Airflow, Spark, Grafana, and Kafka Compose
services provide local infrastructure, not data products. Grafana has no
project dashboard or provisioned data source yet. Spark is a standalone cluster
only; Spark Structured Streaming jobs are not included. Validate source access,
licenses, schemas, resource needs, and image compatibility before production use.

## License

See [LICENSE](LICENSE).
