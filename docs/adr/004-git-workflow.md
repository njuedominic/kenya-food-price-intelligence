# ADR-004: GitHub Flow and pull requests

- **Status:** Accepted
- **Decision date:** 2026-11-06

## Context

The repository needs a low-friction contribution process that keeps the stable
branch reviewable and continuously checked.

## Decision

Use GitHub Flow: scoped short-lived branches, pull requests to `main`, automated
CI, review, and squash merges. Use Conventional Commit prefixes.

## Alternatives

Long-lived release branches add coordination for this early-stage project.
Direct commits to `main` weaken review and quality gates.

## Consequences

Repository files cannot enforce branch protection. An administrator must enable
rules requiring pull requests, appropriate review, and CI checks. The release
workflow validates manually selected existing tags and builds artifacts; it
does not publish or deploy them.
