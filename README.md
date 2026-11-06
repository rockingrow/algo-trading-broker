# Algo Trading Broker

A **trading signal broker** built with FastAPI and NATS. It sits between
**signal producers** and the execution workers (MT5 / Binance VPS nodes):
every signal is buffered on JetStream, persisted to PostgreSQL and fanned out
on a per-strategy NATS subject; workers report their trades back on `TRADE`,
and Telegram broadcasts and trade cards ride on the same events.

Two producers feed it today, through **one payload schema and one pipeline**:

- **TradingView** — Pine strategies posting alert JSON to `POST /secret/webhook`.
- **[Quant-Trading-Engine (QTE)](https://github.com/rockingrow/quant-trading-engine)** —
  the same `WebhookPayload`, sent to the same webhook or published straight onto
  the JetStream buffer.

```text
TradingView / QTE ──► /secret/webhook ──► JetStream SIGNALS ──► SignalWorker ──► NATS {strategy} ──► workers
                                                                     │                                 │
                                                                 PostgreSQL ◄──── TradeEventConsumer ◄─ TRADE
```

## Features

- **Fast webhook** — verifies the token / optional HMAC, enqueues onto JetStream
  and answers `202`; persistence and fan-out run in a durable background consumer.
- **Reliable delivery** — `QUEUED` → `PUBLISHED` status tracking, a retry job,
  and a signal replay for workers that reconnect.
- **Per-strategy distribution** — each strategy has its own NATS subject;
  workers subscribe only to what they trade.
- **Worker handshake** — one `WORKER_CONNECTED_ACK` carries the strategy magic
  map, replay signals, per-account settings and crypto config.
- **Trade feedback & account tracking** — trades and accounts are upserted from
  the workers' `TRADE` events.
- **Telegram** — broadcast messages edited in place per trade cycle, owner trade
  cards, error-log forwarding, and an interactive bot ([`bot/`](bot/)).
- **Admin controls** — API-key-protected endpoints to block/silence signals,
  set limits, and publish `FLAT`.

## Quick start

Requires Python 3.13+, [uv](https://docs.astral.sh/uv/) and Docker Compose.

```bash
git clone <repository-url>
cd algo-trading-broker
cp .env.example .env   # fill in values
make install-dev
make dev               # Postgres + NATS + broker, migrations applied, hot reload
```

`make start` runs the same stack without hot reload. `make run` starts only the
broker on the host and expects Postgres and NATS to be reachable and migrated
already (`docker compose up -d postgres nats`, then `make db-upgrade`).

The Telegram bot is an optional, separate service:
`docker compose up -d bot` — see [`bot/README.md`](bot/README.md).

For any non-local deployment, consider locking NATS and Postgres to a private
tailnet: [Securing NATS with Tailscale](docs/tailscale.md).

## Documentation

| Topic | Page |
| --- | --- |
| Architecture, signal producers, project layout | [docs/architecture.md](docs/architecture.md) |
| Configuration (`.env`), Telegram chat ids | [docs/configuration.md](docs/configuration.md) |
| HTTP API — webhook payload, accounts, trades, admin settings | [docs/api.md](docs/api.md) |
| NATS subjects, JetStream pipeline, worker handshake | [docs/nats.md](docs/nats.md) |
| PostgreSQL schema | [docs/database.md](docs/database.md) |
| Telegram bot and broadcast messages | [docs/telegram.md](docs/telegram.md) |
| Securing NATS with Tailscale (optional) | [docs/tailscale.md](docs/tailscale.md) |
| Development, `make status`, migrations, testing | [docs/development.md](docs/development.md) |
| Telegram bot — commands, rendering, setup | [bot/README.md](bot/README.md) |
| Payload samples and NATS subject list | [examples/](examples/) |
| Change history | [changelog.md](changelog.md) |

With the server running, interactive API docs are at `/docs` (Swagger) and
`/redoc`.

## Development

```bash
make help              # every target, one line each
make lint / make format
uv run pytest -q       # broker suite
cd bot && uv run pytest -q
make db-revision m="add x" && make db-upgrade
```

Details: [docs/development.md](docs/development.md).

## License

See [LICENSE](LICENSE).
