# ADR-005: Separate batch and streaming foundations

- **Status:** Accepted for the initial architecture
- **Decision date:** 2026-11-06

## Context

Historical public statistics and future simulated market events have different
cadence, reliability, replay, and trust requirements.

## Decision

Use Airflow as the future batch orchestrator and Spark as the future batch
processing engine. Keep Kafka opt-in in the streaming Compose file. Future
producers and Spark Structured Streaming consumers must declare schemas,
checkpoints, replay behavior, and deduplication keys. Mark simulated events at
creation and keep them distinct from official observations.

## Alternatives

Routing periodic public data through Kafka adds needless operational complexity.
Combining synthetic event records with official source observations obscures
provenance and trust.

## Consequences

The optional broker is a single-node local development service with persistent
KRaft storage, not a resilient Kafka cluster. No producers, consumers,
Structured Streaming jobs, Airflow DAGs, or replay contracts are implemented.
