# Telegram Bot

Telegram bot for **algo-trading-broker**, built with
[aiogram v3](https://docs.aiogram.dev/). One bot process serves **two roles** —
endusers and admins — and is a **thin HTTP client**: it talks only to the
broker's HTTP API and never touches the database or NATS directly.

## Roles

| Role | Who | Auth |
| ---- | --- | ---- |
| **Enduser** | Anyone who links an account | Sends one of the account's link tokens (UUID) via `/start`; the bot records their Telegram id against that account. |
| **Admin** | Telegram IDs in `TELEGRAM_ADMIN_IDS` | Router-level `IsAdmin` filter. Admins don't need a linked account. |

Menus are role-aware via Telegram command **scopes**, re-applied on every
startup (`setup_bot_commands`): endusers get the default menu, each admin id
gets an extended chat-scoped menu.

> An admin must `/start` the bot once before Telegram will accept a chat-scoped
> menu for them ("chat not found" is caught and logged; the menu applies on the
> next startup after their first message).

### Enduser commands

`/start` (link), `/trades`, `/flat`, `/prevent`, `/allow`, `/status`,
`/myaccounts`, `/link`, `/switch`, `/unlink`, `/help`.

`FLAT`/`PREVENT`/`ALLOW` each require a confirmation tap.

One linked account is **active** at a time; `/status`, `/trades`, `/flat`,
`/prevent`, `/allow` and `/unlink` all act on it. `/myaccounts` lists the linked
accounts, `/link` adds one, and `/switch` lists them with a button per account
to change the active one.

`/subscribe` opts you in to a DM whenever one of your linked accounts **completes
(closes) a trade**; `/unsubscribe` turns it off. The DM is sent by this same bot,
so it lands in your existing chat. This is a per-user preference spanning every
account you hold.

> ⚠️ `PREVENT`/`ALLOW` publish a `BLOCK_ENTRIES`/`ALLOW_ENTRIES` admin command
> over NATS (via the broker). The **worker** must be updated to honor it —
> worker code lives outside this repo.

### Admin commands

Admin commands are prefixed `admin_` so they group under a divider (`/admin_help`,
a header row that also lists them) below the user commands in the menu. Telegram
command names may only contain `[a-z0-9_]`, so the prefix uses an underscore (a
literal `/admin-…` dash or a bare `-----` divider isn't a valid command name).
The handlers also still accept the old un-prefixed names (`/accounts`, `/rotate`,
…) for backward compatibility; only the prefixed form is shown in the menu.

| Command | Action | Broker endpoint |
| ------- | ------ | --------------- |
| `/admin_help` | List the admin commands (also the menu divider) | — |
| `/admin_accounts` | List accounts + linked-user count + link token (spoiler) | `GET /v1/accounts` |
| `/admin_newaccount` | Register an account (pick market → gateway → type id) | `POST /admin/accounts` |
| `/admin_trades [account_id]` | Trades of any account (picker if no arg) | `GET /v1/{account_id}/trades` |
| `/admin_flat [account_id]` | FLAT everything, or one account (confirm) | `POST /admin/flat` |
| `/admin_rotate [account_id]` | Rotate a link token — revokes old **and unlinks every linked user** (confirm) | `POST /admin/accounts/{id}/link-token/rotate` |
| `/admin_linkaccount` | Bind a Telegram user to an account directly (pick account → type user id) | `POST /admin/accounts/{uuid}/link-telegram` |
| `/admin_uuid [account_id]` | Show accounts' internal row UUIDs | `GET /v1/accounts` |
| `/admin_settings` | View + toggle block/silent/include-raw | `GET` + `POST /admin/settings/*` |

### Link-token semantics

A link token is a bearer secret: whoever sends it gains access to the account.
Linking is **additive** — several people can hold the same account (each keeps
their own active-account selection), and one person can hold several accounts.
A token stays reusable after a successful link.

`/admin_rotate` issues a fresh token and revokes every token that was still
valid, so the old secret stops working immediately. It is now a **full access
reset**: it also unlinks every Telegram user currently bound to the account and
clears any active-session pointer at it, so the new token is the only way back
in. To remove a specific person without rotating, they `/unlink` (or delete
their `account_bot_links` row).

`/admin_linkaccount` binds a Telegram user to an account **without** a token
(the admin already knows which account row to bind). It addresses the account by
its row UUID (`accounts.id`) so the target is unambiguous even when a bare
`account_id` is reused across gateways. Linking stays additive and idempotent.

## Rendering

Every list command replies with a monospace table (a Telegram `<pre>` block)
built by `render_table` in `app/utils/table.py` — `/myaccounts`, `/switch`,
`/trades`, `/atrades`:

```text
📊 Trades (1–3 / 20) · times in UTC+7

SYMBOL   ACTION  STATUS       PRICE    QTY    BALANCE  TIME
──────────────────────────────────────────────────────────────────
XAUUSD   LONG    OPEN      2,345.68   1.00  10,102.50  01-01 07:00
BTCUSDT  SHORT   PARTIAL  65,000.00   0.50  10,250.75  01-02 20:45
EURUSD   LONG    CLOSED        1.09  10.00   9,980.00  01-04 13:30
```

`render_table(headers, rows, aligns, max_widths)` sizes each column to its
widest cell, right-aligns where asked (`"r"` — used for the numeric columns),
truncates over-long values with an ellipsis, and HTML-escapes for the caller.
Padding is computed on the visible text *before* escaping, so entities never
skew a column.

Alignment only holds for single-width characters, which rules emoji out of
table cells — they are double-width and vary by platform. So markers inside a
table are text: the active account is `★` (U+2605), and a trade's status is
abbreviated (`OPEN`, `PARTIAL`, `REJECT`, `CLOSED`, `FLAT`) rather than the
colour-coded circle. Inline keyboard buttons are not monospace and keep the
emoji.

**Timezone.** Every displayed timestamp is converted to the broker's
`notification_timezone` setting — the same offset used for broker-sent
notifications — and the zone is always named, once in the table header
(`· times in UTC+7`) rather than on each row. The bot has no DB access, so
`app/utils/timezone.py` takes the offset from
`GET /admin/settings/notification-timezone`; if that call fails it falls back
to UTC+7, the broker's own default.

## Architecture

```
app/
├── __main__.py        # Dispatcher, polling, graceful shutdown
├── commands.py        # USER/ADMIN command lists + scoped setup_bot_commands
├── config.py          # BotSettings (pydantic-settings; admin_ids)
├── constants.py       # markets + gateways valid per market
├── emojis.py          # named emoji constants (no raw glyphs in source)
├── logger.py          # console + daily rolling file
├── states.py          # FSM: LinkAccount, CreateAccount
├── filters/           # is_admin.py — IsAdmin router gate
├── services/          # broker_client.py — httpx client (enduser + admin calls)
├── middlewares/       # deps.py (DI), auth.py (require-linked guard)
├── handlers/          # start, link, trades, commands, account, admin
├── keyboards/         # inline keyboards (confirm, pagination, pickers, settings)
├── presenters/        # render API payloads → Telegram HTML
└── utils/             # table (monospace tables), timezone (local time),
                       # telegram (safe_edit_text), pagination
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
