# Milestone 1 acceptance criteria

| Criterion | Status | Evidence |
|---|---|---|
| Existing repository retained and scaffolding added in a feature branch | Complete | `chore/initial-scaffold` |
| Python 3.12 `src/` package, Ruff and pytest configuration | Complete | `pyproject.toml`, `tests/unit/` |
| Locked dependencies and importable package | Complete | `uv sync --locked`, import check |
| Local Compose configuration with PostgreSQL metadata and warehouse databases | Complete: syntax/config only | `docker compose config` |
| Isolated persistent Kafka configuration | Complete: syntax/config only | Streaming `docker compose config` |
| CI, Docker build, and manual release workflows | Complete: YAML parsed; actionlint unavailable | `.github/workflows/` |
| Requirements, architecture, ADRs, runbooks, and governance documentation | Complete | `docs/` |
| No credentials or generated datasets tracked | Complete: `.env` absent and ignored | `.gitignore`, `.env.example` |
| No cloud infrastructure dependencies | Complete | Compose and architecture docs |

Compose syntax validation does not prove that images start or integrations work.
Docker runtime verification is pending because Docker Desktop is paused. Workflow
YAML linting is also pending because `actionlint` is unavailable in the local
environment.
