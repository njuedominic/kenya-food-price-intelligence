# Infrastructure validation report

- **Date:** 2026-11-06
- **Repository branch:** `chore/initial-scaffold`
- **Scope:** Local Docker Compose audit and runtime validation; no data pipelines
  or production dashboards implemented.
- **Safety:** Only `kenya-food-price-intelligence` Compose services were
  started, stopped, or recreated. No other project containers were modified.
  Existing named volumes were retained. No credentials are recorded here.

## A. Infrastructure status

Docker Desktop reported Docker Engine 29.6.1, 14 CPUs, and 8,320,954,368 bytes
(about 7.75 GiB) available to containers. At the initial inspection only this
project's Spark Master and Worker were running. The Master owned host port 8082;
the macOS PostgreSQL process listened on 5432, while this project uses 5433.
Other planned project ports were free before startup.

| Service | Configured image / version | Final state | Functional validation |
|---|---|---|---|
| PostgreSQL | `postgres:16.6-alpine` | Running, healthy | Authenticated `SELECT 1`; `airflow` and `food_intelligence` databases; separate non-superuser roles and database access boundaries verified. Temporary row survived a PostgreSQL restart and was removed. |
| Airflow init | `apache/airflow:2.10.5-python3.12` | One-shot exited 0 | Metadata migration and administrator presence check succeeded. Subsequent init run reported schema current and skipped migration. |
| Airflow scheduler | `apache/airflow:2.10.5-python3.12` | Running, healthy | `airflow db check` succeeded; scheduler job check found one live job. |
| Airflow webserver | `apache/airflow:2.10.5-python3.12` | Running, healthy | `/health` returned HTTP 200 with healthy metadata database and scheduler. DAG directory mounted; zero DAGs is expected at this milestone. |
| Spark Master | `apache/spark:3.5.5` | Running, healthy | Master REST endpoint reports `ALIVE`; UI reachable at `127.0.0.1:8082`. |
| Spark Worker | `apache/spark:3.5.5` | Running, healthy | Master reports one live worker advertising 2 cores and 2048 MiB. Container limits are 2 CPUs and 2 GiB. A distributed DataFrame transformation/action returned the expected rows. |
| Grafana | `grafana/grafana:11.4.0` | Running, healthy | `/api/health` returned HTTP 200 and database `ok`; local admin API authentication succeeded. Organization remained available after a Grafana restart. Grafana-to-PostgreSQL TCP connectivity succeeded. |
| Kafka | `apache/kafka:3.9.0` | Running, healthy | Single-node KRaft broker API reachable on the internal listener; a temporary topic round-tripped a message and the message survived broker restart. Temporary topic was deleted. Host listener TCP on 9094 succeeded. |

All five images were inspected as `linux/arm64`. Published ports bind to
loopback only: PostgreSQL 5433, Airflow 8081, Spark UI 8082, Grafana 3000, and
Kafka external listener 9094. Kafka's internal broker listener 9092 and KRaft
controller listener 9093 are not published.

Observed final container usage was approximately 2.2 GiB total: PostgreSQL
35 MiB, Airflow webserver 734 MiB, scheduler 403 MiB, Spark Master 272 MiB,
Worker 233 MiB, Grafana 66 MiB, and Kafka 557 MiB. Kafka has a 1 GiB container
limit; Spark Worker is limited to 2 GiB. The Spark Worker advertised 2048 MiB,
which is a separate measurement from its observed container use.

## B. Issues discovered and fixes

| Severity | Finding | Resolution / impact |
|---|---|---|
| High — startup blocker | The pre-existing project PostgreSQL container had no attached network endpoint and no published 5433 mapping. Airflow could not resolve the `postgres` service name. | Recreated only the project's PostgreSQL container with the current Compose definition. Its named data volume was preserved; DNS, health, role-authenticated SQL, and host mapping then passed. |
| High — privilege isolation | Both databases initially used the PostgreSQL bootstrap superuser. | Added dedicated `airflow_service` and `warehouse_service` logins, separate database ownership, revoked cross-database `PUBLIC CONNECT`, and transferred existing application objects. Airflow and warehouse roles are non-superusers and cannot connect to the other database. |
| Medium — Kafka undiscoverable | Kafka was behind a `streaming` profile, so the required two-file merged Compose model omitted it unless a profile was specified. | Removed the profile. Kafka is included only when the optional streaming Compose file is supplied, and is now listed in the merged services. |
| Medium — Spark resource over-allocation | Worker environment settings advertised 2 cores and 2 GiB, but Docker imposed no actual container limits. | Added a 2-CPU and 2-GiB container cap, plus Worker and Master health checks. Verified both the Docker limits and worker-advertised resources after recreation. |
| Medium — host exposure | Published development ports bound to all interfaces by default. | Bound PostgreSQL, Airflow, Spark UI, Grafana, and Kafka's external listener to `127.0.0.1`. |
| Low — repeated Airflow migration work | The one-shot init service is revisited when Compose starts dependent services. | The init script now checks migration state first and skips `airflow db migrate` when the schema is current. The idempotent administrator-existence check remains. |

An initial Airflow initialization attempt failed because of the stale PostgreSQL
network attachment. After PostgreSQL recreation, initialization and all Airflow
health checks passed. Spark Master retained an old `DEAD` worker record after
the Worker was recreated; one current `ALIVE` Worker registered with the target
resources, and the Spark job succeeded. No data-volume deletion or Docker-wide
cleanup was run.

## C. Changes made

- `docker-compose.yml`: role-specific Airflow DB URL, database bootstrap
  environment, loopback-only ports, Spark Worker CPU/memory limits, and
  Airflow/Spark health checks.
