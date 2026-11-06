# Development

| Command | Description |
| ----------------------- | ----------------------------------------------- |
| `make install` | Install production dependencies |
| `make install-dev` | Install all dependencies including dev tools |
| `make update` | Upgrade dependencies and regenerate `uv.lock` |
| `make lock` | Regenerate `uv.lock` |
| `make run` | Run the broker locally |
| `make build` | Rebuild the Docker image (`--no-cache`) |
| `make dev` | Start Docker stack with hot-reload (`compose watch`) |
| `make start` | Start Docker stack detached |
| `make stop` | Stop Docker stack |
| `make logs` | Tail broker container logs (last 500 lines) |
| `make logging` | Follow broker container logs live |
| `make status` | Containers, then a read-only check of every link: PostgreSQL, NATS, JetStream, the API, the engine and the workers — see below |
| `make tailscale-status` | Tailnet status and Serve forwards of the `tailscale` container (when enabled) |
| `make simulate-nats` | Replay an example NATS payload against the running stack |
| `make format` | Format code with Ruff |
| `make lint` | Run Ruff check |
| `make check` | Alias for `make lint` |
| `make fix` | Format and auto-fix linting issues |

## `make status`

Prints the stack's containers (and the tailnet peers, with Tailscale on), then
runs `python -m broker.status` inside the broker container and exits non-zero
if one of the stack's own services is down:

```text
Broker status
  [ OK ] PostgreSQL    postgres:5432/algo_trading_broker, 2 account(s)
  [ OK ] NATS          nats:4222
  [ OK ] JetStream     SIGNALS holds 0 undelivered signal(s); broker_signal_handler is pulling
  [ OK ] Broker API    GET 127.0.0.1:80/v1/health answered 200
  [ OK ] Engine (QTE)  connected as qte-broker-sink from 172.18.0.3
  [WARN] Workers       1 connected, 1 known account(s) offline
                       online   FOREX-MT5-123456  from 172.18.0.3  strategies: MT5_GOLD_M5_V1
                       offline  CRYPTO-BINANCE-7654321  last activity 2026-10-11 21:04 UTC
```

Neither the engine nor a worker has a ping in its contract, so nothing is sent
to them. They are read off the NATS server's own client list (`/connz` on the
monitoring port): QTE names its connection `qte-broker-sink`, and a worker is
the client subscribed to its private `ADMIN.<market>.<gateway>.<account_id>`
subject. Connected workers are then set against the `accounts` table, so an
account whose worker dropped off is listed as offline. A client that is
neither shows under **Other clients**. With Tailscale on, every remote client
arrives through the `tailscale` container and carries its address.

A missing engine or worker is a warning, not a failure: a deployment fed by
TradingView alone has no engine, and an account may be offline on purpose.
Every probe is read-only.

## Database (Alembic)

| Command | Description |
| ----------------------------- | ------------------------------------------- |
| `make db-upgrade` | Apply all pending migrations (`upgrade head`) |
| `make db-downgrade` | Roll back one migration step |
| `make db-history` | Show full migration history |
| `make db-current` | Show current revision in the database |
| `make db-revision m='msg'` | Generate a new auto-migration file |

---

## Testing

### Unit tests (pytest)

```bash
uv run pytest
```

The suite (`tests/`) covers the signal helper, the signal-processing service, and the trade-status policy. `pytest-asyncio` runs in `auto` mode, so async tests need no extra decorators.

The bot is a **separate uv project** with its own suite — run it from `bot/`:

```bash
cd bot && uv run pytest
```

### Manual API testing (Bruno)

Open the `bruno/` directory with the [Bruno API Client](https://www.usebruno.com/) to find pre-configured requests for the webhook, accounts, trades, settings, and health endpoints.

The `examples/` directory also holds sample JSON payloads for webhook, NATS, and worker (`TRADE` event) messages.
