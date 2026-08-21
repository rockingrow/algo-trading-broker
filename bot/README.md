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

Menus are role-aware **and** link-aware, via Telegram command **scopes**
(`app/services/menu.py`):

| Who | Menu |
| --- | ---- |
| Not linked | `/start` — nothing else |
| Linked | The full enduser menu |
| Admin, not linked | `/start` + the `admin_` commands |
| Admin, linked | Enduser menu + the `admin_` commands |

Every user command needs an account behind it, so until one is linked the menu
is trimmed to the command that gets the user somewhere: `/start`. `/help` is
trimmed with the rest — it is a tour of commands they cannot run — and is
refused alongside them (it sits on a router behind `AuthMiddleware`). Admin
commands never needed a linked account, so an unlinked admin loses only the
user half of their menu.

`CommandMenuMiddleware` re-checks the sender's link status on **every** update,
which is what keeps the menu honest after a change the bot never saw in that
chat — an `/admin_rotate` that unlinked the user, an `/admin_linkaccount` that
linked them. Linking and `/unlink` also re-apply the menu on the spot, so it
changes with the same tap instead of on the next message. Two things keep that
cheap: the menu last applied to each chat is remembered in-process (no Telegram
call when nothing changed), and the account resolved for the menu is passed on
to `AuthMiddleware` (no second broker call). A broker that can't be reached
leaves the menu alone — an outage is not evidence that a user unlinked.

On startup the **default** scope — what every chat the bot has never spoken to
falls back to — is set to the `/start`-only menu, and each admin's own menu is
refreshed.

> An admin must `/start` the bot once before Telegram will accept a chat-scoped
> menu for them ("chat not found" is caught and logged; nothing is cached, so
> the next update after their first message applies it).

### Enduser commands

`/start` (link), `/status`, `/trades`, `/flat`, `/prevent`, `/allow`,
`/myaccounts`, `/link`, `/switch`, `/unlink`, `/subscribe`, `/unsubscribe`,
`/help` — all but `/start` require a linked account, and are hidden until
there is one.

`FLAT`/`PREVENT`/`ALLOW` each require a confirmation tap.

