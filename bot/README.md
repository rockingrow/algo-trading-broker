# Telegram Bot

Telegram bot for **algo-trading-broker**, built with
[aiogram v3](https://docs.aiogram.dev/). One bot process serves **two roles** —
endusers and admins — and is a **thin HTTP client**: it talks only to the
broker's HTTP API and never touches the database or NATS directly.

## Roles

| Role | Who | Auth |
| ---- | --- | ---- |
| **Enduser** | Anyone who links an account | Sends the account's `telegram_link_token` (UUID) via `/start`; the bot binds their Telegram id to that account. |
| **Admin** | Telegram IDs in `TELEGRAM_ADMIN_IDS` | Router-level `IsAdmin` filter. Admins don't need a linked account. |

Menus are role-aware via Telegram command **scopes**, re-applied on every
startup (`setup_bot_commands`): endusers get the default menu, each admin id
gets an extended chat-scoped menu.

> An admin must `/start` the bot once before Telegram will accept a chat-scoped
> menu for them ("chat not found" is caught and logged; the menu applies on the
> next startup after their first message).

### Enduser commands

`/start` (link), `/trades`, `/flat`, `/prevent`, `/allow`, `/status`, `/unlink`, `/help`.

`FLAT`/`PREVENT`/`ALLOW` each require a confirmation tap.

> ⚠️ `PREVENT`/`ALLOW` publish a `BLOCK_ENTRIES`/`ALLOW_ENTRIES` admin command
> over NATS (via the broker). The **worker** must be updated to honor it —
> worker code lives outside this repo.

### Admin commands

| Command | Action | Broker endpoint |
| ------- | ------ | --------------- |
| `/accounts` | List accounts + link status + link token (spoiler) | `GET /v1/accounts` |
| `/newaccount` | Register an account (pick market → gateway → type id) | `POST /admin/accounts` |
| `/atrades [account_id]` | Trades of any account (picker if no arg) | `GET /v1/{account_id}/trades` |
| `/aflat [account_id]` | FLAT everything, or one account (confirm) | `POST /admin/flat` |
| `/rotate [account_id]` | Rotate a link token (revokes old, confirm) | `POST /admin/accounts/{id}/link-token/rotate` |
| `/settings` | View + toggle block/silent/include-raw | `GET` + `POST /admin/settings/*` |

### Link-token semantics

`telegram_link_token` is a bearer secret: whoever sends it claims the account
(re-binding moves it — latest claim wins). To revoke access, an admin uses
`/rotate` to issue a fresh token; the old one stops working immediately.

## Architecture

```
app/
├── __main__.py        # Dispatcher, polling, graceful shutdown
├── commands.py        # USER/ADMIN command lists + scoped setup_bot_commands
├── config.py          # BotSettings (pydantic-settings; admin_ids)
├── helpers.py         # safe_edit_text (ignores "message is not modified")
├── logger.py          # console + daily rolling file
├── states.py          # FSM: LinkAccount.waiting_for_token
├── filters/           # is_admin.py — IsAdmin router gate
├── services/          # broker_client.py — httpx client (enduser + admin calls)
├── middlewares/       # deps.py (DI), auth.py (require-linked guard)
├── handlers/          # start, link, trades, commands, account, admin
├── keyboards/         # inline keyboards (confirm, pagination, pickers, settings)
└── presenters/        # render API payloads → Telegram HTML
```

## Configuration (env / `.env`)

| Variable | Default | Description |
| -------- | ------- | ----------- |
| `BOT_TELEGRAM_TOKEN` | — | Bot token for this bot (separate BotFather bot from the broker's notification bot). |
| `TELEGRAM_ADMIN_IDS` | `""` | Comma-separated admin Telegram IDs (e.g. `123,456`). |
| `BROKER_API_KEY` | — | `X-API-KEY` used to call the broker. |
| `BROKER_API_PREFIX` | `""` | Secret URL segment, if the broker uses one. |
| `BOT_BROKER_BASE_URL` | `http://broker:8080` | Broker base URL (Docker service name). |
| `BOT_LOG_LEVEL` | `DEBUG` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`). |
| `BOT_REQUEST_TIMEOUT` | `10.0` | HTTP timeout (seconds). |
| `BOT_VIEW_TRADES_PER_PAGE` | `50` | Trades per page. |

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

Uses **long-polling** (no inbound port). This bot has its own BotFather token
(`BOT_TELEGRAM_TOKEN`), separate from the broker's send-only notifier
(`TELEGRAM_BOT_TOKEN`), so the two never conflict.
