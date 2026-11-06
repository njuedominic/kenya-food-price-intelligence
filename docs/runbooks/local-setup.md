# Local setup runbook

## Host preparation

Install Git, Python 3.12, uv, and Docker Compose. On Windows, install WSL2 and
Docker Desktop integration, then work inside the WSL filesystem. On Linux, ensure
the account can run Docker Engine. On macOS, use Docker Desktop. Allocate disk
and memory for PostgreSQL, Airflow, Spark, Grafana, and optional Kafka.

## Configure and validate

From the repository root, copy `.env.example` to `.env` only if a local `.env`
does not already exist. Preserve existing local settings. Generate distinct
URL-safe hexadecimal values with `openssl rand -hex 32` for
`POSTGRES_PASSWORD`, `AIRFLOW_DB_PASSWORD`, `WAREHOUSE_DB_PASSWORD`,
`AIRFLOW_ADMIN_PASSWORD`, `GRAFANA_ADMIN_PASSWORD`, and
`AIRFLOW_WEBSERVER_SECRET_KEY`. Generate `AIRFLOW_FERNET_KEY` with
`python -c "import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"`.
Never reuse a password. Keep `.env` untracked.

Run `uv sync --locked --group dev`, `uv run ruff check .`,
`uv run ruff format --check .`, and `uv run pytest`. Run `make compose-config`
to check both base and streaming merged definitions before startup.

## Start selectively

`docker compose --env-file .env up -d postgres grafana` starts these services
only. `docker compose --env-file .env up -d` starts the full base stack. Start
Kafka separately with
`docker compose --env-file .env -f docker-compose.yml -f docker-compose.streaming.yml up -d kafka`.
Kafka is not included in the base Compose file. The Airflow initialization
service migrates the metadata database and creates the configured administrator
if absent. It checks migration state first and skips the migration when the
schema is already current. Its database and warehouse use distinct PostgreSQL
login roles. PostgreSQL's initialization SQL runs automatically only when its
data directory is empty. For a pre-existing volume that has not yet been
upgraded with the dedicated roles, start PostgreSQL and apply the idempotent
role/bootstrap SQL once before starting Airflow:

```sh
docker compose --env-file .env up -d postgres
docker compose --env-file .env exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d food_intelligence -v ON_ERROR_STOP=1 -f /docker-entrypoint-initdb.d/10-init-databases.sql'
docker compose --env-file .env up -d
```

Review service health with `docker compose --env-file .env ps`; inspect logs with
`make logs`. Airflow, Spark master, and Grafana local UIs are on ports 8081, 8082,
and 3000 respectively. PostgreSQL host access defaults to 5433. Change host
ports if they are already in use. Kafka's internal broker listener is 9092 and
its host listener is 9094; controller port 9093 is not published.

The Spark Worker advertises 2 cores and 2 GiB and has container limits of 2 CPU
and 2 GiB. The Docker Desktop resource pool observed during validation was 14
CPUs and about 7.75 GiB; the Airflow webserver used about 1.15 GiB. Avoid
starting unnecessary services on memory-constrained hosts.

For health checks, use `docker compose ps`, PostgreSQL `pg_isready`, Airflow's
`/health` endpoint, Spark Master `/json/`, Grafana `/api/health`, and Kafka's
Compose health check. See the infrastructure validation report for the exact
commands and verified outcomes. No Grafana datasource is provisioned yet.

## Stop

Run `make down` for base services. Stop Kafka with the same two Compose files
and `stop kafka`. `down` preserves named volumes. Do not use
`down --volumes` unless intentionally deleting local state.
