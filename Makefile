.PHONY: help install install-dev update lock fix format lint check run simulate-nats \
        check-tailscale db-upgrade db-downgrade db-history db-current db-revision

# Tailscale is opt-in: it takes effect only when .env sets both
# TAILSCALE_ENABLED=true (default false) and a non-empty TAILSCALE_IP.
TAILSCALE_ENABLED := $(shell grep -E '^TAILSCALE_ENABLED=' .env 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d '\r' | tr '[:upper:]' '[:lower:]')
TAILSCALE_IP := $(shell grep -E '^TAILSCALE_IP=' .env 2>/dev/null | tail -n1 | cut -d= -f2- | tr -d '\r')
TAILSCALE_ACTIVE := $(if $(and $(filter true 1 yes on,$(TAILSCALE_ENABLED)),$(TAILSCALE_IP)),1,)

# Enables the `tailscale-check` container (docker-compose.yml, profile
# `tailscale`) as part of `docker compose up` when Tailscale is active —
# hosts with the feature off never pull/run that container.
COMPOSE_PROFILE := $(if $(TAILSCALE_ACTIVE),--profile tailscale,)

# The address docker-compose binds the NATS ports to. Passing it explicitly is
# what makes the switch authoritative: with Tailscale off, the ports bind every
# interface even if TAILSCALE_IP is still filled in from an earlier setup.
NATS_BIND_ADDRESS := $(if $(TAILSCALE_ACTIVE),$(TAILSCALE_IP),0.0.0.0)
export NATS_BIND_ADDRESS

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
	@echo "  make check-tailscale - Manually warn if TAILSCALE_IP is set in .env but Tailscale is down/mismatched"
	@echo "                       (make dev/start already run this check automatically as part of the stack)"
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

check-tailscale:
	@bash scripts/check-tailscale.sh

dev:
	docker compose $(COMPOSE_PROFILE) up --build -d
	docker compose watch

start:
	docker compose $(COMPOSE_PROFILE) up --build -d

stop:
	docker compose down

logs:
	docker logs algo_trading_broker --tail 500

logging:
	docker logs algo_trading_broker --follow

simulate-nats:
	uv run python e2e/simulate_signals.py

# ── Alembic ──────────────────────────────────────────────────────────────────
ALEMBIC = docker compose exec broker uv run alembic

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
