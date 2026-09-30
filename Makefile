.PHONY: help install install-dev update lock fix format lint check run simulate-nats \
        tailscale-status db-upgrade db-downgrade db-history db-current db-revision

# Tailscale is opt-in: .env sets TAILSCALE_ENABLED=true (default false). That
# one switch decides both halves of the setup, so .env never has to spell them
# out:
#   - the `tailscale` container (docker-compose.yml, profile `tailscale`) is
#     started, joining the tailnet and forwarding NATS/Postgres onto it;
#   - the NATS and Postgres host ports are published on 127.0.0.1 only, so
#     another machine can reach them through the tailnet and nothing else.
# With it off, the container is never pulled and those ports bind every
# interface (0.0.0.0), as before Tailscale existed.
TAILSCALE_ENABLED := $(shell sed -n 's/^TAILSCALE_ENABLED=//p' .env 2>/dev/null | tail -n1 | sed 's/[[:space:]]*\#.*//' | tr -d '\r"' | tr -d "' " | tr '[:upper:]' '[:lower:]')
TAILSCALE_ACTIVE := $(if $(filter true 1 yes on,$(TAILSCALE_ENABLED)),1,)

COMPOSE_PROFILE := $(if $(TAILSCALE_ACTIVE),--profile tailscale,)

# Passed explicitly so the switch is authoritative over whatever the shell
# happens to export.
PRIVATE_BIND_ADDRESS := $(if $(TAILSCALE_ACTIVE),127.0.0.1,0.0.0.0)
export PRIVATE_BIND_ADDRESS

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
	@echo "  make tailscale-status - Show the tailscale container's tailnet status, Serve forwards and login URL"
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

# The image's containerboot puts tailscaled's socket at /tmp/tailscaled.sock,
# not where the CLI looks by default. A node that has not logged in yet fails
# `status`; its container log then carries the login URL.
TS_CLI = docker compose $(COMPOSE_PROFILE) exec tailscale tailscale --socket=/tmp/tailscaled.sock

tailscale-status:
ifeq ($(TAILSCALE_ACTIVE),1)
	@$(TS_CLI) status || docker logs algo_trading_tailscale --tail 30
	@$(TS_CLI) serve status
else
	@echo "Tailscale is disabled (TAILSCALE_ENABLED is not true in .env) — nothing to show."
endif

dev:
	docker compose $(COMPOSE_PROFILE) up --build -d
	docker compose watch

start:
	docker compose $(COMPOSE_PROFILE) up --build -d

stop:
	docker compose --profile tailscale down

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
