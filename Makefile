.PHONY: help install install-dev update lock fix format lint check run gen-zmq-keys

help:
	@echo "Available commands:"
	@echo "  make install     - Install production dependencies"
	@echo "  make install-dev - Install all dependencies including dev"
	@echo "  make update      - Update dependencies and regenerate lock file"
	@echo "  make lock        - Regenerate uv.lock"
	@echo "  make fix         - Run ruff format and check --fix"
	@echo "  make format      - Run ruff format"
	@echo "  make lint        - Run ruff check"
	@echo "  make check       - Alias for lint"
	@echo "  make run         - Run the broker locally"
	@echo "  make dev         - Run docker compose locally with hot module reload on code change"
	@echo "  make simulate-zmq - Run ZMQ signal simulator (E2E)"
	@echo "  make gen-zmq-keys - Generate ZMQ CURVE keypair using Docker"

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

dev:
	docker compose up --build -d
	docker compose watch

start:
	docker compose up --build -d

stop:
	docker compose down

logs:
	docker logs algo_trading_broker --tail 500

logs-follow:
	docker logs algo_trading_broker --follow

simulate-zmq:
	uv run python e2e/simulate_signals.py

simulate-security:
	uv run python e2e/simulate_security.py

gen-zmq-keys:
	docker compose run --rm -v ./scripts:/app/scripts broker uv run python scripts/generate_curve_keypair.py