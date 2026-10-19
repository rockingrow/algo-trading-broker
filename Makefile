.PHONY: help install install-dev update lock fix format lint check run simulate-nats \
        status tailscale-status tailscale-serve-config db-upgrade db-downgrade db-history db-current db-revision

# Tailscale is opt-in: .env sets TAILSCALE_ENABLED=true (default false). That
# one switch decides both halves of the setup, so .env never has to spell them
# out:
#   - the `tailscale` container (docker-compose.yml, profile `tailscale`) is
#     started, joining the tailnet and forwarding NATS/Postgres onto it;
#   - the NATS and Postgres host ports are published on 127.0.0.1 only, so
#     another machine can reach them through the tailnet and nothing else.
# With it off, the container is never pulled and those ports bind every
# interface (0.0.0.0), as before Tailscale existed.
#
# env-value reads one key out of .env the way docker compose does: the last
# assignment wins, and a trailing comment, CR (files edited on Windows),
# quotes and padding are stripped. .env is not `include`d: a value containing
# a `#` or a space would break make, and these two files need to agree on
# nothing but the key names.
env-value = $(shell sed -n 's/^$(1)=//p' .env 2>/dev/null | tail -n1 | sed 's/[[:space:]]*\#.*//' | tr -d '\r"' | tr -d "' ")

TAILSCALE_ENABLED := $(shell echo '$(call env-value,TAILSCALE_ENABLED)' | tr '[:upper:]' '[:lower:]')
TAILSCALE_ACTIVE := $(if $(filter true 1 yes on,$(TAILSCALE_ENABLED)),1,)

# The node's name on the tailnet. Tailscale Serve addresses its HTTPS handler
# by full DNS name, and tailscaled does not expand environment variables
# inside TS_SERVE_CONFIG, so config/tailscale/serve.json is rendered from
# serve.json.template (target tailscale-serve-config) rather than committed
# with one deployment's name baked in.
TAILSCALE_HOSTNAME := $(call env-value,TAILSCALE_HOSTNAME)
TAILSCALE_DNS_NAME := $(call env-value,TAILSCALE_DNS_NAME)
TAILSCALE_FQDN := $(TAILSCALE_HOSTNAME).$(TAILSCALE_DNS_NAME)

# The port the broker container listens on, and so the port Funnel's handler
# must proxy to. Rendered rather than hardcoded: WEBHOOK_PORT moving while
# serve.json still said 80 would break the webhook with a 502 that looks like
# a broker fault.
WEBHOOK_PORT := $(or $(call env-value,WEBHOOK_PORT),80)

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
	@echo "  make status        - Containers, then every link: Postgres, NATS, JetStream, API, engine, workers"
	@echo "  make tailscale-status - Show the tailscale container's tailnet status, Serve forwards and login URL"
	@echo "  make tailscale-serve-config - Render config/tailscale/serve.json from .env (run by make dev/start)"
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
# `status`; its container log then carries the login URL. MSYS_NO_PATHCONV stops
# Git Bash on Windows from rewriting that path into C:/Users/.../Temp/...; it is
# ignored everywhere else.
TS_CLI = MSYS_NO_PATHCONV=1 docker compose $(COMPOSE_PROFILE) exec tailscale tailscale --socket=/tmp/tailscaled.sock

# serve.json is generated, not tracked: it carries this deployment's own
# tailnet name. tailscaled watches the mounted
# directory, so writing it while the container runs is applied without a
# restart.
#
# Two exposure levels come out of that one file:
#   - tailnet-only: the TCP forwards (4222, 8222, 5432), reachable only by the
#     nodes config/tailscale/policy.hujson grants and never from the internet;
#   - public: Funnel on 443, whose handler is mounted at "/". Funnel ignores
#     tailnet grants, so everything FastAPI serves is a public endpoint behind
#     BROKER_API_PREFIX and, for the webhook, the in-payload token - rotate the
#     two together.
#
# The handler is at "/" and not at the webhook path on purpose. Tailscale Serve
# strips the mount point before proxying, and a mount matches its whole
# subtree; measured against a live tailnet:
#
#   mount "/wang2026/secret/webhook" + request to it -> broker sees "POST /"
#   mount "/probeA"  + request "/probeA/hello"       -> broker sees "GET /hello"
#   mount "/probeA/" + request "/probeA/hello"       -> broker sees "GET /hello"
#   mount "/"        + request "/wang2026/v1/health" -> arrives unchanged
#
# So mounting the webhook's own path makes the broker answer 404 for every
# signal (it receives "/"), and the shallower mount that would leave
# "/secret/webhook" intact is "/wang2026", which forwards /v1 and /admin too.
# Restricting Funnel to one path needs a route the stripped remainder can hit,
# which is an application change - not a serve.json one. Do not re-narrow this
# handler without re-reading that.
tailscale-serve-config:
ifeq ($(TAILSCALE_ACTIVE),1)
	@if [ -z "$(TAILSCALE_HOSTNAME)" ] || [ -z "$(TAILSCALE_DNS_NAME)" ]; then \
		echo "TAILSCALE_ENABLED is true, so .env must also set TAILSCALE_HOSTNAME" >&2; \
		echo "and TAILSCALE_DNS_NAME (the tailnet domain, e.g. rudd-komodo.ts.net," >&2; \
		echo "admin console > DNS). Serve has no FQDN to bind without them." >&2; \
		exit 1; \
	fi
	@sed -e 's/@TAILSCALE_FQDN@/$(TAILSCALE_FQDN)/g' \
		-e 's|@WEBHOOK_PORT@|$(WEBHOOK_PORT)|g' \
		config/tailscale/serve.json.template > config/tailscale/serve.json
	@echo "Rendered config/tailscale/serve.json for $(TAILSCALE_FQDN)"
endif

tailscale-status:
ifeq ($(TAILSCALE_ACTIVE),1)
	@$(TS_CLI) status || docker logs algo_trading_broker_tailscale --tail 30
	@$(TS_CLI) serve status
else
	@echo "Tailscale is disabled (TAILSCALE_ENABLED is not true in .env) — nothing to show."
endif

# Probed from inside the broker container: that is where `nats` and `postgres`
# resolve, and where NATS' monitoring port can be read without publishing it.
# `--profile tailscale` so the table lists the tailnet container whatever the
# switch says today. The probe is read-only (see broker/status.py).
status:
	@docker compose --profile tailscale ps --all
ifeq ($(TAILSCALE_ACTIVE),1)
	@echo
	@$(TS_CLI) status || echo "The tailscale container did not answer - see make tailscale-status."
endif
	@echo
	@if [ -n "$$(docker compose ps --quiet --status running broker 2>/dev/null)" ]; then \
		docker compose exec -T broker python -m broker.status; \
	else \
		echo "The broker container is not running, so nothing can be probed from inside" >&2; \
		echo "the stack. Start it with make start, then run make status again." >&2; \
		exit 1; \
	fi

dev: tailscale-serve-config
	docker compose $(COMPOSE_PROFILE) up --build -d
	docker compose watch

start: tailscale-serve-config
	docker compose $(COMPOSE_PROFILE) up --build -d

stop:
	docker compose --profile tailscale down

logs:
	docker logs algo_trading_broker_broker --tail 500

logging:
	docker logs algo_trading_broker_broker --follow

simulate-nats:
	uv run python e2e/simulate_signals.py

# ── Alembic ──────────────────────────────────────────────────────────────────
# The image puts /app/.venv/bin on PATH, so alembic runs without uv.
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
