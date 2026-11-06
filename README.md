# Kenya Food Price & Inflation Intelligence Platform

A Kenyan food prices and inflation platform for collecting and analyzing Kenyan food-price,
agricultural, and inflation data. 

## Requirements

- Python 3.12, [uv](https://docs.astral.sh/uv/), and Git.
- Docker Engine with the Compose plugin (Docker Desktop on macOS/Windows, or
  Docker Engine on Linux).
- For Windows, use WSL2 and run these commands from the WSL filesystem.

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






## License

See [LICENSE](LICENSE).
