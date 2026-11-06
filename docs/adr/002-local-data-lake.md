# ADR-002: Local filesystem data lake

- **Status:** Accepted for the initial local foundation
- **Decision date:** 2026-11-06

## Context

Raw public observations, transformed datasets, and streaming checkpoints need
persistent storage without cloud object storage.

## Decision

Use the repository's ignored local `data/` directory and Parquet as the planned
columnar storage format. Preserve immutable source responses in Bronze; Silver
holds validated and normalized data; Gold holds documented analytical outputs.
Partitioning must follow verified source volume and query patterns, not assumed
county or time dimensions. Airflow and Spark mount the lake at
`/opt/platform/data`; Spark checkpoints use a named volume.

## Alternatives

S3-compatible and managed object stores conflict with the all-local requirement.
Keeping all records only in PostgreSQL does not provide the intended lake
separation.

## Consequences

Local disk capacity, permissions, and backups are operator concerns. Named
volumes and ignored datasets are not included in Git. A local filesystem copy is
not a backup; recovery requires tested independent copies, consistent database
snapshots, checkpoint awareness, and documented restore ordering. No Parquet
pipeline is implemented yet.
