# Backup and restore

This is a local single-host foundation, not a backup system. Docker named
volumes are persistent across container recreation but remain vulnerable to host
disk loss. No automated backup jobs are included.

Before any future production-like use, define a schedule and retention policy,
store encrypted backups on a separate local device, and regularly test restores.
Back up PostgreSQL consistently (including both `food_intelligence` and
`airflow` databases), local lake files, Kafka state if required, and Grafana
configuration. Treat Spark checkpoints as processing state: preserve them for
continuation or intentionally rebuild them under a documented replay plan.

A restore should stop writers, restore compatible data and database versions,
verify record counts and metadata, then restart services in dependency order.
Document tested commands and recovery objectives before claiming recoverability.
