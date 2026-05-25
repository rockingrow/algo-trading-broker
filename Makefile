.PHONY: help install install-dev update lock fix format lint check run simulate-nats \
        db-upgrade db-downgrade db-history db-current db-revision

help:
	@echo "Available commands:"
	@echo "  make install       - Install production dependencies"
	@echo "  make install-dev   - Install all dependencies including dev"
	@echo "  make update        - Update dependencies and regenerate lock file"
	@echo "  make lock          - Regenerate uv.lock"
	@echo "  make fix           - Run ruff format and check --fix"
	@echo "  make format        - Run ruff format"
	@echo "  make lint          - Run ruff check"
	@echo "  make check         - Alias for lint"
	@echo "  make run           - Run the broker locally"
	@echo "  make dev           - Run docker compose locally with hot module reload on code change"
	@echo "  make simulate-nats - Run NATS signal simulator (E2E)"
	@echo ""
	@echo "Database (Alembic — runs inside broker container, requires stack up):"
	@echo "  make db-upgrade          - Apply all pending migrations"
	@echo "  make db-downgrade        - Roll back one migration step"
	@echo "  make db-history          - Show migration history"
	@echo "  make db-current          - Show current revision"
	@echo "  make db-revision m='msg' - Create a new blank migration file"

install:
	uv sync --no-dev

install-dev:
	uv sync

update:
	uv lock --upgrade
	uv sync

lock:
	uv lock

fix:
	uv run ruff format .
	uv run ruff check --fix .

format:
	uv run ruff format .

lint:
	uv run ruff check .

check: lint

run:
	uv run python -m broker.main

build:
	docker compose build --no-cache

dev:
	docker compose up --build -d
	docker compose watch

start:
	docker compose up --build -d

stop:
	docker compose down

logs:
	docker logs algo_trading_broker --tail 500

logging:
	docker logs algo_trading_broker --follow

simulate-nats:
	uv run python e2e/simulate_signals.py

# ── Alembic ──────────────────────────────────────────────────────────────────
ALEMBIC = docker compose exec broker alembic

db-upgrade:
	$(ALEMBIC) upgrade head

db-downgrade:
	$(ALEMBIC) downgrade -1

db-history:
	$(ALEMBIC) history --verbose

db-current:
	$(ALEMBIC) current

db-revision:
	$(ALEMBIC) revision --autogenerate -m "$(m)"
