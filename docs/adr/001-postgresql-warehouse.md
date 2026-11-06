# ADR-001: PostgreSQL analytical warehouse

- **Status:** Accepted for the initial local foundation
- **Decision date:** 2026-11-06

## Context

The project needs a locally operated relational target for future curated
analytical products. The first deployment should be simple to inspect and run,
without introducing a cloud dependency.

## Decision

Use PostgreSQL as the planned warehouse and local metadata store. The Compose
deployment initializes a separate `airflow` metadata database alongside
`food_intelligence` in the same PostgreSQL instance. Airflow uses PostgreSQL,
not SQLite, with LocalExecutor.

## Alternatives

SQLite is unsuitable as the multi-service Airflow metadata backend. A separate
database server increases initial operational burden. Cloud warehouses violate
the local-hosting constraint.

## Consequences

The shared PostgreSQL server is simple for local development, while distinct
databases isolate metadata and analytics. They still share host resources,
failure domain, and backups; production sizing, roles, migrations, retention,
and restore procedures require further validation. No warehouse schema or
migrations are implemented yet.
