# Configuration (`.env`)

Start from [`.env.example`](../.env.example) (`cp .env.example .env`). One file
configures **both** services — the broker reads it directly, and
`docker-compose.yml` passes the same file to the `bot` container.

```env
# ── Server ───────────────────────────────────────────
BROKER_PUBLIC_URL=server_ip_or_domain

# Secret URL prefix — all routes are mounted under /<BROKER_API_PREFIX>/
# e.g. "abc123xyz" → /abc123xyz/v1/..., /abc123xyz/admin/..., etc.
# Leave blank to use the default paths without a prefix.
BROKER_API_PREFIX=

# API key for authenticating requests to the broker API (X-API-KEY header).
# The bot reuses this value to call the broker.
BROKER_API_KEY=api_key

# ── Webhook ──────────────────────────────────────────
WEBHOOK_HOST=0.0.0.0
WEBHOOK_PORT=80

# Seconds an idle keep-alive connection is held open. Must exceed the gap
# between TradingView alerts: TradingView reuses pooled connections and
# uvicorn's 5s default closes them first, so the alert fails with
# "server closed the connection unexpectedly".
WEBHOOK_KEEPALIVE_TIMEOUT=120

# Seconds the webhook may wait for JetStream to ack the enqueue before it
# answers anyway and keeps retrying in the background. Must stay well under
# TradingView's own patience, or the alert dies as
# "request took too long and timed out" — and TradingView never re-sends it.
WEBHOOK_ENQUEUE_TIMEOUT=1.0

# Shared webhook token — every alert (TradingView or QTE) must carry the same
# value in the payload's "token" field. Required: left blank, the webhook
# rejects every alert with 500. Generate one, e.g. `uuidgen`.
WEBHOOK_SECRET=

# ── NATS ─────────────────────────────────────────────
NATS_HOST=localhost        # overridden to "nats" inside Docker
NATS_PORT=4222
NATS_MONITOR_PORT=8222     # HTTP monitoring dashboard (compose only)
NATS_TOKEN=changeme        # shared secret; leave blank = no auth

# ── Tailscale (optional) ─────────────────────────────
# One switch. true = start the `tailscale` container, which joins the tailnet
# as TAILSCALE_HOSTNAME and forwards NATS/Postgres onto it; the host ports
# then bind 127.0.0.1 only. See docs/tailscale.md.
TAILSCALE_ENABLED=false
TAILSCALE_AUTHKEY=         # tskey-auth-…, tagged tag:broker; first login only
TAILSCALE_HOSTNAME=algo-broker
TAILSCALE_DNS_NAME=        # tailnet domain, e.g. rudd-komodo.ts.net; with the
                           # hostname it is the FQDN Serve binds

# ── PostgreSQL ────────────────────────────────────────
POSTGRES_HOST=localhost    # overridden to "postgres" inside Docker
POSTGRES_PORT=5432
POSTGRES_DB=algo_trading_broker
POSTGRES_USER=algo_trading
POSTGRES_PASSWORD=algotrading_broker_db_password

# ── Logging ──────────────────────────────────────────
LOG_LEVEL=INFO

# ── API Docs ─────────────────────────────────────────
# Set to false in production to hide /docs, /redoc and /openapi.json.
DOCS_ENABLED=false

# ── Telegram notifier (broker → chat, send-only) ─────
TELEGRAM_ENABLED=false
TELEGRAM_BOT_TOKEN=
TELEGRAM_BROKER_LOG_CHAT_IDS=          # management chat: broker lifecycle events
TELEGRAM_PRIVATE_BROADCAST_CHAT_IDS=   # private signal-cycle broadcast: strategy, signal id + worker table
# Every chat id (incl. TELEGRAM_LOG_CHAT_ID) accepts a comma-separated list,
# and an entry may address one topic of a group with Topics enabled:
# -1002173777783_924584 (see below).

# Forward log records at ERROR level or above to Telegram.
TELEGRAM_LOG_ERRORS_ENABLED=false
TELEGRAM_LOG_DEDUP_WINDOW=60   # seconds — suppress identical messages
TELEGRAM_HTTP_TIMEOUT=5.0      # seconds to connect to the Bot API
TELEGRAM_HTTP_READ_TIMEOUT=20.0  # seconds to wait for its answer
TELEGRAM_LOG_BOT_TOKEN=        # dedicated log bot (falls back to TELEGRAM_BOT_TOKEN)
TELEGRAM_LOG_CHAT_ID=          # dedicated log chat (falls back to TELEGRAM_BROKER_LOG_CHAT_IDS)

# ── Telegram bot service (interactive, ./bot) ────────
# A *second* BotFather bot, separate from TELEGRAM_BOT_TOKEN above.
# Full reference: bot/README.md → Configuration
BOT_TELEGRAM_TOKEN=
TELEGRAM_ADMIN_IDS=            # comma-separated admin user ids, e.g. 123,456
BOT_BROKER_BASE_URL=http://localhost:80   # → http://broker:80 in Docker
BOT_LOG_LEVEL=INFO
BOT_REQUEST_TIMEOUT=10.0
```

