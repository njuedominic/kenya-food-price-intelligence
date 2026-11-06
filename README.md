# Kenya Food Price & Inflation Intelligence Platform

A locally hosted foundation for collecting and analyzing Kenyan food-price,
agricultural, and inflation data. This repository implements the engineering
foundation only; source connectors, ETL, streaming applications, warehouse
models, and dashboards are not included.

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
