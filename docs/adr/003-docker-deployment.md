# ADR-003: Docker Compose local deployment

- **Status:** Accepted for the initial local foundation
- **Decision date:** 2026-11-06

## Context

The platform services need reproducible local configuration while remaining
self-hosted.

## Decision

Use pinned service image tags and Docker Compose. Isolate Kafka in a second
Compose file and a `streaming` profile. Persist service state in named volumes,
and use environment variables for local credentials.

## Alternatives

Cloud-managed infrastructure violates the deployment constraint. Manual host
installation makes service versions and dependencies harder to reproduce.

## Consequences

All services share a single-host availability and resource boundary. Operators
must provision memory and disk, protect exposed ports, rotate local secrets, and
back up volumes. Compose validation checks configuration only; actual image
compatibility and startup must be verified on supported hosts.
