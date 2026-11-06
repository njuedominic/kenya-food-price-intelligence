# Deployment architecture

Docker Compose runs on one local host. The base Compose file defines PostgreSQL,
Airflow with LocalExecutor, Spark standalone master/worker, and Grafana. A
separate Compose file and `streaming` profile add a single-node Kafka KRaft
broker. Named volumes persist database, Airflow log, Spark checkpoint, Grafana,
and Kafka state. The repository's local `data/` directory is mounted at the
same `/opt/platform/data` path in Airflow and Spark.

PostgreSQL contains a `food_intelligence` database and a separately initialized
Airflow metadata database. PostgreSQL is not exposed with its default port:
local access defaults to host port 5433. Airflow UI, Spark master UI, and Grafana
are exposed only on their documented local ports. Do not bind these development
services to an untrusted network.

The topology is single-host and has no high availability. Docker resource limits,
disk capacity, backups, image upgrades, and secret rotation remain operator
responsibilities. Compose configuration validation is not a runtime integration
test.
