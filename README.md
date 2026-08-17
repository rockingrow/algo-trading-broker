# Algo Trading Broker

A high-performance, decentralized **trading signal broker** built with FastAPI and NATS. It acts as a central hub between TradingView alerts and distributed execution nodes (VPS workers).

## ⚡ Quick Start

### 1. Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- Docker & Docker Compose

### 2. Installation

```bash
git clone <repository-url>
cd algo-trading-broker

cp .env.example .env   # fill in values
make install-dev
```

### 3. Start Infrastructure

```bash
# Start PostgreSQL + NATS via Docker
docker compose up -d postgres nats
```

### 4. Run Database Migrations

```bash
make db-upgrade
```

### 5. Run the Broker

```bash
# Run locally (requires postgres and nats to be reachable)
make run

# Or run the full stack via Docker with hot-reload
make dev
```

### 6. (Optional) Run the Telegram Bot

```bash
docker compose up -d bot
```

The bot is a separate service under [`bot/`](bot/) with its own BotFather token
and uv project. It reads the same root `.env`. See
[`bot/README.md`](bot/README.md) for setup and local development.

---

## ✨ Features

- **Webhook Hub**: Receives and validates TradingView JSON alerts (with optional HMAC signature verification). Every alert is persisted (`status=QUEUED`) and pushed onto a **NATS JetStream** stream so the HTTP request returns as soon as the message is durably queued — the fan-out to workers runs in a background consumer, which closes the `Webhook delivery failed — server closed the connection unexpectedly` failure mode from holding the request open across the pipeline.
- **Persistence**: Logs every signal (with a `QUEUED` → `PUBLISHED` status), trade, and account snapshot to **PostgreSQL** via Alembic-managed migrations.
- **Distribution**: Fan-out signals via **NATS** — each strategy publishes to its own dedicated subject so workers subscribe only to what they need. A durable JetStream consumer (`broker_signal_handler`) does the fan-out so a broker restart mid-fan-out replays the message instead of losing it.
- **Signal replay on reconnect**: Every `WORKER_CONNECTED` handshake is answered with a `retry_signals` list holding every signal persisted in the last `max_retry_timeout` seconds whose strategy the worker announced — so a worker that just came back online catches up without needing external help.
- **Strategy magic map**: The same handshake reply carries a `strategy_magic_map` (both markets) — the strategy → magic-number map from the `strategy_magic_map` setting, filtered to the strategies the worker announced. Editable via `POST /admin/settings/strategy-magic-map` or the Telegram bot's `/admin_magicmap`.
- **Trade Feedback**: Workers report executed positions back to the broker via the NATS `TRADE` subject (no REST endpoint required).
- **Account Tracking**: Worker accounts are auto-upserted from every incoming trade event.
- **API Key Auth**: Management endpoints (`/accounts`, `/settings/*`) are protected by an `X-API-KEY` header validated against `BROKER_API_KEY`.
- **Signal Gating**: A `SIGNAL_BLOCKED` broker setting can pause signal forwarding without restarting the server.
- **Notifications**: Optional Telegram alerts for broker lifecycle events and published signals, plus optional forwarding of `ERROR`-level logs to a dedicated Telegram chat.
- **Developer Friendly**: Includes Makefile, Bruno API collections, Alembic CLI helpers, a pytest suite, and Ruff for linting.

---

## 🏗️ System Architecture

```mermaid
graph TD
    TV[TradingView Alert] -- "POST :8080/webhook" --> Broker
    subgraph "Broker Node (This Repo)"
        Broker[FastAPI Webhook Server]
        DB[(PostgreSQL)]
        NATS["NATS Server :4222 (Token Auth + JetStream)"]
        JS[["JetStream SIGNALS stream"]]
        SW[SignalWorker]
        RJ["SignalRetryJob (15s tick)"]
        Broker -- "Publish SIGNALS.{strategy}" --> JS
        JS -- "Pull consumer" --> SW
        SW -- "Persist QUEUED / mark PUBLISHED / record attempt" --> DB
        SW -- "Publish {strategy}" --> NATS
        RJ -- "list_retryable (QUEUED, attempts > 0)" --> DB
        RJ -- "retry_signal → publish {strategy}" --> NATS
        NATS -- "TRADE events" --> Consumer[TradeEventConsumer]
        Consumer -- "Upsert Trade + Account" --> DB
    end
    NATS -- "{strategy}" --> W1
    NATS -- "{strategy}" --> W2
    NATS -- "{strategy}" --> WN
    subgraph W1["Worker — Forex (MT5)"]
        W1A[Signal Handler] --> W1B[(SQLite)]
        W1B -. "NATS TRADE event" .-> NATS
    end
    subgraph W2["Worker — Forex (MT5)"]
        W2A[Signal Handler] --> W2B[(SQLite)]
        W2B -. "NATS TRADE event" .-> NATS
    end
    subgraph WN["Worker — Crypto"]
        WNA[Signal Handler] --> WNB[(SQLite)]
        WNB -. "NATS TRADE event" .-> NATS
    end
```

---

## 📁 Project Structure

```text
algo-trading-broker/
├── broker/
│   ├── api/             # FastAPI routers: api.py (v1), admin.py, telegram.py, webhook.py
│   ├── db/              # SQLAlchemy models, async engine, repository
│   ├── domain/          # Domain policies (e.g. trade-status state machine)
│   ├── helpers/         # Signal, timeframe, and message-formatting utilities
│   ├── interfaces/      # Protocols for DI (DB, notifier, publisher)
│   ├── schemas/         # Pydantic schemas (webhook, publisher, subscriber, trade, account, admin)
│   ├── security/        # Auth guard (ensure_api_key — X-API-KEY)
│   ├── services/        # nats_service, notification_service,
│   │                    #   signal_processing_service (+ SignalWorker), signal_retry_job
│   ├── app.py           # FastAPI application factory
│   ├── main.py          # Entrypoint (uvicorn runner)
│   ├── router.py        # Aggregates sub-routers under /v1, /admin, /secret
│   ├── providers.py     # Dependency-injection providers
│   ├── nats.py          # NATS connection lifecycle (connect/drain/close)
│   ├── openapi.py       # Shared OpenAPI response definitions
│   ├── constants.py     # Broker setting keys
│   ├── logger.py        # Logging configuration
│   └── settings.py      # Pydantic settings (grouped sub-models) loaded from .env
├── bot/                 # Telegram bot service (aiogram v3) — see bot/README.md
│   ├── app/             # handlers, services, middlewares, keyboards, presenters, utils
│   └── tests/           # Bot pytest suite (own pyproject.toml / uv project)
├── alembic/             # Alembic migration environment and version scripts
├── bruno/               # Bruno API client collections
├── examples/            # Example webhook / NATS / worker JSON payloads
├── scripts/             # Utility scripts (docker-entrypoint, ensure_keys)
├── tests/               # Pytest unit tests
├── Makefile             # Automation shortcuts (uv, Docker, Alembic, linters)
├── Dockerfile           # Production container definition
├── docker-compose.yml   # Infrastructure (PostgreSQL + NATS + Broker)
└── pyproject.toml       # uv dependencies & tool config
```

---

## 📡 NATS Subjects

The broker uses **token-based authentication** with the NATS server. Workers must supply the same token when connecting.

| Direction | Subject | Purpose |
| --------- | ------- | ------- |
| Publish (broker → workers) | `{strategy}` | Signal routed to subscribers of that strategy (e.g. `wt_cross_v1`) |
| Publish (broker → workers) | `ADMIN` | Broadcast administrative messages (no `account_id`) — every worker receives and filters for itself |
| Publish (broker → one worker) | `ADMIN.<market>.<gateway>.<account_id>` | Account-scoped administrative message on a private per-account subject; only that account's worker is subscribed, so no other worker learns the `account_id` |
| Publish (broker → workers) | `SYSTEM` | The `WORKER_CONNECTED_ACK` answering a worker's announcement (magic map, signal replay, crypto leverage), plus the `CRYPTO_LEVERAGE_INIT` pushed to already-connected workers on an admin change |
| Publish (broker → broker) | `SIGNALS.<strategy>` (JetStream stream `SIGNALS`) | Durable webhook envelope buffer — the webhook endpoint enqueues here, the broker's own `SignalWorker` consumes and fans out to `{strategy}` |
| Subscribe (workers → broker) | `TRADE` | Position events reported by workers after execution |
| Subscribe (workers → broker) | `SYSTEM` | `WORKER_CONNECTED` announcements published by a worker right after it connects (payload carries `account_id` in `<market>-<gateway>-<account_id>` format, plus `market`, `gateway`, and `strategies`) |

