# Telegram Bot

Self-service Telegram bot for **algo-trading-broker** end-users. Built with
[aiogram v3](https://docs.aiogram.dev/). The bot is a **thin HTTP client**: it
talks only to the broker's `/v1/telegram/*` API and never touches the database
or NATS directly.

## What it does

1. **Link** — the user sends the UUID (`telegram_link_token`) issued by an admin;
   the bot binds their Telegram id to that account (`/start`).
2. **Trades** — paginated recent trades (`/trades`).
3. **Control** — `FLAT` (close positions) and `PREVENT`/`ALLOW` new entries,
   each behind a confirmation step (`/flat`, `/prevent`, `/allow`).
4. **Account** — `/status`, `/unlink`.

> ⚠️ `PREVENT`/`ALLOW` publish a `BLOCK_ENTRIES`/`ALLOW_ENTRIES` admin command
> over NATS (via the broker). The **worker** must be updated to honor it for it
> to take effect — worker code lives outside this repo.

## Architecture

```
app/
├── __main__.py        # Dispatcher, polling, graceful shutdown
├── config.py          # BotSettings (pydantic-settings, reads .env)
├── logger.py          # console + daily rolling file
├── states.py          # FSM: LinkAccount.waiting_for_token
├── services/          # broker_client.py — httpx client over the Broker API
├── middlewares/       # deps.py (DI), auth.py (require-linked guard)
├── handlers/          # start, link, trades, commands, account
├── keyboards/         # inline keyboards (confirm, pagination)
└── formatters/        # render API payloads → Telegram HTML
```

## Configuration (env / `.env`)

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `TELEGRAM_BOT_TOKEN` | — | Bot token (shared with broker notifications). |
| `BROKER_API_KEY` | — | `X-API-KEY` used to call the broker. |
| `BROKER_API_PREFIX` | `""` | Secret URL segment, if the broker uses one. |
| `BOT_BROKER_BASE_URL` | `http://broker:8080` | Broker base URL (Docker service name). |
| `BOT_LOG_LEVEL` | `INFO` | Log level. |
| `BOT_REQUEST_TIMEOUT` | `10.0` | HTTP timeout (seconds). |
| `BOT_TRADES_PAGE_SIZE` | `5` | Trades per page. |

## Run

```bash
# Whole stack (from repo root)
docker compose up -d postgres nats broker bot

# Local dev (bot only; broker must be reachable)
cd bot
uv sync
uv run python -m app

# Tests
uv run pytest
```

Uses **long-polling** (no inbound port). Only this service polls Telegram, so
sharing `TELEGRAM_BOT_TOKEN` with the broker's send-only notifier is safe.
