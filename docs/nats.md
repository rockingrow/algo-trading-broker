# NATS Subjects

The broker uses **token-based authentication** with the NATS server. Workers must supply the same token when connecting.

| Direction | Subject | Purpose |
| --------- | ------- | ------- |
| Publish (broker → workers) | `{strategy}` | Signal routed to subscribers of that strategy (e.g. `wt_cross_v1`) |
| Publish (broker → workers) | `ADMIN` | Broadcast administrative messages (no `account_id`) — every worker receives and filters for itself |
| Publish (broker → one worker) | `ADMIN.<market>.<gateway>.<account_id>` | Account-scoped administrative message on a private per-account subject; only that account's worker is subscribed, so no other worker learns the `account_id` |
| Publish (broker → workers) | `SYSTEM` | The `WORKER_CONNECTED_ACK` answering a worker's announcement (magic map, signal replay, crypto leverage), plus the `CRYPTO_LEVERAGE_INIT` pushed to already-connected workers on an admin change |
| Publish (broker → broker) | `SIGNALS.<strategy>` (JetStream stream `SIGNALS`) | Durable webhook envelope buffer — the webhook endpoint enqueues here, the broker's own `SignalWorker` consumes and fans out to `{strategy}` |
| Subscribe (producer → broker) | `SIGNALS_DEMO.<strategy>` (core NATS, no stream) | The end-to-end drill: the same webhook envelope, rendered to Telegram by `DemoSignalConsumer` and **never fanned out to a worker**. A separate first token, because the `SIGNALS` stream captures `SIGNALS.>` — see [`examples/nats/subjects.md`](../examples/nats/subjects.md) |
| Subscribe (workers → broker) | `TRADE` | Position events reported by workers after execution |
| Subscribe (workers → broker) | `SYSTEM` | `WORKER_CONNECTED` announcements published by a worker right after it connects (payload carries `account_id` in `<market>-<gateway>-<account_id>` format, plus `market`, `gateway`, and `strategies`) |

Each signal is published to the subject that matches its `strategy` field. Workers subscribe only to the strategies they handle, eliminating cross-strategy noise.

Every payload on `{strategy}` — whether it's a full `TradingSignal` (LONG/SHORT/TP/…) or the shorter FLAT directive — carries a `signal_id`. That is the same id the broker uses inside the handshake's `retry_signals` replay, so a worker that sees a signal live and then again as part of a reconnect replay can de-duplicate by `signal_id`.

## `TRADE` events

