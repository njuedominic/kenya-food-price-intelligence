.PHONY: install lint format test compose-config up down logs

install:
	uv sync --locked --group dev

lint:
	uv run ruff check .

format:
	uv run ruff format .

test:
	uv run pytest

compose-config:
	@test -f .env || (echo "Copy .env.example to .env and replace its placeholder values first." >&2; exit 1)
	docker compose --env-file .env config --quiet
	docker compose --env-file .env -f docker-compose.yml -f docker-compose.streaming.yml config --quiet

up:
	@test -f .env || (echo "Copy .env.example to .env and replace its placeholder values first." >&2; exit 1)
	docker compose --env-file .env up -d

down:
	@test -f .env || (echo "Copy .env.example to .env first." >&2; exit 1)
	docker compose --env-file .env down

logs:
	@test -f .env || (echo "Copy .env.example to .env first." >&2; exit 1)
	docker compose --env-file .env logs -f
