# Deployment architecture

Docker Compose runs on one local host. The base Compose file defines PostgreSQL,
Airflow with LocalExecutor, Spark standalone master/worker, and Grafana. A
separate Compose file adds an opt-in single-node Kafka KRaft broker; the base
Compose file does not include Kafka. Named volumes persist database, Airflow
log, Spark checkpoint, Grafana, and Kafka state. The repository's local `data/`
directory is mounted at the same `/opt/platform/data` path in Airflow and Spark.

PostgreSQL contains `food_intelligence` and `airflow` databases with distinct
owners and non-superuser login roles. Published ports bind to loopback only:
PostgreSQL 5433, Airflow 8081, Spark master UI 8082, Grafana 3000, and Kafka's
external listener 9094. Kafka's internal broker port 9092 and controller port
9093 are not published.

The topology is single-host and has no high availability. Docker resource limits,
disk capacity, backups, image upgrades, and secret rotation remain operator
responsibilities. Compose configuration validation is not a runtime integration
test.
