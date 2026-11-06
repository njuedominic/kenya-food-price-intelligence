# Local setup runbook

## Host preparation

Install Git, Python 3.12, uv, and Docker Compose. On Windows, install WSL2 and
Docker Desktop integration, then work inside the WSL filesystem. On Linux, ensure
the account can run Docker Engine. On macOS, use Docker Desktop. Allocate disk
and memory for PostgreSQL, Airflow, Spark, Grafana, and optional Kafka.

## Configure and validate

From the repository root, copy `.env.example` to `.env`. Replace all local
credential placeholders with distinct secrets. Generate URL-safe hexadecimal
values independently with `openssl rand -hex 32` for each password and the
webserver secret, and a valid Fernet key with
`python -c "import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"`,
and `AIRFLOW_WEBSERVER_SECRET_KEY` with `openssl rand -hex 32`. Never reuse a
database password. Keep `.env` untracked.

Run `uv sync --locked --group dev`, `uv run ruff check .`,
`uv run ruff format --check .`, and `uv run pytest`. Run `make compose-config`
to check both base and streaming merged definitions before startup.

## Start selectively

`docker compose --env-file .env up -d postgres grafana` starts these services
only. `docker compose --env-file .env up -d` starts the full base stack. Include
`-f docker-compose.streaming.yml --profile streaming` for Kafka. The Airflow
initialization service migrates the metadata database and creates the configured
administrator if absent.

Review service health with `docker compose --env-file .env ps`; inspect logs with
`make logs`. Airflow, Spark master, and Grafana local UIs are on ports 8081, 8082,
and 3000 respectively. PostgreSQL host access defaults to 5433. Change host
ports if they are already in use.

## Stop

Run `make down` for base services. Include the streaming Compose file/profile
when stopping the broker. `down` preserves named volumes. Do not use
`down --volumes` unless intentionally deleting local state.