- `docker-compose.streaming.yml`: removed the profile gate, added internal and
  host external KRaft listeners, loopback-only host binding, and bounded Kafka
  resources.
- `infrastructure/postgres/init-databases.sql`: idempotent creation of
  dedicated login roles/databases, access isolation, and ownership of existing
  application objects.
- `.env.example`: documented separate Airflow and warehouse database role
  settings. Missing role credentials were appended to the existing ignored
  local `.env`; its existing values were preserved and no secret values were
  printed or committed.
- `scripts/airflow-init.sh`: skip metadata migration when already current.
- `README.md`, `docs/runbooks/local-setup.md`, PostgreSQL and deployment ADRs,
  acceptance criteria, and CI/Makefile Compose validation were aligned with the
  actual configuration. The Makefile validator uses `config --quiet` so it does
  not print interpolated secret values.
- Added this runtime validation report.

## D. Commands and results

| Command/check | Result |
|---|---|
| `docker info --format 'Docker server={{.ServerVersion}} OS={{.OperatingSystem}} CPUs={{.NCPU}} MemoryBytes={{.MemTotal}}'` | Passed; Docker 29.6.1, 14 CPUs, about 7.75 GiB. |
| `docker ps --format ...`, `docker stats --no-stream`, `netstat -anv -p tcp` | Passed; initially only this project's Spark services were running. 8082 was the project's Master; host PostgreSQL used 5432, not the configured 5433. Other target ports were free. |
| `docker compose --env-file .env config --quiet` | Passed. |
| `docker compose --env-file .env -f docker-compose.yml -f docker-compose.streaming.yml config --quiet` | Passed; merged model contains all eight services including Kafka. |
| `make compose-config` | Passed for the base and merged model; quiet mode did not print secrets. |
| `docker compose ... up -d` with both Compose files | Passed; all long-running services became healthy. Airflow init exited successfully as expected. |
| Authenticated PostgreSQL `psql` queries using container environment credentials | Passed for bootstrap `SELECT 1`, `airflow_service` on `airflow`, and `warehouse_service` on `food_intelligence`. Cross-database `CONNECT` checks returned false. |
| PostgreSQL temporary-table restart test | Passed; value `42` remained after restart, then the temporary table was dropped. |
| `airflow db check`, `airflow jobs check --job-type SchedulerJob ...`, and `curl http://127.0.0.1:8081/health` | Passed; metadata connection succeeded, one scheduler heartbeat was found, and HTTP health returned 200. |
| Spark Master/Worker `/json/` checks and Docker `inspect` limits | Passed; one ALIVE Worker advertised 2 cores / 2048 MiB; container CPU quota was 2 CPUs and memory limit 2,147,483,648 bytes. |
| Spark `spark-submit --master spark://spark-master:7077 ...` with a temporary DataFrame job | Passed; result was `[(12, 'beans'), (11, 'maize')]`. The temporary script was removed. |
| Grafana `/api/health`, authenticated `/api/org`, and container `nc` to `postgres:5432` | Passed; Grafana database healthy, API authenticated, and PostgreSQL TCP reachable. Organization persisted after Grafana restart. |
| Kafka `kafka-broker-api-versions.sh`, temporary topic producer/consumer, broker restart, and host `nc` to 9094 | Passed; message content was verified before and after restart, temporary topic deleted, external TCP listener reachable. |
| `uv sync --locked --group dev`, Ruff check/format, and `uv run pytest` | Passed; one unit test passed. |
| Ruby YAML parsing for `.github/workflows/*.yml`, `bash -n scripts/airflow-init.sh`, and `git diff --check` | Passed. |
| `actionlint` | Not run; unavailable in the environment. No GitHub workflow was triggered. |
| Dockerfile image build | Not applicable; no project Dockerfiles exist, and no placeholder image build was claimed. |

## E. Remaining risks and incomplete checks

- The PostgreSQL bootstrap SQL was applied and verified against the existing
  persistent volume, including role creation and ownership transfer. A brand-new
  empty PostgreSQL volume was not created solely for testing, so the complete
  first-initialization path was not independently exercised from a clean volume.
- Airflow has no DAGs; Grafana has no provisioned PostgreSQL datasource or
  dashboards; the warehouse has no schema or migrations. The Grafana-to-Postgres
  check was TCP connectivity, not a datasource query.
- Kafka is a single-node, plaintext local development broker. Its host listener
  is loopback-only, but it is not suitable for an untrusted network or
  production-grade availability.
- PostgreSQL still has a local bootstrap superuser. Application roles are
  isolated, but the bootstrap secret must remain protected.
- Spark Worker resource limits and the full stack were tested on this Docker
  Desktop allocation; lower-memory hosts may need services started selectively.
- GitHub Actions were syntax parsed, not executed on GitHub; actionlint is
  unavailable. The Docker-build workflow correctly finds no Dockerfiles.
- No source connector, ETL, streaming producer/consumer, or business dashboard
  was implemented or validated.

## F. Repository and Docker state

- Branch: `chore/initial-scaffold`.
- No commit or push was created by this audit.
- `.env` remains ignored and untracked; only missing role settings were appended.
- The complete project Compose stack is running. The `airflow-init` container is
  exited with code 0 by design; all long-running services report healthy.
- No volumes were deleted, and no unrelated project containers were modified.

## G. Recommended next actions

The local infrastructure is ready for a pull-request review as a development
foundation, subject to team review of the PostgreSQL upgrade SQL. Before merge,
consider installing `actionlint` in CI or running it locally. Before production
use, test a fresh-volume bootstrap in an isolated disposable environment, add
verified source-specific pipelines, and document backup/restore drills.