## Telegram chat ids: many chats, and group topics

`TELEGRAM_BROKER_LOG_CHAT_IDS`, `TELEGRAM_PRIVATE_BROADCAST_CHAT_IDS` and `TELEGRAM_LOG_CHAT_ID` are
all parsed the same way — none of them is special — so each can reach several
chats and can address a **topic** inside a group that has the Topics feature
switched on:

```bash
# Two groups + one topic inside a third, all from one setting
TELEGRAM_PRIVATE_BROADCAST_CHAT_IDS="-1001111111111,@public_channel,-1002173777783_924584"
# The same syntax works for the management chat and the error-log chat
TELEGRAM_BROKER_LOG_CHAT_IDS="-1001111111111,-1002173777783_100"
TELEGRAM_LOG_CHAT_ID="-1002173777783_555"
```

| Entry | Delivered to |
| ----- | ------------ |
| `-1001111111111` | The group/channel itself (a group with Topics enabled gets it in **General**) |
| `@public_channel` | Public channel by username |
| `-1002173777783_924584` | Topic `924584` of supergroup `-1002173777783` |

The `_<topic id>` suffix makes the broker add
[`message_thread_id`](https://core.telegram.org/bots/api#sendmessage) to the
`sendMessage` call — the Bot API's identifier for "the target message thread
(topic) of a forum", and the only way a bot can post into a specific topic
rather than General. Both numbers are the ones in the topic's own link:
`t.me/c/2173777783/924584` → `-1002173777783_924584` (the chat id is the link's
first number prefixed with `-100`).

Notes:

- Only a **numeric** chat id may carry a topic suffix — usernames may contain
  underscores themselves (`@my_group_2`), so those are never split.
- The suffix is sent *only* when configured: Telegram answers
  `400 Bad Request: message thread not found` if a `message_thread_id` is
  passed for a chat that has no such topic.
- Each chat gets its own Bot API call, issued concurrently, and one failing
  chat (bot kicked, topic deleted) is logged without stopping the others.
- Whitespace and empty entries are ignored, and a chat listed twice is only
  notified once.
- An empty `TELEGRAM_LOG_CHAT_ID` still falls back to `TELEGRAM_BROKER_LOG_CHAT_IDS`,
  list and topics included.
- Completed-trade owner DMs take their chat id from the database rather than
  from `.env`, but run through the same parsing, so nothing has to special-case
  a plain user id.

---

## Not in `.env`

A few knobs live in [`broker/settings.py`](../broker/settings.py) only, because they
change behaviour rather than deployment topology. Override them via the
environment if you really need to:

| Env var | In-code path | Default | Effect |
| ------- | ------------ | ------- | ------ |
| `WEBHOOK_DEFERRED_ENQUEUE_INTERVAL` | `settings.webhook.DEFERRED_ENQUEUE_INTERVAL` | `2.0` | Gap between background re-enqueue attempts after the webhook deadline expired |
| `WEBHOOK_DEFERRED_ENQUEUE_MAX_ATTEMPTS` | `settings.webhook.DEFERRED_ENQUEUE_MAX_ATTEMPTS` | `15` | Re-enqueue attempts spent on one envelope before it is dropped (logged at `error`) |
| `SIGNAL_MAX_ATTEMPTS` | `settings.signal.MAX_ATTEMPTS` | `3` | Total fan-out attempts before a signal is marked `FAILED` |
| `SIGNAL_RETRY_INTERVAL_SECONDS` | `settings.signal.RETRY_INTERVAL_SECONDS` | `15` | Retry-job tick, and the minimum gap between two attempts on one row |
| `JETSTREAM_SIGNAL_CONSUMER` | `settings.jetstream.SIGNAL_CONSUMER` | `broker_signal_handler` | Durable consumer name on the `SIGNALS` stream |
| `JETSTREAM_FETCH_BATCH` | `settings.jetstream.FETCH_BATCH` | `10` | Envelopes pulled per fetch |
| `JETSTREAM_FETCH_TIMEOUT_SECONDS` | `settings.jetstream.FETCH_TIMEOUT_SECONDS` | `1.0` | Pull-fetch timeout |
| `DEFAULT_NOTIFICATION_TIMEZONE_OFFSET_HOURS` | `settings.notification.DEFAULT_TIMEZONE_OFFSET_HOURS` | `7.0` | Fallback offset when the `notification_timezone` broker setting is unset |

> **Settings layout** — In code, settings are grouped into nested sub-models on
> the `Settings` object (`settings.webhook`, `.broker_api`, `.nats`,
> `.postgres`, `.logging`, `.docs`, `.telegram`, `.notification`, `.signal`,
> `.jetstream`), e.g. `settings.webhook.HOST`. The **env var names are flat and
> unchanged** — each sub-model carries an `env_prefix`, so `WEBHOOK_HOST` still
> populates `settings.webhook.HOST`. The `settings.broker_url` / `nats_url` /
> `postgres_dsn` convenience properties remain on the top-level object.
