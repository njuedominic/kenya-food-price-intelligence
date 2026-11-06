# Functional requirements

## Milestone 1 scope

- Provide a versioned Python 3.12 package installable from the `src/` layout.
- Provide repeatable development commands, static checks, and an automated unit
  test in GitHub Actions.
- Describe the local service boundaries for PostgreSQL, Airflow, Spark, Grafana,
  and optional Kafka, with persistent state and no cloud hosting dependencies.
- Define source metadata and governance expectations before connectors are built.
- Provide architecture, setup, recovery, contribution, and deployment guidance.

## Later milestones

- Profile and verify each proposed KNBS, World Bank, FAOSTAT, and county data
  source, including access method, license, schema, geography, and cadence.
- Implement only verified and licensed batch connectors and source contracts.
- Add tested Bronze/Silver/Gold processing and a documented analytical model.
- Add explicitly labeled simulated events and separately deployed streaming jobs.
- Add data quality reports and dashboards backed by verified metrics.

No future requirement implies that an API, observation, or dataset is currently
available. Connector development requires recorded source evidence first.