Each signal is published to the subject that matches its `strategy` field. Workers subscribe only to the strategies they handle, eliminating cross-strategy noise.

Every payload on `{strategy}` — whether it's a full `TradingSignal` (LONG/SHORT/TP/…) or the shorter FLAT directive — carries a `signal_id`. That is the same id the broker uses inside the handshake's `retry_signals` replay, so a worker that sees a signal live and then again as part of a reconnect replay can de-duplicate by `signal_id`.

### `TRADE` events

Workers publish a `TRADE` message (a `PositionEvent`) whenever a row in their local `positions` table is inserted or updated. The broker upserts it into the `trades` table keyed by `(market, gateway, account_id, ref_id)` — not `account_id` alone, since the same bare `account_id` can exist under a different market/gateway (see [`accounts` table](#accounts-table)) — translating the worker's position status into a broker trade `status`:

| Worker position status | Broker trade `status` | Running? |
| ---------------------- | --------------------- | -------- |
| `OPENED` | `OPENED` | Yes |
| `TP1` | `PARTIALLY_CLOSED` | Yes |
| `TP2`, `SL`, `R_SL`, `TERMINAL_CLOSED`, `FORCED_CLOSED` | `CLOSED` | No |
| `FLATTED` | `FLAT` | No |
| `REJECTED` | `REJECTED` | No |

**`REJECTED`** is emitted when a worker refuses to place an order — for example when its **MAX ORDER** limit is reached. The worker still records the order in its own database and fires the `TRADE` event, so the broker persists a terminal, non-running trade carrying the worker's `reject_reason` (e.g. `"MAX ORDER limit reached"`). `REJECTED` ranks below every other status, so an event for an already-existing `ref_id` is treated as a lifecycle downgrade and ignored — only a brand-new order is recorded as rejected.

### JetStream signal pipeline

The webhook endpoint is a fast enqueue-only path. Everything else runs from a background handler, with a retry loop that can re-try failed signals a bounded number of times.

1. **Webhook** (`POST /secret/webhook`) verifies the `token` and pushes the raw envelope onto the JetStream stream `SIGNALS` (subject `SIGNALS.<strategy>`). No DB write, no block check, no fan-out — the response is `202 {"status":"queued"}` as soon as JetStream ack-s the write, so TradingView is never held open across the pipeline.
   - That ack is waited for under a hard deadline (`WEBHOOK_ENQUEUE_TIMEOUT`, default `1.0s`). nats-py would otherwise wait `5s` for it, which is longer than TradingView waits for the whole request: one slow ack — a NATS reconnect, a busy file store — and the alert dies as **"request took too long and timed out"**, never to be re-sent.
   - Past the deadline the envelope goes to the in-memory **`DeferredEnqueuer`**, the response is `202 {"status":"deferred"}`, and a background task keeps re-publishing it every `WEBHOOK_DEFERRED_ENQUEUE_INTERVAL` seconds up to `WEBHOOK_DEFERRED_ENQUEUE_MAX_ATTEMPTS` times. Giving up (or a full backlog) is logged at `error`. If the queue cannot take the envelope at all, the webhook answers `503` immediately — a refusal TradingView shows in its alert log beats a timeout it can only report as "too slow".
   - Every enqueue carries a `Nats-Msg-Id` header that stays the same across those retries, and the stream sets a 120s `duplicate_window`. So a first publish whose ack was merely *slow* (the message did land) is de-duplicated by JetStream instead of reaching the workers twice as a second position.
   - Each webhook response is logged with the elapsed milliseconds — raised to `warning` past the deadline — since TradingView's own timeout leaves no trace on the server.
2. **`SignalWorker`** (`broker/services/signal_processing_service.py`) is a durable pull consumer (`broker_signal_handler`) that fetches envelopes from the stream. On the first attempt it runs the block gate (drops + notifies if blocked), persists the row (`status=QUEUED`, `attempts=SIGNAL_MAX_ATTEMPTS`, `last_attempt=NULL`), and calls the shared fan-out (`_fanout`) which publishes to workers on `{strategy}` (or `ADMIN` for `FLAT`), sends the Telegram notification, and flips the DB row to `status=PUBLISHED`.
3. On a fan-out failure the row stays `QUEUED` but `record_attempt_failure` decrements `attempts` and stamps `last_attempt`. The JetStream message is `ack`-ed regardless — retries are driven by the DB rather than JetStream redelivery so the two mechanisms cannot race.
4. **`SignalRetryJob`** (`broker/services/signal_retry_job.py`) ticks every `settings.signal.RETRY_INTERVAL_SECONDS` (default `15`), looks up rows still `QUEUED` with `attempts > 0` and `last_attempt` older than that same interval, and hands each to `SignalProcessingService.retry_signal`. The retry rebuilds the `WebhookPayload` from `row.raw` and calls `_fanout` again.
5. Once `attempts` would drop below `1`, the row is flipped to `status=FAILED` and no longer picked up.

**Retry-aware notifications**: the Telegram signal / FLAT message carries an `Attempt: N` line on the 2nd and 3rd attempts (not on the fresh first attempt) so the operator sees when the broker is retrying.

**Notifications never sit on the signal path**: the fan-out hands its Telegram message to a `QueuedNotifier` and moves on. `api.telegram.org` is throttled or filtered on many networks — the connection is accepted and no response arrives, so a send hangs for the whole `TELEGRAM_HTTP_TIMEOUT` — and the `SignalWorker` handles envelopes one at a time, so awaiting that send would delay the *next* signal's delivery to the trading workers by the same amount. NATS lifecycle alerts are queued for the same reason: nats-py awaits those callbacks inside its own reconnect loop.

Enable JetStream on your NATS server (`nats-server -js -sd <path>`) — the bundled `docker-compose.yml` already does so and mounts the `nats_data` volume for durability.

### `SYSTEM` handshake

When a worker successfully connects to NATS, it announces itself on the `SYSTEM` subject. `account_id`, `market`, and `gateway` are all required — messages missing any of them are rejected by validation. `strategies` is optional; when set, it lists the strategy subjects the worker subscribes to and selects both the magic-map entries and the signal replay it gets back.

```json
{
  "action": "WORKER_CONNECTED",
  "account_id": "CRYPTO-BINANCE-7654321",
  "timestamp": "2026-06-30T00:00:00+00:00",
  "market": "CRYPTO",
  "gateway": "BINANCE",
  "strategies": ["wt_cross_v1", "MT5_GOLD_M5_V1"]
}
```

#### Request/reply (recommended)

Workers should announce themselves with **NATS request/reply** (`nc.request(...)`) rather than a fire-and-forget publish. The broker replies **directly on the request's inbox** with the outcome of the handshake, so:

- the reply reaches only the worker that asked (no fan-out to every `SYSTEM` subscriber), and
- the worker's `request` **always resolves** — on success or on an error — instead of hanging.

Because the reply is worker-driven, a worker that connects while the broker is **down or restarting** simply **times out and retries**; the handshake is idempotent, so retries are safe. This closes the delivery gap of plain fire-and-forget pub/sub, where a `WORKER_CONNECTED` published before the broker's subscription was active would be lost silently.

**One handshake, one reply.** A NATS reply inbox accepts a *single* message: `request()` resolves its future (or, in the `old_style` form, auto-unsubscribes at `max_msgs=1`) on the first reply and silently drops everything after it. So the broker answers with exactly one of two actions, and the successful one carries the worker's **whole** initial configuration:

| Situation | Reply action | Payload |
| --------- | ------------ | ------- |
| Settings loaded | `WORKER_CONNECTED_ACK` | `strategy_magic_map`, `retry_signals`, and `crypto_leverage_init` (crypto only) |
| Crypto settings missing/invalid | `WORKER_CONNECTED_ERROR` | `reason` |

```json
{
  "action": "WORKER_CONNECTED_ACK",
  "account_id": "CRYPTO-BINANCE-7654321",
  "timestamp": "2026-06-30T00:00:00+00:00",
  "strategy_magic_map": { "wt_cross_v1": 20260617 },
  "retry_signals": [],
  "crypto_leverage_init": {
    "symbols": ["BTC", "ETH"],
    "default_leverage": 10
  }
}
```

The three blocks are always present, so a worker can parse them unconditionally:

- **`strategy_magic_map`** — the strategy → magic-number map, see [Strategy magic map](#strategy-magic-map) below. `{}` when nothing matched.
- **`retry_signals`** — the catch-up replay, see [Signal replay on reconnect](#signal-replay-on-reconnect) below. `[]` when there is nothing to replay.
- **`crypto_leverage_init`** — `symbols` + `default_leverage` from the `crypto_allowed_symbol` and `crypto_max_leverage` `BrokerSetting` rows, for a crypto worker. `null` for every other market.

Examples: `examples/nats/system.worker_connected_ack.json` (forex) and `examples/nats/system.worker_connected_ack.crypto.json`.

If a crypto worker's settings are missing or invalid, it gets an explicit error it can log or retry on — **instead of** the ACK, never after it — rather than being told it is configured when it is not:

```json
{
  "action": "WORKER_CONNECTED_ERROR",
  "account_id": "CRYPTO-BINANCE-7654321",
  "timestamp": "2026-06-30T00:00:00+00:00",
  "reason": "crypto settings not configured"
}
```

#### Strategy magic map

The `strategy_magic_map` block of the ACK carries the strategy → magic-number map the worker needs, for **both** markets. It is sourced from the `strategy_magic_map` `BrokerSetting` (stored as JSON text) and filtered down to just the strategies the worker announced in `strategies`, so each worker only receives its own entries. Because it is filtered per worker, it rides on the request's reply inbox (or, for a fire-and-forget `publish`, on the shared `SYSTEM` subject with the `account_id` for the worker to filter). It is mandatory — present even when the resulting map is empty (the worker announced no mapped strategy, or the setting is unset).

```json
{
  "strategy_magic_map": {
    "MT5_GOLD_M5_V1": 20260409,
    "MT5_MULTI_M5_V1": 20260708
  }
}
```

Edit the map with `POST /admin/settings/strategy-magic-map` (or the Telegram bot's `/admin_magicmap`); workers pick up the new map on their next connect (within the ~30s settings cache).

#### Fire-and-forget (backward compatible)

A worker may still `publish` `WORKER_CONNECTED` without a reply inbox. In that case the broker broadcasts the same `WORKER_CONNECTED_ACK` on the shared `SYSTEM` subject and every worker filters by `account_id`; error outcomes can only be logged, not signalled back. Request/reply is preferred precisely because it removes that blind spot and keeps one worker's configuration out of every other worker's inbox.

The broker filters its own outgoing `SYSTEM` actions (`CRYPTO_LEVERAGE_INIT`, `WORKER_CONNECTED_ACK`, `WORKER_CONNECTED_ERROR`) by `action`, so it never reacts to its own messages.

#### Signal replay on reconnect

The `retry_signals` block of the ACK carries every signal the broker persisted in the last `max_retry_timeout` seconds (default `60`, tunable via the `max_retry_timeout` broker setting) whose `strategy` is in the worker's announced `strategies` list. It is a **list** of the same signal objects normally published on the `{strategy}` subject, so the worker can feed them straight back into its usual signal handler and de-duplicate against live signals by `signal_id`.

Empty when the worker announced no strategies, or when there is nothing recent to replay.

```json
{
  "retry_signals": [
    {
      "signal_id": "sig_123456789_long",
      "timestamp": "2026-07-15T23:59:30+00:00",
      "strategy": "wt_cross_v1",
      "action": "LONG",
      "symbol": "XAUUSD",
      "price": 2350.5,
      "quantity": 0.1,
      "sl": 2340.0,
      "tp1": 2370.0,
      "tp2": 2390.0,
      "risk_percent": 1.0
    }
  ]
}
```

#### Live config push on admin update

The handshake pushes crypto config when a worker *connects*. To also update workers that are **already connected**, `POST /admin/settings/crypto-allowed-symbol` and `POST /admin/settings/crypto-max-leverage` send a `CRYPTO_LEVERAGE_INIT` on the shared `SYSTEM` subject right after persisting the change, so the new value applies immediately instead of waiting for the next reconnect (up to the ~30s settings cache).

The broker looks up every **crypto account** in the `accounts` table and addresses one message per account to its `<market>-<gateway>-<account_id>` worker id — built from the account's `market`, `gateway`, and `account_id` — so each worker filters by its own id:

```json
{
  "action": "CRYPTO_LEVERAGE_INIT",
  "account_id": "CRYPTO-BINANCE-7654321",
  "timestamp": "2026-06-30T00:00:00+00:00",
  "symbols": ["BTC", "ETH"],
  "default_leverage": 10
}
```

Both `symbols` and `default_leverage` are read back from `BrokerSetting`, so whichever setting the admin did *not* just change is included from the DB. The push is best-effort: the setting is already persisted (and still reaches workers on their next handshake), so if the complementary setting is missing or invalid, a crypto account has no `gateway` recorded yet, or a publish fails — the affected message is logged and skipped while the endpoint still returns `200`.

---

## ⚙️ Configuration (`.env`)

Start from [`.env.example`](.env.example) (`cp .env.example .env`). One file
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
WEBHOOK_PORT=80            # docker-compose defaults this to 8080 instead

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

# Optional HMAC secret — set the same value in TradingView alert header
# X-Signature: <sha256-hex-of-body>
# Leave blank to disable validation.
WEBHOOK_SECRET=

# ── NATS ─────────────────────────────────────────────
NATS_HOST=localhost        # overridden to "nats" inside Docker
NATS_PORT=4222
NATS_MONITOR_PORT=8222     # HTTP monitoring dashboard (compose only)
NATS_TOKEN=changeme        # shared secret; leave blank = no auth

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
TELEGRAM_HTTP_TIMEOUT=5.0      # seconds per Bot API call
TELEGRAM_LOG_BOT_TOKEN=        # dedicated log bot (falls back to TELEGRAM_BOT_TOKEN)
TELEGRAM_LOG_CHAT_ID=          # dedicated log chat (falls back to TELEGRAM_BROKER_LOG_CHAT_IDS)

# ── Telegram bot service (interactive, ./bot) ────────
# A *second* BotFather bot, separate from TELEGRAM_BOT_TOKEN above.
# Full reference: bot/README.md → Configuration
BOT_TELEGRAM_TOKEN=
TELEGRAM_ADMIN_IDS=            # comma-separated admin user ids, e.g. 123,456
BOT_BROKER_BASE_URL=http://localhost:8080   # → http://broker:8080 in Docker
BOT_LOG_LEVEL=INFO
BOT_REQUEST_TIMEOUT=10.0
```

### Telegram chat ids: many chats, and group topics

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

### Not in `.env`

A few knobs live in [`broker/settings.py`](broker/settings.py) only, because they
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

---

## 🛠️ Development

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
| `make simulate-nats` | Replay an example NATS payload against the running stack |
| `make format` | Format code with Ruff |
| `make lint` | Run Ruff check |
| `make check` | Alias for `make lint` |
| `make fix` | Format and auto-fix linting issues |

### Database (Alembic)

| Command | Description |
| ----------------------------- | ------------------------------------------- |
| `make db-upgrade` | Apply all pending migrations (`upgrade head`) |
| `make db-downgrade` | Roll back one migration step |
| `make db-history` | Show full migration history |
| `make db-current` | Show current revision in the database |
| `make db-revision m='msg'` | Generate a new auto-migration file |

---

## 🌐 API

### Interactive docs (Swagger / OpenAPI)

FastAPI auto-generates interactive API documentation. With the server running:

| Page | URL | Notes |
| ---- | --- | ----- |
| Swagger UI | `http://localhost:8080/docs` | Try endpoints; click **Authorize** to set `X-API-KEY`. |
| ReDoc | `http://localhost:8080/redoc` | Read-only reference. |
| OpenAPI schema | `http://localhost:8080/openapi.json` | Raw spec. |

Set `DOCS_ENABLED=false` in `.env` to disable all three in production.

### URL Prefixes

All routes are grouped under versioned or purpose-scoped prefixes:

| Prefix | Router | Description |
| ------ | ------ | ----------- |
| `/v1` | API | Public API endpoints (accounts, trades, health) |
| `/admin` | Admin | Management endpoints (settings, trading actions) |
| `/secret` | Webhook | TradingView webhook receiver |

If `BROKER_API_PREFIX` is set (e.g. `abc123xyz`), every route is mounted under that secret segment:

```text
/abc123xyz/v1/health
/abc123xyz/v1/accounts
/abc123xyz/admin/flat
/abc123xyz/secret/webhook
```

The prefix acts as a URL secret — an attacker who knows the IP or domain still cannot enumerate endpoints without it. Leave blank to use the default paths.

### Authentication

Management endpoints require an API key passed in the `X-API-KEY` header, validated against `BROKER_API_KEY`:

```bash
curl http://localhost:8080/v1/accounts -H "X-API-KEY: $BROKER_API_KEY"
```

Missing or invalid keys return `401 Unauthorized`. If `BROKER_API_KEY` is unset, protected endpoints return `500`. The `/v1/health` and `/secret/webhook` endpoints are **not** key-protected (`/secret/webhook` uses its own in-payload `token`).

| Endpoint | Auth |
| -------- | ---- |
| `GET /v1/health` | None |
| `POST /secret/webhook` | In-payload `token` (+ optional HMAC) |
| `GET /v1/accounts` | `X-API-KEY` |
| `POST /admin/accounts` | `X-API-KEY` |
| `GET /v1/{account_id}/trades` | `X-API-KEY` |
| `POST /admin/settings/block-signal` | `X-API-KEY` |
| `POST /admin/settings/silent-signal` | `X-API-KEY` |
| `POST /admin/settings/include-signal-raw` | `X-API-KEY` |
| `POST /admin/settings/crypto-allowed-symbol` | `X-API-KEY` |
| `GET /admin/settings/crypto-allowed-symbol` | `X-API-KEY` |
| `POST /admin/settings/crypto-max-leverage` | `X-API-KEY` |
| `GET /admin/settings/crypto-max-leverage` | `X-API-KEY` |
| `GET /admin/settings/strategy-magic-map` | `X-API-KEY` |
| `POST /admin/settings/strategy-magic-map` | `X-API-KEY` |
| `POST /admin/settings/notification-timezone` | `X-API-KEY` |
| `GET /admin/settings/notification-timezone` | `X-API-KEY` |
| `GET /admin/settings` | `X-API-KEY` |
| `POST /admin/flat` | `X-API-KEY` |
| `POST /admin/accounts/{account_id}/link-token/rotate` | `X-API-KEY` |
| `POST /admin/accounts/{account_uuid}/link-telegram` | `X-API-KEY` |
| `POST /v1/telegram/link` | `X-API-KEY` |
| `GET /v1/telegram/{telegram_user_id}` | `X-API-KEY` |
| `GET /v1/telegram/{telegram_user_id}/accounts` | `X-API-KEY` |
| `POST /v1/telegram/{telegram_user_id}/active-account` | `X-API-KEY` |
| `GET /v1/telegram/{telegram_user_id}/trades` | `X-API-KEY` |
| `POST /v1/telegram/{telegram_user_id}/commands/flat` | `X-API-KEY` |
| `POST /v1/telegram/{telegram_user_id}/commands/prevent` | `X-API-KEY` |
| `POST /v1/telegram/{telegram_user_id}/unlink` | `X-API-KEY` |
| `GET /v1/telegram/{telegram_user_id}/broadcast` | `X-API-KEY` |
| `POST /v1/telegram/{telegram_user_id}/broadcast/subscribe` | `X-API-KEY` |
| `POST /v1/telegram/{telegram_user_id}/broadcast/unsubscribe` | `X-API-KEY` |

---

### GET `/v1/health`

Returns `{"status": "ok"}`. No authentication required.

---

### POST `/secret/webhook`

Receives signals from TradingView. Validates the optional HMAC `X-Signature` header if `WEBHOOK_SECRET` is set. Verifies the in-payload `token` and pushes the raw envelope onto the JetStream `SIGNALS` stream (`SIGNALS.<strategy>`). Responds `202 Accepted` (`status=queued`) as soon as JetStream ack-s the write. Everything else — DB persist, block gate, publish to the `{strategy}` subject, Telegram notification, retries — runs from the background `SignalWorker` and, on failure, the periodic `SignalRetryJob`.

The wait for that ack is capped at `WEBHOOK_ENQUEUE_TIMEOUT`: past it the response is still `202`, with `status=deferred`, and the enqueue is retried in the background (see [JetStream signal pipeline](#jetstream-signal-pipeline)). `503` means the enqueue failed *and* could not be deferred — the signal was dropped.

**Example Payload:**

```json
{
  "token": "your_secure_token",
  "strategy": "wt_cross_v1",
  "signal_uxid": "9f2c4b7e18a3d605",
  "symbol": "XAUUSD",
  "timeframe": "M5",
  "timestamp": "2024-03-20T10:00:00Z",
  "position": {
    "action": "LONG",
    "price": 1900.5,
    "quantity": 0.1,
    "sl": 1890.0,
    "tp1": 1920.0,
    "tp2": 1950.0,
    "is_running": true,
    "is_scale_position": true,
    "scaling": {
      "tp": 1925.0,
      "sl": 1895.0,
      "quantity": 0.05
    }
  },
  "indicators": {
    "wt1": 12.5,
    "wt2": 10.2,
    "ema200": 1880.0
  },
  "inputs": {
    "risk_percent": 1.0,
    "use_session": true
  }
}
```

**Supported Actions:** `LONG`, `SHORT`, `TP1`, `TP2`, `R_SL`, `SL`, `FLAT`.

**`signal_uxid` (required):** a 16-character lowercase-hex short uuid the
TradingView strategy generates once per *trade cycle* and reuses across
every action of that cycle — the `LONG` entry, its `TP1`, `TP2`, `SL`, and
the closing `FLAT` all carry the same `signal_uxid`. That is what ties the
whole trade to one edited-in-place Telegram broadcast message. A payload
that omits it or sends any other shape is rejected with `422`. The
per-signal `signal_id` the broker mints on persist is still unique per
alert (workers keep deduping on it) — the two ids serve different jobs and
both travel on every downstream NATS payload.

#### Position fields — `tp1`/`tp2`/`sl` vs `scaling`

A TradingView strategy can contain **multiple sub-strategies** running under the same parent strategy name. Each sub-strategy may apply different risk/reward profiles to the same signal — for example, a `LOW_RR_TIER` sub-strategy is designed to catch entries more frequently but accepts a tighter TP and higher relative risk, which means the effective TP, SL, and quantity differ from the base signal values.

To support this, the `position` block carries two sets of exit levels:

| Field | Purpose |
| ----- | ------- |
| `tp1`, `tp2`, `sl` | Base levels from the **primary** strategy logic — always present. |
| `is_scale_position` | `true` when a sub-strategy wants to **scale into** an existing open position rather than open a new one. |
| `scale_strategy` | Name of the sub-strategy that triggered the scale-in (e.g. `LOW_RR_TIER`). Lets workers apply sub-strategy-specific position sizing or risk rules. |
| `scaling.tp`, `scaling.sl`, `scaling.quantity` | **Override** levels and size for the scale-in leg. These replace `tp1`/`sl`/`quantity` for the additional entry — they are forwarded on the NATS `SIGNAL` payload only when `is_scale_position` is `true`. |

**Example flow:** the main strategy fires a `LONG` signal with `tp1=1950, sl=1890`. At the same bar, the embedded `LOW_RR_TIER` sub-strategy decides to add to the position with a tighter target (`tp=1925`) and smaller size (`quantity=0.05`). The webhook sets `is_scale_position=true`, `scale_strategy="LOW_RR_TIER"`, and populates the `scaling` block accordingly. Workers that receive the signal can read `scale_strategy` to decide whether to apply the scale-in and by how much.

---

### GET `/v1/accounts`

Returns all trading accounts ordered by most recent activity. Requires the `X-API-KEY` header.

**Response:**

```json
[
  {
    "id": "uuid",
    "account_id": "12345678",
    "account_name": "Demo Account",
    "account_balance": 10000.0,
    "market": "FOREX",
    "gateway": "MT5",
    "last_activity_at": "2024-03-20T10:05:00Z",
    "link_token": "b5dc0374-9639-4861-acf4-2d239aa5c1b4",
    "linked_user_ids": ["123456789"],
    "createdAt": "2024-03-01T00:00:00Z",
    "updatedAt": "2024-03-20T10:05:00Z"
  }
]
```

`link_token` is the account's currently valid invite secret (joined in from
`account_link_tokens`) — hand it to a user so they can link the bot.
`linked_user_ids` lists every bot user already linked to the account (from
`account_bot_links`); it is empty for an unclaimed account, and can hold more
than one id since an account may be managed by several people.

Accounts are automatically created or updated each time a `TRADE` event arrives from a worker (or manually via `POST /admin/accounts`, below). `gateway` records the exchange the account trades through (e.g. `MT5` for forex, `BINANCE` for crypto), taken from the `TRADE` event; combined with `market` and `account_id` it forms the `<market>-<gateway>-<account_id>` worker id the broker uses to address `SYSTEM` messages.

`account_id` alone is **not** unique — the same bare id can exist under a different `market`/`gateway` pair (two unrelated real accounts, e.g. an MT5 login and a Binance account, can coincidentally share a number). The unique key is the full `(market, gateway, account_id)` triple.

---

### POST `/admin/accounts`

Manually registers an account — `market`, `gateway`, and an `account_id` chosen by the admin — before it has ever traded or its worker has connected, so a link token can be handed to the end-user right away. Requires the `X-API-KEY` header.

**Request Body:**

```json
{
  "market": "CRYPTO",
  "gateway": "BINANCE",
  "account_id": "7654321",
  "account_name": "Main Crypto"
}
```

`gateway` must be valid for `market` (currently `FOREX` → `MT5`, `CRYPTO` → `BINANCE`) or the request is rejected with `422`. `account_id` may not contain `:` or whitespace (it's embedded verbatim in the Telegram bot's callback data) and must be at most 50 characters. Returns `409` if the `(market, gateway, account_id)` triple already exists — reusing the same `account_id` under a *different* gateway is allowed and creates a distinct account.

**Response** (`201`): the created account, shaped like a row in [`GET /v1/accounts`](#get-v1accounts) — including a freshly generated `link_token`.

---

### GET `/v1/{account_id}/trades`

Returns a paginated list of trades for the given account. Requires the `X-API-KEY` header.

> **Note:** filters by bare `account_id` only. If that id has been reused across gateways (see above), this can match trades from more than one account — pass a `account_id` you know is unambiguous, or avoid reusing ids across gateways.

**Query Parameters:**

| Parameter | Default | Description |
| --------- | ------- | ----------- |
| `limit` | `20` | Number of results (1–100) |
| `offset` | `0` | Skip this many rows |
| `order` | `desc` | Sort direction: `asc` or `desc` |
| `order_by` | `updatedAt` | Sort column: `updatedAt`, `createdAt`, `status`, `symbol` |

**Response:**

```json
{
  "data": [
    {
      "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "account_id": "MT5-12345678",
      "account_leverage": 100,
      "account_balance_init": 10000.0,
      "account_balance": 10250.75,
      "ref_id": "987654321",
      "comment": null,
      "strategy_code": "LONG|SIG-001",
      "gateway_return_code": 0,
      "strategy": "BTC-M15",
      "symbol": "BTCUSDT",
      "action": "LONG",
      "price": 65000.0,
      "quantity": 0.01,
      "sl": 63000.0,
      "tp1": 67000.0,
      "tp2": 69000.0,
      "is_running": true,
      "risk_percent": 1.0,
      "status": "OPENED",
      "reject_reason": null,
      "createdAt": "2026-06-01T08:00:00Z",
      "updatedAt": "2026-06-02T09:30:00Z"
    }
  ],
  "page": {
    "total": 42,
    "limit": 20,
    "offset": 0,
    "order": "desc",
    "order_by": "updatedAt"
  }
}
```

---

### POST `/admin/settings/block-signal`

Toggles the `SIGNAL_BLOCKED` broker setting between `"1"` (signals blocked) and `"0"` (signals forwarded). Requires the `X-API-KEY` header. Does not require a restart. Sends a Telegram notification on change.

---

### POST `/admin/settings/silent-signal`

Toggles the `SILENT_SIGNAL` broker setting between `"1"` (Telegram notifications muted) and `"0"` (notifications active). Useful for pausing alerts without disabling Telegram entirely. Requires the `X-API-KEY` header.

---

### POST `/admin/settings/include-signal-raw`

Toggles the `NOTIFICATION_INCLUDE_SIGNAL_RAW` setting. When enabled (`"1"`), Telegram signal notifications include the full `indicators` and `inputs` blocks. Requires the `X-API-KEY` header.

---

### POST `/admin/settings/crypto-allowed-symbol`

Sets the `crypto_allowed_symbol` broker setting pushed to crypto workers via `SYSTEM.CRYPTO_LEVERAGE_INIT`. Requires the `X-API-KEY` header.

**Request Body:**

```json
{
  "symbols": ["BTC", "ETH"]
}
```

Symbols are upper-cased, trimmed, and de-duplicated before being stored as a comma-separated string. At least one non-empty symbol is required (`422` otherwise).

On success the broker also **pushes** a targeted `SYSTEM.CRYPTO_LEVERAGE_INIT` to each crypto account by its `<market>-<gateway>-<account_id>` worker id (see [Live config push on admin update](#live-config-push-on-admin-update)), so already-running workers apply the new list immediately. That message also carries `crypto_max_leverage` read from the DB, so it is skipped (and logged) until that setting is configured. A worker that *connects* right after this call may still read the previous value from `SystemEventConsumer`'s up-to-30s cache.

---

### POST `/admin/settings/crypto-max-leverage`

Sets the `crypto_max_leverage` broker setting pushed to crypto workers via `SYSTEM.CRYPTO_LEVERAGE_INIT`. Requires the `X-API-KEY` header.

**Request Body:**

```json
{
  "default_leverage": 10
}
```

`default_leverage` must be a positive integer (`422` otherwise).

On success the broker also **pushes** a targeted `SYSTEM.CRYPTO_LEVERAGE_INIT` to each crypto account by its `<market>-<gateway>-<account_id>` worker id (see [Live config push on admin update](#live-config-push-on-admin-update)), so already-running workers apply the new leverage immediately. That message also carries `crypto_allowed_symbol` read from the DB, so it is skipped (and logged) until that setting is configured. A worker that *connects* right after this call is still subject to the same up-to-30s cache as `crypto-allowed-symbol`.

---

### GET `/admin/settings/strategy-magic-map`

Returns the current `strategy_magic_map` broker setting as JSON text (`{}` when unset). Requires the `X-API-KEY` header.

**Response Body:**

```json
{
  "setting": "strategy_magic_map",
  "value": "{\"MT5_GOLD_M5_V1\": 20260409, \"SIDEWAY_M15_V1\": 20260617}"
}
```

---

### POST `/admin/settings/strategy-magic-map`

Sets the `strategy_magic_map` broker setting: the strategy → magic-number map sent to every worker in its `WORKER_CONNECTED_ACK` on connect (filtered to the strategies each worker announces). Requires the `X-API-KEY` header.

**Request Body:**

```json
{
  "magic_map": {
    "MT5_GOLD_M5_V1": 20260409,
    "SIDEWAY_M15_V1": 20260617
  }
}
```

Values must be integers and at least one entry is required (`422` otherwise), so an accidental empty submission can't wipe the map. The map is stored as canonical JSON text. Unlike the crypto settings, there is **no live push** — the broker does not persist which strategies each connected worker holds, so workers pick up the new map on their next `WORKER_CONNECTED` (within the up-to-30s settings cache). The Telegram bot's `/admin_magicmap` command wraps this endpoint.

---

### POST `/admin/settings/notification-timezone`

Sets the `notification_timezone` broker setting: the UTC offset (in hours) applied to the `Time:` line of Telegram notifications. Requires the `X-API-KEY` header.

**Request Body:**

```json
{
  "utc_offset_hours": 7
}
```

`utc_offset_hours` must be between `-12` and `14` (`422` otherwise). Signal timestamps are normalised to UTC first, then shifted by this offset before formatting, e.g. `Time: 2026-07-06 12:55:00 (UTC+7)`. Defaults to `7` (UTC+7) when unset.

---

### GET `/admin/settings/notification-timezone`

Reads the current `notification_timezone` offset. Requires the `X-API-KEY` header.

**Response:**

```json
{
  "setting": "notification_timezone",
  "value": "7"
}
```

Returns the default `"7"` when the setting is unset or holds an unparseable value, so the caller always gets the offset actually in effect. This is what lets the [Telegram bot](#-telegram-bot) render times in the same zone as broker-sent notifications — the bot talks only to the HTTP API and never reads `broker_settings` itself.

---

### POST `/admin/flat`

Publishes a `FLAT` directive to workers over NATS. Scope can be narrowed by passing optional fields in the JSON body. An account-scoped FLAT (see below) goes to a private per-account subject; an unscoped FLAT is broadcast on the shared `ADMIN` subject.

**Request Body (all fields optional):**

```json
{
  "strategy": "wt_cross_v1",
  "symbol": "XAUUSD",
  "account_id": "MT5-12345678",
  "market": "FOREX",
  "gateway": "MT5"
}
```

Omit all fields (or send an empty body `{}`) to flat every open position across all workers.

When `account_id` is set, `market` and `gateway` are **required** with it (422 otherwise) — since `account_id` is no longer globally unique (see [`accounts` table](#accounts-table)), all three together identify one account. The FLAT is then published to the private subject `ADMIN.<market>.<gateway>.<account_id>` that **only that account's worker** is subscribed to, so no other worker ever sees the `account_id` and each worker stays isolated to its own account. Omitting `account_id` (a strategy/symbol-scoped or flat-everything directive) broadcasts on the shared `ADMIN` subject to **every** connected worker, which filters for itself client-side (worker-side code, outside this repo).

---

## 🤖 Telegram Bot

An interactive bot lives in [`bot/`](bot/) (built with **aiogram v3**), serving
**two roles** from one process — endusers and admins. It is a **thin HTTP
client** of the broker — it never touches PostgreSQL or NATS directly, calling
broker endpoints with the broker `X-API-KEY`.

> 📖 **Full documentation — commands, rendering, architecture, configuration and
> local development — lives in [`bot/README.md`](bot/README.md).** This section
> only covers what the *broker* side needs to know: the data model backing the
> bot and the endpoints it calls.

Command menus are role-aware **and** link-aware (Telegram command **scopes**): a
user with no linked account sees `/start` and nothing else — every other
command, `/help` included, needs an account behind it — and the full menu
appears the moment they link. Each id in `TELEGRAM_ADMIN_IDS` additionally gets
the admin menu (`/admin_accounts`, `/admin_newaccount`, `/admin_trades`,
`/admin_flat`, `/admin_rotate`, `/admin_settings`, …), which is not gated on
linking.

**Onboarding / auth flow**

1. Every account has at least one link token (UUID) in `account_link_tokens`. An
   admin reads it as `link_token` from `GET /v1/accounts` (or rotates it via
   `POST /admin/accounts/{account_id}/link-token/rotate`) and hands it to the user.
2. The user sends `/start` to the bot and pastes the token. The bot calls
   `POST /v1/telegram/link`, which records an `account_bot_links` row joining
   their Telegram id to the account.
3. Linked users can then query trades (`/trades`) and issue control commands.

**Many-to-many: accounts ↔ bot users**

`account_bot_links` is a join table, so both directions are open:

- One user may link **several** accounts — typically one per market/gateway
  pair (e.g. an MT5 forex account and a Binance crypto account).
- One account may be linked by **several** users — e.g. an owner and an
  assistant. There is no role distinction yet: every linked user has the same
  rights over the account.

Linking never removes an existing link in either direction, and `/unlink` only
drops the caller's own.

**Active account**

For each user, exactly one of their linked accounts is **active** at a time, and
every single-account command (`/status`, `/trades`, `/flat`, `/prevent`,
`/allow`, `/unlink`) acts on whichever one that is.

- `/link` — add another account (paste a second token). The first account
  linked becomes active automatically; adding more does not change the
  active one.
- `/myaccounts` — list the linked accounts read-only, without the picker.
- `/switch` — the same list, paired with one button per account; tap one to
  activate it.

The selection lives in the broker's `bot_sessions` table, not in bot memory, so
it survives bot restarts. If it ever points at an account the user no longer
holds a link to, the broker falls back to their most recently active account and
repairs the row.

**Control commands** publish `ADMIN`-subject directives via the broker:

| Command | Admin action | Notes |
| ------- | ------------ | ----- |
| `/flat` | `FLAT` | Close positions for the **active** account. |
| `/prevent` | `BLOCK_SIGNAL` | Block new signals (worker must honor it). |
| `/allow` | `ALLOW_SIGNAL` | Re-enable new signals. |

> `BLOCK_SIGNAL` / `ALLOW_SIGNAL` are scoped by `account_id` in the `AdminSignal`
> payload. Enforcement is the **worker's** responsibility — worker code lives
> outside this repo, so the bot/broker only publish the directive.

**Presentation** — list commands reply with monospace tables, and every
timestamp is rendered in the `notification_timezone` broker setting (read over
HTTP from `GET /admin/settings/notification-timezone`, since the bot has no DB
access, falling back to UTC+7). Details in
[`bot/README.md` → Rendering](bot/README.md#rendering).

Run it with the stack: `docker compose up -d bot`. See
[`bot/README.md`](bot/README.md) for the full command reference, configuration
and local development.

---

## 🗄️ PostgreSQL Schema

### `signals` table

| Column | Type | Description |
| ------------------ | ---------------- | --------------------------------------- |
| `id` | UUID (PK) | Unique record identifier |
| `strategy` | String(50) | Strategy name that generated the signal |
| `signal_uxid` | String(16) (Nullable) | Cycle correlator — 16-char lowercase-hex short uuid shared by every signal of one trade (entry + TPs + SL + FLAT). Ties a signal to its `broadcast_messages` row so the trade renders as one edited-in-place Telegram message. |
| `symbol` | String(50) | Trading symbol (e.g., XAUUSD) |
| `timeframe` | String(20) | Chart timeframe (e.g., M15) |
| `timestamp` | DateTime | Signal generation time from TradingView |
| `action` | Enum | LONG, SHORT, TP1, TP2, R_SL, SL, FLAT |
| `price` | Numeric(20,8) | Entry/trigger price |
| `quantity` | Numeric(20,8) | Lot size / volume |
| `sl`, `tp1`, `tp2` | Numeric(20,8) | Exit prices (nullable) |
| `is_running` | Boolean | Strategy active state |
| `risk_percent` | Numeric(10,4) | Risk percentage for position sizing |
| `is_scale_position` | Boolean | Whether this signal scales into an existing position |
| `scale_strategy` | String(50) (Nullable) | Scale-in strategy name (e.g. `add_on_pullback`) |
| `status` | Enum | Delivery state: `QUEUED` on insert, `PUBLISHED` after a successful fan-out, `FAILED` once every attempt has been exhausted |
| `attempts` | Integer | Remaining fan-out attempts (seeded from `settings.signal.MAX_ATTEMPTS`, default `3`). Decremented on failure; `0` marks the row `FAILED`. |
| `last_attempt` | DateTime (Nullable) | Timestamp of the most recent fan-out attempt (`NULL` before the first attempt). Drives the retry job's minimum-gap filter. |
| `indicators` | JSONB (Nullable) | Full technical indicator snapshot |
| `inputs` | JSONB (Nullable) | Strategy input parameters |
| `raw` | JSONB (Nullable) | Raw webhook payload |
| `createdAt` | DateTime | Broker log insertion time |

### `trades` table

| Column | Type | Description |
| ----------------------- | ------------ | ------------------------------------------ |
| `id` | UUID (PK) | Unique record identifier |
| `account_id` | String(50) | Worker's broker account ID |
| `market` | Enum (nullable) | `FOREX` or `CRYPTO`, copied from the owning `accounts` row |
| `gateway` | String(50) (nullable) | Exchange the account trades through, copied from the owning `accounts` row |
| `account_leverage` | Integer | Account leverage at time of trade |
| `account_balance_init` | Numeric(20,8) | Account balance before trade (nullable) |
| `account_balance` | Numeric(20,8) | Account balance after trade (nullable) |
| `strategy` | String(50) | Strategy that originated the signal |
| `strategy_code` | String(255) | Mapping between strategy and number (defined by Worker) |
| `ref_id` | String(255) | Worker's source position reference id (original entry; shared by all child executions; nullable) |
| `symbol` | String(50) | Trading symbol |
| `action` | Enum | LONG, SHORT, TP1, TP2, R_SL, SL, FLAT |
| `price` | Numeric(20,8) | Execution price |
| `quantity` | Numeric(20,8) | Lot size |
| `sl`, `tp1`, `tp2` | Numeric(20,8) | Exit prices (nullable) |
| `is_running` | Boolean | Strategy active state |
| `risk_percent` | Numeric(10,4) | Risk percentage used |
| `comment` | String(255) | Trade comment (nullable) |
| `gateway_return_code` | Integer | Return code from the exchange gateway (nullable) |
| `status` | Enum | OPENED, REJECTED, PARTIALLY_CLOSED, CLOSED, FLAT |
| `reject_reason` | String(255) | Reason if trade was rejected (nullable) |
| `createdAt` | DateTime | Record insertion time |
| `updatedAt` | DateTime | Last update time |

### `accounts` table

| Column | Type | Description |
| ------------------- | ------------ | ------------------------------------------ |
| `id` | UUID (PK) | Unique record identifier |
| `account_id` | String(50) | Worker's broker account ID (unique together with `market` + `gateway` — **not** unique alone; see note below) |
| `account_name` | String(255) | Display name of the account (nullable) |
| `account_balance` | Numeric(20,8) | Most recent account balance (nullable) |
| `market` | Enum | `FOREX` or `CRYPTO` |
| `gateway` | String(50) | Exchange the account trades through, e.g. `MT5`, `BINANCE` (nullable) |
| `last_activity_at` | DateTime | Timestamp of the last TRADE event received |
| `createdAt` | DateTime | Record insertion time |
| `updatedAt` | DateTime | Last update time |

**Unique constraint:** `(market, gateway, account_id)` — a bare `account_id` can exist under more than one market/gateway (two unrelated real accounts, e.g. an MT5 login and a Binance account, can coincidentally share a number). Endpoints and repository methods that take only `account_id` (`POST /admin/accounts/{account_id}/link-token/rotate`, `GET /v1/{account_id}/trades`, the `account_id` scope on `POST /admin/flat`) resolve/match on that bare id and can be ambiguous if it's reused across gateways — avoid deliberately reusing an `account_id` across gateways until those callers are updated to also pass `market`/`gateway`.

> The `accounts` table deliberately carries **no** bot/chat-platform columns: an
> account is a trading domain object. Who may drive it from a bot lives in
> `account_bot_links`, the invite secrets in `account_link_tokens`, and the
> per-user active selection in `bot_sessions`. All three are keyed by
> `platform` (`BotPlatformTypeEnum`, currently only `TELEGRAM`) so adding
> Discord/Slack is a new enum member, not a migration.

### `account_bot_links` table

Many-to-many join between accounts and chat-platform users: an account may be
managed by several people, and a person may hold several accounts. There is no
role/permission column — every linked user has the same rights.

| Column | Type | Description |
| ------- | ------------ | --------------------------------------- |
| `id` | UUID (PK) | Unique record identifier |
| `account_id` | UUID | FK → `accounts.id`, `ON DELETE CASCADE` |
| `platform` | Enum | `TELEGRAM` |
| `platform_user_id` | String(64) | The bot user's platform id. Stored as text, not a number: Telegram/Discord ids are numeric but Slack/Matrix ids are opaque strings |
| `createdAt` | DateTime | Record insertion time |
| `updatedAt` | DateTime | Last update time |

**Unique constraint:** `(platform, platform_user_id, account_id)`.

### `account_link_tokens` table

Invite secrets that let a bot user claim an account. Split out of `accounts` so
an account can have several outstanding tokens (invite two people with two
separately revocable secrets) and so revocation is a state change rather than an
overwrite.

| Column | Type | Description |
| ------- | ------------ | --------------------------------------- |
| `id` | UUID (PK) | Unique record identifier |
| `account_id` | UUID | FK → `accounts.id`, `ON DELETE CASCADE` |
| `token` | UUID | The bearer secret handed to the end-user (unique) |
| `expires_at` | DateTime (Nullable) | `NULL` = never expires. Nothing issues a deadline today |
| `revoked_at` | DateTime (Nullable) | Set by `/rotate` on every previously valid token |
| `last_used_at` | DateTime (Nullable) | Audit only — a token stays reusable after a successful link |
| `createdAt` | DateTime | Record insertion time |
| `updatedAt` | DateTime | Last update time |

A token is **valid** when `revoked_at IS NULL AND (expires_at IS NULL OR
expires_at > now())`. One is minted automatically whenever an account row is
created. Rotating revokes the old ones but never evicts anyone already linked —
a token only grants the initial claim.

### `bot_sessions` table

One row per `(platform, bot user)`, tracking which of their linked accounts is
currently **active** — the one every single-account bot command (`/status`,
`/trades`, `/flat`, `/prevent`, `/allow`, `/unlink`) acts on. Kept separate from
`account_bot_links` because "may drive" and "is currently driving" are different
facts: a user has N links but exactly one active selection.

| Column | Type | Description |
| ------- | ------------ | --------------------------------------- |
| `id` | UUID (PK) | Unique record identifier |
| `platform` | Enum | `TELEGRAM` |
| `platform_user_id` | String(64) | The bot user this session belongs to |
| `active_account_id` | UUID (Nullable) | FK → `accounts.id`, `ON DELETE SET NULL`. The active account, or `NULL` once the user has unlinked everything |
| `createdAt` | DateTime | Record insertion time |
| `updatedAt` | DateTime | Last update time |

**Unique constraint:** `(platform, platform_user_id)`.

The row is created on first link and updated by
`POST /v1/telegram/{telegram_user_id}/active-account` (the bot's `/switch`).
If `active_account_id` ever points at an account the user no longer holds a link
to, the broker falls back to their most recently active account and self-heals
the row on the next read.

### `trade_broadcast_subscriptions` table

One row per `(platform, bot user)` who has opted in — via the bot's `/subscribe`
— to a Telegram DM whenever one of their linked accounts **completes (closes) a
trade**. Unsubscribing (`/unsubscribe`) deletes the row. When a worker's `TRADE`
event ends a trade, the broker resolves the account's owners as the
intersection of `account_bot_links` (who is linked) and this table (who opted
in), then DMs each via the bot-service bot token (`BOT_TELEGRAM_TOKEN`) — the
bot the user actually started, since a bot can only message users who started
it. The opt-in spans every account the user holds, which is why it is a per-user
row here rather than a column on a link.

"Ends a trade" means the event's own status maps to a **terminal** trade status —
`CLOSED` (TP2 / SL / R_SL / TERMINAL_CLOSED / FORCED_CLOSED) or `FLAT` (an admin
`POST /admin/flat`). An admin FLAT counts because the position is over and the
owner did not close it themselves. Gating on the event's status rather than the
persisted row's keys the DM to the one discrete close event the worker emits, so
a later touch of an already-closed row does not fire a second one.

A `FLATTED` event reports `closed_price=0` when the worker has no close price to
give. No instrument closes at 0, so the broker treats it as missing and keeps the
open price rather than persisting — and DM-ing — a bogus `0`.

The DM's status line names the event that ended the trade in brackets —
`Status: CLOSED (TP2)`, `CLOSED (SL)`, `CLOSED (R_SL)`,
`CLOSED (TERMINAL_CLOSED)`, `CLOSED (FORCED_CLOSED)` — since five worker events
collapse onto the one `CLOSED`, while the DM's `Action` line stays the entry
direction (`LONG` / `SHORT`) the trade was opened with. The label is the `TRADE`
event's own status, with `FLATTED` shown as the `FLAT` it is; an admin FLAT
therefore reads `Status: FLAT`, not `FLAT (FLAT)`.

| Column | Type | Description |
| ------- | ------------ | --------------------------------------- |
| `id` | UUID (PK) | Unique record identifier |
| `platform` | Enum | `TELEGRAM` |
| `platform_user_id` | String(64) | The bot user opted in to broadcasts |
| `createdAt` | DateTime | Record insertion time |
| `updatedAt` | DateTime | Last update time |

**Unique constraint:** `(platform, platform_user_id)`.

### `broker_settings` table

| Column | Type | Description |
| ------- | ------------ | --------------------------------------- |
| `id` | UUID (PK) | Unique record identifier |
| `key` | String(255) | Setting key (see known keys below) |
| `value` | Text | Setting value (`"0"` / `"1"` for flags) |

**Known setting keys:**

| Key | Default | Admin endpoint | Description |
| --- | ------- | --------------- | ----------- |
| `signal_blocked` | `"0"` | `POST /admin/settings/block-signal` | Pause signal forwarding to workers |
| `silent_signal` | `"0"` | `POST /admin/settings/silent-signal` | Mute Telegram signal notifications |
| `notification_include_signal_raw` | `"0"` | `POST /admin/settings/include-signal-raw` | Append indicators/inputs to notifications |
| `crypto_allowed_symbol` | `"BTC,ETH"` | `POST /admin/settings/crypto-allowed-symbol` | Comma-separated list of crypto symbols pushed to workers via `SYSTEM.CRYPTO_LEVERAGE_INIT` |
| `crypto_max_leverage` | `"10"` | `POST /admin/settings/crypto-max-leverage` | Default leverage pushed to workers via `SYSTEM.CRYPTO_LEVERAGE_INIT` |
| `strategy_magic_map` | `'{"MT5_GOLD_M5_V1": 20260409, …}'` | `POST` / `GET /admin/settings/strategy-magic-map` | JSON-text strategy → magic-number map sent to every worker in its `WORKER_CONNECTED_ACK` on connect, filtered to the strategies it announces |
| `notification_timezone` | `"7"` | `POST` / `GET /admin/settings/notification-timezone` | UTC offset (hours) applied to every time the broker or bot displays — the `Time:` line of Telegram notifications and the bot's `/trades` table |
| `public_broadcast_chat_ids` | `""` | `POST` / `GET /admin/settings/public-broadcast-chat-ids` | Comma-separated chat ids (with the same topic suffix syntax as `TELEGRAM_PRIVATE_BROADCAST_CHAT_IDS`) that receive the **public** signal-cycle broadcast — the bare price/level/timeline copy, without strategy internals or the worker execution table. Editable at runtime from the admin API and the bot's `/admin_public_chats` command; empty turns the public broadcast off. |
| `max_retry_timeout` | `"60"` | — (edit directly) | Seconds of history included in the `retry_signals` replay sent to a freshly-connected worker |

### `broadcast_messages` table

One row per signal *cycle*: everything the broker has seen for one trade
(``strategy`` + ``signal_uxid``) rolled up into a single record. The row's
``events`` list — an ordered JSONB array of the actions in the order they
arrived — is what the Telegram message body is rendered from every time an
edit is due, so the operator sees the full timeline in one place instead of
one message per action.

| Column | Type | Description |
| ------- | ------------ | --------------------------------------- |
| `id` | UUID (PK) | Unique record identifier |
| `strategy` | String(50) | Strategy the cycle belongs to |
| `signal_uxid` | String(16) | 16-char lowercase-hex cycle correlator (matches `signals.signal_uxid`) |
| `symbol` | String(50) | Symbol carried on the entry signal |
| `timeframe` | String(20) (Nullable) | Chart timeframe carried on the entry signal |
| `status` | Enum | `RUNNING` until a closing action arrives (TP2, R_SL, SL, FLAT); then `CLOSED` |
| `events` | JSONB | Ordered list of stored action events — action, price/qty/levels, timestamp, attempt number, indicator/input dumps |
| `last_seq` | Integer | Sequence number of the most recent write-log entry against this cycle (see below) |
| `last_broadcast_at` | DateTime (Nullable) | Timestamp of the last successful dispatcher run for this cycle |
| `createdAt` / `updatedAt` | DateTime | Record insertion / last-update times |

**Unique constraint:** `(strategy, signal_uxid)` — the cycle key.

### `broadcast_message_chats` table

Per-chat delivery state for a cycle. Every chat the cycle addressed (private
audience from `TELEGRAM_PRIVATE_BROADCAST_CHAT_IDS`, public audience from the
`public_broadcast_chat_ids` broker setting) has its own row here, so the
same cycle can carry a different Telegram `message_id` in each chat and can
render a slightly different body per audience (the private copy carries the
strategy name, signal id, the worker execution table and the raw dump; the
public copy is the bare price/level/timeline body only).

| Column | Type | Description |
| ------- | ------------ | --------------------------------------- |
| `id` | UUID (PK) | Unique record identifier |
| `broadcast_message_id` | UUID | FK → `broadcast_messages.id`, `ON DELETE CASCADE` |
| `audience` | Enum | `PRIVATE` (operator) or `PUBLIC` (subscribers) |
| `chat_id` | String(255) | The setting entry the row represents — verbatim, so `-1002173777783_924584` (a group + topic id) is distinct from `-1002173777783_924585` |
| `message_id` | String(64) (Nullable) | Telegram `message_id` of the chat's cycle message; `NULL` until the first send succeeds |
| `message` | Text (Nullable) | Last body actually delivered to this chat — kept so the dispatcher can skip a no-op edit |
| `delivered_seq` | Integer | Highest `last_seq` this chat has been shown; a delivery whose seq is not greater is dropped so a slow edit cannot overwrite a newer body |
| `last_error` | String(500) (Nullable) | Last transient error seen when editing/sending, or `NULL` on success |
| `createdAt` / `updatedAt` | DateTime | Record insertion / last-update times |

**Unique constraint:** `(broadcast_message_id, chat_id)`.

### `broadcast_message_workers` table

Rows appended by the TRADE consumer for the *public* broadcast's execution
table (one line per worker per cycle, updated in place as the worker
progresses through the trade). Account ids are stored verbatim; the
formatter masks them to their last four characters when rendering
(`MT5 ****5678`) so the public channel never publishes anyone's full
account number.

| Column | Type | Description |
| ------- | ------------ | --------------------------------------- |
| `id` | UUID (PK) | Unique record identifier |
| `broadcast_message_id` | UUID | FK → `broadcast_messages.id`, `ON DELETE CASCADE` |
| `worker_id` | String(255) | `<market>-<gateway>-<account_id>` composed by `compose_worker_id` |
| `account_id` | String(50) | Worker's raw account id (masked at render time) |
| `market` | Enum (Nullable) | `FOREX` / `CRYPTO`, copied from the TRADE event or the persisted account |
| `gateway` | String(50) (Nullable) | Exchange the account trades through |
| `latest_status` | Enum | Last trade status seen from this worker for this cycle: `OPENED`, `PARTIALLY_CLOSED`, `CLOSED`, `FLAT`, `REJECTED` |
| `latest_action` | String(20) (Nullable) | Last raw action label the worker reported (e.g. `TP1`, `SL`, `R_SL`) |
| `reject_reason` | String(255) (Nullable) | Reason if the last event was a rejection |
| `createdAt` / `updatedAt` | DateTime | Record insertion / last-update times |

**Unique constraint:** `(broadcast_message_id, worker_id)`.

### `broadcast_message_logs` table

Append-only write log that turns a cycle change into a delivery. Every
recorded event and every worker execution appends one row here *in the same
transaction*, and a Postgres trigger fires `pg_notify` on the
`broadcast_message_logs` channel so `BroadcastDispatcher` can wake up and
edit the affected chats without polling. A sweeper re-drains the log on a
timer as a safety net for anything appended while the dispatcher was down.

| Column | Type | Description |
| ------- | ------------ | --------------------------------------- |
| `id` | UUID (PK) | Unique record identifier |
| `broadcast_message_id` | UUID | FK → `broadcast_messages.id`, `ON DELETE CASCADE` |
| `seq` | Integer | Per-cycle sequence handed out under a row lock; strictly increasing |
| `kind` | Enum | `SIGNAL_EVENT` (a signal folded in) or `WORKER_EXECUTION` (a TRADE event) |
| `payload` | JSONB | Copy of the event that was recorded — kept so the log is self-contained if the cycle is later purged |
| `status` | Enum | `PENDING` on insert; `SENDING` while the dispatcher claims it; `DELIVERED` on success; `FAILED` after the retry cap is reached |
| `attempts` | Integer | Delivery attempts spent on this row |
| `last_error` | String(500) (Nullable) | Last error seen from the notifier, `NULL` on success |
| `claimed_at` | DateTime (Nullable) | Set while the row is `SENDING` — a stale claim is reclaimed by the sweeper after 60 s |
| `createdAt` / `updatedAt` | DateTime | Record insertion / last-update times |

---

## 🧪 Testing

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
