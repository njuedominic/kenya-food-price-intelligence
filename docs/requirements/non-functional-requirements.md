# Non-functional requirements

- **Local-first:** application services and state are hosted on the developer's
  machine using Docker Compose. Public data endpoints are external inputs only.
- **Reproducible:** Python dependencies are lockfile-managed; service image tags
  are pinned; CI uses pinned GitHub Actions references.
- **Maintainable:** Python code uses the `src/` layout, type hints, Ruff, pytest,
  modular boundaries, and focused documentation.
- **Recoverable:** databases, Airflow logs, Spark checkpoints, Kafka data, and
  Grafana state use persistent named volumes where applicable.
- **Auditable:** preserve source identity, observation and ingestion times,
  schema version, run identity, and record hash; do not silently discard bad data.
- **Secure by default:** no tracked credentials, anonymous Grafana access, or
  cloud-managed resources; use local secrets and least-privilege CI permissions.
- **Resource-aware:** the complete local stack is optional and can exceed modest
  machine memory. Start only needed services and document single-host limits.
- **Testable:** automated CI must fail on package lint, format, import, or test
  failures; Compose configuration is separately validated.
