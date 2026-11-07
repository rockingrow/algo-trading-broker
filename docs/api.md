# API

## Interactive docs (Swagger / OpenAPI)

FastAPI auto-generates interactive API documentation. With the server running:

| Page | URL | Notes |
| ---- | --- | ----- |
| Swagger UI | `http://localhost:80/docs` | Try endpoints; click **Authorize** to set `X-API-KEY`. |
| ReDoc | `http://localhost:80/redoc` | Read-only reference. |
| OpenAPI schema | `http://localhost:80/openapi.json` | Raw spec. |

Set `DOCS_ENABLED=false` in `.env` to disable all three in production.

## URL Prefixes

All routes are grouped under versioned or purpose-scoped prefixes:

| Prefix | Router | Description |
| ------ | ------ | ----------- |
| `/v1` | API | Public API endpoints (accounts, trades, health) |
| `/admin` | Admin | Management endpoints (settings, trading actions) |
| `/secret` | Webhook | Signal webhook receiver (TradingView, QTE) |

If `BROKER_API_PREFIX` is set (e.g. `abc123xyz`), every route is mounted under that secret segment:

```text
/abc123xyz/v1/health
/abc123xyz/v1/accounts
/abc123xyz/admin/flat
/abc123xyz/secret/webhook
```

The prefix acts as a URL secret — an attacker who knows the IP or domain still cannot enumerate endpoints without it. Leave blank to use the default paths.

## Authentication

Management endpoints require an API key passed in the `X-API-KEY` header, validated against `BROKER_API_KEY`:

```bash
curl http://localhost:80/v1/accounts -H "X-API-KEY: $BROKER_API_KEY"
```

Missing or invalid keys return `401 Unauthorized`. If `BROKER_API_KEY` is unset, protected endpoints return `500`. The `/v1/health` and `/secret/webhook` endpoints are **not** key-protected (`/secret/webhook` uses its own in-payload `token`).

| Endpoint | Auth |
| -------- | ---- |
| `GET /v1/health` | None |
| `POST /secret/webhook` | In-payload `token` (must equal `WEBHOOK_SECRET`) |
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
| `GET /v1/telegram/{telegram_user_id}/trades/{trade_id}` | `X-API-KEY` |
| `POST /v1/telegram/{telegram_user_id}/trades/{trade_id}/exit` | `X-API-KEY` |
| `GET /v1/telegram/{telegram_user_id}/positions` | `X-API-KEY` |
| `POST /v1/telegram/{telegram_user_id}/commands/flat` | `X-API-KEY` |
| `POST /v1/telegram/{telegram_user_id}/commands/prevent` | `X-API-KEY` |
| `POST /v1/telegram/{telegram_user_id}/unlink` | `X-API-KEY` |
| `GET /v1/telegram/{telegram_user_id}/broadcast` | `X-API-KEY` |
| `POST /v1/telegram/{telegram_user_id}/broadcast/subscribe` | `X-API-KEY` |
| `POST /v1/telegram/{telegram_user_id}/broadcast/unsubscribe` | `X-API-KEY` |
| `POST /v1/telegram/{telegram_user_id}/broadcast/subscribe-all` | `X-API-KEY` |
| `POST /v1/telegram/{telegram_user_id}/broadcast/unsubscribe-all` | `X-API-KEY` |

The three `…/broadcast` endpoints act on the caller's **active account** (the one
`POST …/active-account` selects), like `/flat` or `/maxorders`: the live trade
card opt-in is per account, so `subscribe` on account A leaves account B alone.
They answer `404` when the user has no linked account. Each response carries
`subscribed` plus the account it was read from or written to —
`account_id`, `market`, `gateway` — because the caller may have switched
accounts since it last resolved one.

