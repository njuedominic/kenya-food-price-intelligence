# Contributing

## Workflow

1. Start from an up-to-date `main` and create a scoped branch:
   `git switch main`, `git pull --ff-only`, then
   `git switch -c feature/short-description` (or `fix/`, `docs/`, `chore/`,
   `test/`).
2. Keep changes focused. Use Conventional Commit subjects such as
   `feat: add verified source profiling` or `docs: clarify local setup`.
3. Install development tools with `uv sync --locked --group dev`. Run
   `uv run ruff check .`, `uv run ruff format --check .`, and `uv run pytest`.
   If you change Compose files, also run `make compose-config`.
4. Push your branch and open a pull request against `main`. Explain the
   motivation, implementation, tests, and any source/license assumptions. Do
   not include credentials or generated datasets.
5. Address review feedback and wait for required checks and approvals. Resolve
   conflicts by fetching `main`, merging or rebasing it onto your feature branch,
   resolving files deliberately, then rerunning checks. Do not force-push shared
   branches.
6. Squash merge the approved pull request. Delete the feature branch only through
   the normal pull-request workflow after merge.

Keep `main` stable: all changes go through pull requests; do not commit directly
to `main`. Configure branch protection/rulesets in GitHub to require pull
requests, reviews, and the CI checks. Those settings cannot be enforced by this
repository's files.

## Engineering and data practices

- Follow the `src/` package layout, type-hint public Python interfaces, and
  reuse standard logging.
- Add focused tests for behavior. Do not implement connectors before documenting
  verified source access, licensing, schema, and update frequency.
- Preserve source metadata and clearly distinguish official, derived, and
  synthetic data. Never invent observations or commit secrets.
- Update relevant runbooks, architecture decisions, and configuration examples
  when behavior changes.

See the README and `docs/runbooks/local-setup.md` for local environment commands.