Workers publish a `TRADE` message (a `PositionEvent`) whenever a row in their local `positions` table is inserted or updated. The broker upserts it into the `trades` table keyed by `(market, gateway, account_id, ref_id)` — not `account_id` alone, since the same bare `account_id` can exist under a different market/gateway (see [`accounts` table](database.md#accounts-table)) — translating the worker's position status into a broker trade `status`:

| Worker position status | Broker trade `status` | Running? |
| ---------------------- | --------------------- | -------- |
| `OPENED` | `OPENED` | Yes |
| `TP1` | `PARTIALLY_CLOSED` | Yes |
| `TP2`, `SL`, `R_SL`, `TERMINAL_CLOSED`, `FORCED_CLOSED` | `CLOSED` | No |
| `FLATTED` | `FLAT` | No |
| `REJECTED` | `REJECTED` | No |

**`REJECTED`** is emitted when a worker refuses to place an order — for example when its **MAX ORDER** limit is reached. The worker still records the order in its own database and fires the `TRADE` event, so the broker persists a terminal, non-running trade carrying the worker's `reject_reason` (e.g. `"MAX ORDER limit reached"`). `REJECTED` ranks below every other status, so an event for an already-existing `ref_id` is treated as a lifecycle downgrade and ignored — only a brand-new order is recorded as rejected.

## JetStream signal pipeline

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

## `SYSTEM` handshake

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

### Request/reply (recommended)

Workers should announce themselves with **NATS request/reply** (`nc.request(...)`) rather than a fire-and-forget publish. The broker replies **directly on the request's inbox** with the outcome of the handshake, so:

- the reply reaches only the worker that asked (no fan-out to every `SYSTEM` subscriber), and
- the worker's `request` **always resolves** — on success or on an error — instead of hanging.

Because the reply is worker-driven, a worker that connects while the broker is **down or restarting** simply **times out and retries**; the handshake is idempotent, so retries are safe. This closes the delivery gap of plain fire-and-forget pub/sub, where a `WORKER_CONNECTED` published before the broker's subscription was active would be lost silently.

**One handshake, one reply.** A NATS reply inbox accepts a *single* message: `request()` resolves its future (or, in the `old_style` form, auto-unsubscribes at `max_msgs=1`) on the first reply and silently drops everything after it. So the broker answers with exactly one of two actions, and the successful one carries the worker's **whole** initial configuration:

| Situation | Reply action | Payload |
| --------- | ------------ | ------- |
| Config loaded | `WORKER_CONNECTED_ACK` | `strategy_magic_map`, `retry_signals`, `settings`, and `crypto_leverage_init` (crypto only) |
| Crypto settings missing/invalid | `WORKER_CONNECTED_ERROR` | `reason` |

```json
{
  "action": "WORKER_CONNECTED_ACK",
  "account_id": "CRYPTO-BINANCE-7654321",
  "timestamp": "2026-06-30T00:00:00+00:00",
  "strategy_magic_map": { "wt_cross_v1": 20260617 },
  "retry_signals": [],
  "settings": { "signal_blocked": true, "max_open_orders": 3 },
  "crypto_leverage_init": {
    "symbols": ["BTC", "ETH"],
    "default_leverage": 10
  }
}
```

The four blocks are always present, so a worker can parse them unconditionally:

- **`strategy_magic_map`** — the strategy → magic-number map, see [Strategy magic map](#strategy-magic-map) below. `{}` when nothing matched.
- **`retry_signals`** — the catch-up replay, see [Signal replay on reconnect](#signal-replay-on-reconnect) below. `[]` when there is nothing to replay.
- **`settings`** — the account's own settings, see [Account settings](#account-settings) below. Always complete: an account that has never run a command gets the defaults.
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

### Strategy magic map

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

### Account settings

The `settings` block of the ACK carries what the account's **owner** set from the bot — as opposed to the broker-wide `BrokerSetting` rows behind `strategy_magic_map` and `crypto_leverage_init`. It is the `accounts.settings` JSONB column of that one account, so each worker only ever sees its own.

```json
{
  "settings": {
    "signal_blocked": true,
    "max_open_orders": 3
  }
}
```

| Key | Type | Set by | Meaning |
| --- | ---- | ------ | ------- |
| `signal_blocked` | bool (default `false`) | bot `/prevent` → `true`, `/allow` → `false` | The owner has blocked new entries for this account. Same state the `BLOCK_SIGNAL` / `ALLOW_SIGNAL` ADMIN messages push live |
| `max_open_orders` | int (never `null` on the wire) | bot `/maxorders`, else the `max_open_orders` `BrokerSetting` | How many positions the account may hold at once; `0` disables the cap. This is what the worker enforces as its `MAX_OPEN_ORDERS` limit |

**`max_open_orders` has two scopes, and the worker sees one.** The owner's own
value lives in `accounts.settings`; when they have set none, the account
inherits the broker-wide `max_open_orders` `BrokerSetting` (default `5` when
that row is unset or unreadable). The broker resolves the two **before** it
replies, so the ACK always carries a concrete number and a worker never has to
know a global default exists. On the worker side this replaces its old
`MAX_OPEN_ORDERS` env var, which no longer exists: a worker falls back to its
own built-in default of `5` only if the broker sends no value at all.

Both scopes are editable at runtime, each by whoever owns it:

| Scope | Edited by | Applies to |
| ----- | --------- | ---------- |
| Account | bot `/maxorders`, or `POST /v1/telegram/{id}/settings/max-open-orders` | That one account. Sending `null` clears the override and goes back to inheriting |
| Broker-wide | bot `/admin_max_orders`, or `POST /admin/settings/max-open-orders` | Every account that has **not** set its own; the ones that did keep theirs |

A change does not wait for the worker to reconnect: both endpoints push the
account's resolved `settings` block to the worker(s) concerned as a SYSTEM
`WORKER_SETTINGS_UPDATE` — the same block, the same values and the same
worker-side code path as the ACK's, so the live push and the next handshake can
never disagree. The push is best-effort (an account with no `gateway` recorded
yet has no worker id to address, and a NATS failure is logged, not surfaced):
the stored value is the contract, and the next handshake re-delivers it.

**Why it is in the handshake at all.** A command like `/prevent` publishes an ADMIN message, which only reaches a worker that is connected *right now*. Before this block existed, a worker that restarted came back **unblocked** — it had no way to learn that its owner had stopped it while it was away. `POST /v1/telegram/{id}/commands/prevent` now writes `signal_blocked` to the account **before** publishing, and every handshake replays it: the ADMIN message is the live push, the column is the durable state, and a worker reconciles to it on every connect.

Enforcement lives in the worker — the broker records and reports the setting, it does not filter signals per account on its behalf.

The block is read fresh from the row on every handshake (deliberately **not** cached like the broker-wide settings, which are per-broker and shared by every worker): a command run between two connects must reach the second one. Unknown keys in the column are dropped from the payload, so a worker only ever receives keys this broker version knows — they are not erased from the row, since writes merge (`settings || patch`) rather than replace.

Adding a setting is a field on `AccountSettings` (`broker/schemas/account_schema.py`) plus its key in `broker/constants.py`, written from the command's endpoint — no migration, the column is a blob. A setting that also has a broker-wide default resolves in `broker/services/account_settings_service.py`, which is the single place both the handshake and the live push go through.

### Fire-and-forget (backward compatible)

A worker may still `publish` `WORKER_CONNECTED` without a reply inbox. In that case the broker broadcasts the same `WORKER_CONNECTED_ACK` on the shared `SYSTEM` subject and every worker filters by `account_id`; error outcomes can only be logged, not signalled back. Request/reply is preferred precisely because it removes that blind spot and keeps one worker's configuration out of every other worker's inbox.

The broker filters its own outgoing `SYSTEM` actions (`CRYPTO_LEVERAGE_INIT`, `WORKER_CONNECTED_ACK`, `WORKER_CONNECTED_ERROR`) by `action`, so it never reacts to its own messages.

### Signal replay on reconnect

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
      "risk_percent": 1.0,
      "use_equity_sizing": true
    }
  ]
}
```

### Live config push on admin update

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

The same pattern covers the account `settings` block, for both markets. A change
to `max_open_orders` — by its owner from the bot's `/maxorders`
(`POST /v1/telegram/{id}/settings/max-open-orders`) or by an admin changing the
broker-wide default (`POST /admin/settings/max-open-orders`) — publishes a
`WORKER_SETTINGS_UPDATE` carrying that account's **resolved** block:

```json
{
  "action": "WORKER_SETTINGS_UPDATE",
  "account_id": "FOREX-MT5-12345678",
  "timestamp": "2026-06-30T00:05:00+00:00",
  "settings": { "signal_blocked": false, "max_open_orders": 3 }
}
```

The whole block goes out, not just the field that changed: the worker applies it
through the very same code path as the ACK's `settings`, which reads every
setting it knows. An account-scoped change pushes to that one account; a change
to the broker-wide default pushes one message per account, each carrying the
value *that* account resolves to — so an account with its own cap receives the
value it already had rather than the new default. Best-effort on the same terms
as above.