`/start` also takes the link code as a deep-link payload (`/start <code>`) and
links the account immediately, skipping the "paste your UUID" step. That is what
an `/admin_invite_url` link produces — see [Invite links](#invite-links).

One linked account is **active** at a time; `/status`, `/trades`, `/flat`,
`/prevent`, `/allow` and `/unlink` all act on it. `/myaccounts` lists the linked
accounts, `/link` adds one, and `/switch` lists them with a button per account
to change the active one.

`/status` also shows an **Open positions** line — the number of trades
currently running (``is_running``) on the active account — and, whenever
that count is above zero, the same trade table `/trades` renders, filtered
to just those open positions. Backed by `GET
/v1/telegram/{telegram_user_id}/positions`.

### Live trade cards

`/subscribe` opts you in to a message the moment one of your linked accounts
**opens a trade**; `/unsubscribe` turns it off. This is a per-user preference
spanning every account you hold.

The message is a **card**, not an alert: it is sent by this same bot into your
existing chat and then **edits itself** as the trade moves — `Opened` →
`Partially closed` (TP1) → `Closed` / `Flatted` / `Rejected` — so you end up with
one message per trade rather than a stream of them. It shows symbol, direction,
status, price, quantity, SL/TP1/TP2, balance and running PnL.

While the trade is still running the card carries two buttons:

| Button | What it does |
| ------- | --------------------------------------- |
| 🔍 **Detail** | Expands the card with strategy, account, market/gateway, leverage, risk, reference id and any worker comment. ⬆️ **Summary** collapses it again. |
| 🛑 **Exit** | Asks to confirm, then closes the trade — a FLAT scoped to that trade's strategy and symbol. The card notes that the exit is in flight and updates itself once the worker reports the close. |

Once the trade reaches a terminal status the card is updated one last time and
the buttons disappear — that final state is your completed-trade notification.
"Terminal" covers a normal TP/SL close, an admin `/admin_flat`, and a worker
rejection. Buttons keep working across an account switch, since they act on the
trade rather than on whichever account is active.

A card that was already posted keeps updating even after you `/unsubscribe`;
the opt-in only decides whether *new* trades get one.

> ⚠️ `PREVENT`/`ALLOW` publish a `BLOCK_SIGNAL`/`ALLOW_SIGNAL` admin command
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
| `/admin_accounts` | Accounts + linked-user count + link token (spoiler), then a second table of row UUIDs | `GET /v1/accounts` |
| `/admin_newaccount` | Register an account (pick market → gateway → type id) | `POST /admin/accounts` |
| `/admin_trades [account_id]` | Trades of any account (picker if no arg) | `GET /v1/{account_id}/trades` |
| `/admin_flat [account_id]` | Bare: walk strategy → market → gateway pickers (each with **All**) then confirm; with `account_id`: one account (confirm) | `GET /admin/strategies`, `POST /admin/flat` |
| `/admin_rotate [account_id]` | Rotate a link token — revokes old **and unlinks every linked user** (confirm) | `POST /admin/accounts/{id}/link-token/rotate` |
| `/admin_linkaccount` | Bind a Telegram user to an account directly (pick account → type user id) | `POST /admin/accounts/{uuid}/link-telegram` |
| `/admin_invite_url [code]` | One-tap invite link for an account (picker if no arg) | `GET /v1/accounts` (picker only) |
| `/admin_settings` | View + toggle block/silent/include-raw | `GET` + `POST /admin/settings/*` |
| `/admin_magicmap [json]` | View + replace the strategy → magic-number map (paste JSON, or pass it inline) | `GET` + `POST /admin/settings/strategy-magic-map` |
| `/admin_public_chats` | View + replace the chats the **public** signal broadcast goes to (comma-separated; `-` turns it off) | `GET` + `POST /admin/settings/public-broadcast-chat-ids` |

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

### Invite links

`/admin_invite_url` packages a link token as a Telegram deep link:

```text
https://t.me/<bot_username>?start=b5dc037496394861acf42d239aa5c1b4
```

Opening it starts the bot with the token already in the `/start` payload, so the
account is linked on the first tap — the end user never sees or types a UUID.
Called with a code it just wraps that code; called bare it lists the accounts and
takes the token from the one picked (the button carries the account's row UUID,
not the token, so a bearer secret never rides in `callback_data`).

The payload is the token in **bare hex** (32 chars, no dashes) purely to keep the
URL short — Telegram's payload alphabet is `[A-Za-z0-9_-]` up to 64 chars, so the
dashed form would be legal too. `/start` and the manual prompt both accept either
form and normalise before calling the broker.

An invite URL is **exactly as sensitive as the token inside it** — same access,
no expiry, still additive, and revoked by the same `/admin_rotate`. It buys
convenience, not a second security model, so share it as privately as the token.

## Rendering

Every list command replies with a monospace table (a Telegram `<pre>` block)
built by `render_table` in `app/utils/table.py` — `/myaccounts`, `/switch`,
`/trades`, `/atrades`, and `/status` (when there are open positions to show):

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
├── commands.py        # START-only/USER/ADMIN command lists + menu_for()
├── config.py          # BotSettings (pydantic-settings; admin_ids)
├── constants.py       # markets + gateways valid per market
├── emojis.py          # named emoji constants (no raw glyphs in source)
├── logger.py          # console + daily rolling file
├── states.py          # FSM: LinkAccount, CreateAccount
├── filters/           # is_admin.py — IsAdmin router gate
├── services/          # broker_client.py — httpx client (enduser + admin calls)
│                      # menu.py — applies the link-aware command menu
├── middlewares/       # deps.py (DI), auth.py (require-linked guard),
│                      # menu.py (re-check link status on every update)
├── handlers/          # start, link, trades, commands, account, admin
├── keyboards/         # inline keyboards (confirm, pagination, pickers, settings)
├── presenters/        # render API payloads → Telegram HTML
└── utils/             # table (monospace tables), timezone (local time),
                       # telegram (safe_edit_text), pagination,
                       # invite (link code ↔ /start deep-link payload)
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

Page sizes are not env vars. Every command that renders a table paginates, and
how many rows fit follows from how wide that table is — so they live as code
constants (`TRADES_PER_PAGE`, `ACCOUNTS_PER_PAGE`, `ADMIN_ACCOUNTS_PER_PAGE`)
in [`app/constants.py`](app/constants.py).

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