`…/broadcast/subscribe-all` and `…/broadcast/unsubscribe-all` are the bulk form
(the bot's `/subscribeall` and `/unsubscribeall`): one `UPDATE` of
`trade_cards_enabled` over **every** `account_bot_links` row the caller holds, so
they need no active account. They answer `{"subscribed": bool, "accounts": n}`,
where `n` is how many links the switch covered; `404` when the caller holds none,
`500` when the write failed. A link made afterwards starts off — the bulk switch
is not a standing preference.

---

## GET `/v1/health`

Returns `{"status": "ok"}`. No authentication required.

---

## POST `/secret/webhook`

Receives signals from **TradingView** and from **[Quant-Trading-Engine](https://github.com/rockingrow/quant-trading-engine)** — one endpoint, one payload schema, one `token`, no per-producer branch anywhere downstream. Verifies the in-payload `token` against `WEBHOOK_SECRET` (constant-time; `500` when the secret is not configured, `401` when it does not match) and pushes the raw envelope onto the JetStream `SIGNALS` stream (`SIGNALS.<strategy>`). Responds `202 Accepted` (`status=queued`) as soon as JetStream ack-s the write. Everything else — DB persist, block gate, publish to the `{strategy}` subject, Telegram notification, retries — runs from the background `SignalWorker` and, on failure, the periodic `SignalRetryJob`.

The wait for that ack is capped at `WEBHOOK_ENQUEUE_TIMEOUT`: past it the response is still `202`, with `status=deferred`, and the enqueue is retried in the background (see [JetStream signal pipeline](nats.md#jetstream-signal-pipeline)). `503` means the enqueue failed *and* could not be deferred — the signal was dropped.

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
    "use_equity_sizing": true,
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
whole trade to one edited-in-place Telegram broadcast message (each new
action also replying a two-line notice under it — see
[Broadcast update notices](telegram.md#broadcast-update-notices-the-reply-under-an-edited-message)).
A payload
that omits it or sends any other shape is rejected with `422`. The
per-signal `signal_id` the broker mints on persist is still unique per
alert (workers keep deduping on it) — the two ids serve different jobs and
both travel on every downstream NATS payload.

**Several cycles on one symbol + strategy at the same time:** a producer may
run more than one trade on the same `symbol` + `strategy` concurrently. Each
one mints its own `signal_uxid` and carries the full action trail from entry to
exit under that id; the broker keys a cycle on `(symbol, strategy,
signal_uxid)`, so the concurrent trades stay separate records and separate
Telegram messages, and a `TP1` belonging to one of them never lands on
another. Nothing else about the payload changes — the producer only has to keep
one `signal_uxid` per trade and not reuse it for a second, overlapping trade on
the same symbol and strategy.

### Position fields — `tp1`/`tp2`/`sl` vs `scaling`

A TradingView strategy can contain **multiple sub-strategies** running under the same parent strategy name. Each sub-strategy may apply different risk/reward profiles to the same signal — for example, a `LOW_RR_TIER` sub-strategy is designed to catch entries more frequently but accepts a tighter TP and higher relative risk, which means the effective TP, SL, and quantity differ from the base signal values.

To support this, the `position` block carries two sets of exit levels:

| Field | Purpose |
| ----- | ------- |
| `tp1`, `tp2`, `sl` | Base levels from the **primary** strategy logic — always present. |
| `use_equity_sizing` | `true` when the worker should size the position off account **equity** (balance + floating P/L) instead of the plain balance. Optional — omitted (or `null`) leaves the worker's own default in place. Forwarded as-is on the NATS `SIGNAL` payload. |
| `is_scale_position` | `true` when a sub-strategy wants to **scale into** an existing open position rather than open a new one. |
| `scale_strategy` | Name of the sub-strategy that triggered the scale-in (e.g. `LOW_RR_TIER`). Lets workers apply sub-strategy-specific position sizing or risk rules. |
| `scaling.tp`, `scaling.sl`, `scaling.quantity` | **Override** levels and size for the scale-in leg. These replace `tp1`/`sl`/`quantity` for the additional entry — they are forwarded on the NATS `SIGNAL` payload only when `is_scale_position` is `true`. |

**Example flow:** the main strategy fires a `LONG` signal with `tp1=1950, sl=1890`. At the same bar, the embedded `LOW_RR_TIER` sub-strategy decides to add to the position with a tighter target (`tp=1925`) and smaller size (`quantity=0.05`). The webhook sets `is_scale_position=true`, `scale_strategy="LOW_RR_TIER"`, and populates the `scaling` block accordingly. Workers that receive the signal can read `scale_strategy` to decide whether to apply the scale-in and by how much.

---

## GET `/v1/accounts`

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

## POST `/admin/accounts`

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

## GET `/v1/{account_id}/trades`

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

## POST `/admin/settings/block-signal`

Toggles the `SIGNAL_BLOCKED` broker setting between `"1"` (signals blocked) and `"0"` (signals forwarded). Requires the `X-API-KEY` header. Does not require a restart. Sends a Telegram notification on change.

---

## POST `/admin/settings/silent-signal`

Toggles the `SILENT_SIGNAL` broker setting between `"1"` (Telegram notifications muted) and `"0"` (notifications active). Useful for pausing alerts without disabling Telegram entirely. Requires the `X-API-KEY` header.

---

## POST `/admin/settings/include-signal-raw`

Toggles the `NOTIFICATION_INCLUDE_SIGNAL_RAW` setting. When enabled (`"1"`), Telegram signal notifications include the full `indicators` and `inputs` blocks. Requires the `X-API-KEY` header.

---

## POST `/admin/settings/crypto-allowed-symbol`

Sets the `crypto_allowed_symbol` broker setting pushed to crypto workers via `SYSTEM.CRYPTO_LEVERAGE_INIT`. Requires the `X-API-KEY` header.

**Request Body:**

```json
{
  "symbols": ["BTC", "ETH"]
}
```

Symbols are upper-cased, trimmed, and de-duplicated before being stored as a comma-separated string. At least one non-empty symbol is required (`422` otherwise).

On success the broker also **pushes** a targeted `SYSTEM.CRYPTO_LEVERAGE_INIT` to each crypto account by its `<market>-<gateway>-<account_id>` worker id (see [Live config push on admin update](nats.md#live-config-push-on-admin-update)), so already-running workers apply the new list immediately. That message also carries `crypto_max_leverage` read from the DB, so it is skipped (and logged) until that setting is configured. A worker that *connects* right after this call may still read the previous value from `SystemEventConsumer`'s up-to-30s cache.

---

## POST `/admin/settings/crypto-max-leverage`

Sets the `crypto_max_leverage` broker setting pushed to crypto workers via `SYSTEM.CRYPTO_LEVERAGE_INIT`. Requires the `X-API-KEY` header.

**Request Body:**

```json
{
  "default_leverage": 10
}
```

`default_leverage` must be a positive integer (`422` otherwise).

On success the broker also **pushes** a targeted `SYSTEM.CRYPTO_LEVERAGE_INIT` to each crypto account by its `<market>-<gateway>-<account_id>` worker id (see [Live config push on admin update](nats.md#live-config-push-on-admin-update)), so already-running workers apply the new leverage immediately. That message also carries `crypto_allowed_symbol` read from the DB, so it is skipped (and logged) until that setting is configured. A worker that *connects* right after this call is still subject to the same up-to-30s cache as `crypto-allowed-symbol`.

---

## GET `/admin/settings/max-open-orders`

Returns the broker-wide `max_open_orders` default — the cap an account inherits when its owner has set none of their own. Requires the `X-API-KEY` header.

```json
{
  "setting": "max_open_orders",
  "value": "5"
}
```

`value` is the number the broker actually resolves to, so an unset or unreadable row reads as the built-in default (`5`) rather than an empty string.

---

## POST `/admin/settings/max-open-orders`

Sets that default. Requires the `X-API-KEY` header.

**Request Body:**

```json
{
  "max_open_orders": 5
}
```

`max_open_orders` must be `0` or more (`422` otherwise); `0` disables the cap for every account that inherits it.

> **⚠️ Seed this before rolling out the worker release that reads it.** The setting is unset on an existing broker, so it resolves to `5` — while the matching worker release stops reading its own `MAX_OPEN_ORDERS` env var. A worker host bootstrapped with `make init` carries `MAX_OPEN_ORDERS=0` from the old `.env.example`, i.e. **uncapped**, so the upgrade would silently start rejecting its entries beyond the 5th. POST the value the fleet is on today (`0` to stay uncapped) and the upgrade is a no-op; tighten the cap deliberately afterwards.

This is the default to **inherit**, so it changes nothing for an account whose owner set their own cap with the bot's `/maxorders` — theirs wins until they clear it. On success the broker pushes a `SYSTEM.WORKER_SETTINGS_UPDATE` carrying each account's *resolved* `settings` block, addressed by its `<market>-<gateway>-<account_id>` worker id, so already-running workers apply the new cap immediately instead of at their next handshake. An account with no `gateway` recorded yet cannot be addressed and is skipped (and logged) — it reads the value from its first handshake, which is also what records the gateway.

---

## GET `/v1/telegram/{telegram_user_id}/settings/max-open-orders`

The cap in force for the caller's **active** account, and where it came from. Requires the `X-API-KEY` header (the bot is the only caller).

```json
{
  "max_open_orders": 3,
  "source": "account",
  "global_max_open_orders": 5
}
```

`source` is `"account"` when the owner set this value and `"global"` when it is inherited — the same number means two different things, and only the first is theirs to clear. `404` when the Telegram user has no linked account.

---

## POST `/v1/telegram/{telegram_user_id}/settings/max-open-orders`

Sets the owner's own cap for that account. Requires the `X-API-KEY` header.

**Request Body:**

```json
{
  "max_open_orders": 3
}
```

| Value | Effect |
| ----- | ------ |
| `3` (any int ≥ 0) | That account is capped at 3 open positions |
| `0` | The cap is disabled for that account |
| `null` | The override is cleared — the account goes back to inheriting the broker-wide default |

A negative value is `422`. The response is the same shape as the `GET`, reporting the effective value after the write.

The value is **persisted before it is published**, like `/commands/prevent`: `accounts.settings` is what every handshake replays, so a DB failure fails the call outright (`500`, nothing published), while a failed push still leaves the intent recorded for the worker to pick up when it reconnects. On success the broker pushes the account's resolved `settings` block as a `SYSTEM.WORKER_SETTINGS_UPDATE` so a running worker applies it right away.

---

## GET `/admin/settings/strategy-magic-map`

Returns the current `strategy_magic_map` broker setting as JSON text (`{}` when unset). Requires the `X-API-KEY` header.

**Response Body:**

```json
{
  "setting": "strategy_magic_map",
  "value": "{\"MT5_GOLD_M5_V1\": 20260409, \"SIDEWAY_M15_V1\": 20260617}"
}
```

---

## POST `/admin/settings/strategy-magic-map`

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

## POST `/admin/settings/notification-timezone`

Sets the `notification_timezone` broker setting: the UTC offset (in hours) applied to the `Time:` line of Telegram notifications. Requires the `X-API-KEY` header.

**Request Body:**

```json
{
  "utc_offset_hours": 7
}
```

`utc_offset_hours` must be between `-12` and `14` (`422` otherwise). Signal timestamps are normalised to UTC first, then shifted by this offset before formatting, e.g. `Time: 2026-07-06 12:55:00 (UTC+7)`. Defaults to `7` (UTC+7) when unset.

---

## GET `/admin/settings/notification-timezone`

Reads the current `notification_timezone` offset. Requires the `X-API-KEY` header.

**Response:**

```json
{
  "setting": "notification_timezone",
  "value": "7"
}
```

Returns the default `"7"` when the setting is unset or holds an unparseable value, so the caller always gets the offset actually in effect. This is what lets the [Telegram bot](telegram.md) render times in the same zone as broker-sent notifications — the bot talks only to the HTTP API and never reads `broker_settings` itself.

---

## POST `/admin/flat`

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

When `account_id` is set, `market` and `gateway` are **required** with it (422 otherwise) — since `account_id` is no longer globally unique (see [`accounts` table](database.md#accounts-table)), all three together identify one account. The FLAT is then published to the private subject `ADMIN.<market>.<gateway>.<account_id>` that **only that account's worker** is subscribed to, so no other worker ever sees the `account_id` and each worker stays isolated to its own account. Omitting `account_id` (a strategy/symbol-scoped or flat-everything directive) broadcasts on the shared `ADMIN` subject to **every** connected worker, which filters for itself client-side (worker-side code, outside this repo).
