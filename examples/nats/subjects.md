# NATS subjects & example payloads

Every subject the broker and its workers exchange messages on, with a runnable
JSON example for **every action that can occur** on each. Files are named
`<subject-group>.<action>.json`; open the file next to each row below.

Legend for **Dir**: `B→W` broker → workers · `W→B` workers → broker ·
`B→B` broker → broker (internal).

---

## 1. `{strategy}` — trade signals (B→W)

Each signal is published on the subject equal to its `strategy` field (e.g.
`MT5_GOLD_M5_V1`). Workers subscribe only to the strategies they handle.
Full-shape payloads are a `TradingSignal`; the FLAT directive is a lighter
payload sharing only `signal_id`/`strategy`/`timestamp`/`action`/`symbol`.
`action` is one of `SignalActionEnum`: `LONG`, `SHORT`, `TP1`, `TP2`, `R_SL`,
`SL`, `FLAT`.

| Action | Meaning | Example |
| ------ | ------- | ------- |
| `LONG` | Open a long entry | [`entry.long.json`](entry.long.json) |
| `SHORT` | Open a short entry | [`entry.short.json`](entry.short.json) |
| `LONG` (scale-in) | Add to an existing position (`is_scale_position` + `scaling` block) | [`entry.long.scale.json`](entry.long.scale.json) |
| `TP1` | First partial-close target | [`close.tp1.json`](close.tp1.json) |
| `TP2` | Second (final) close target | [`close.tp2.json`](close.tp2.json) |
| `R_SL` | Runner stop-loss | [`close.r_sl.json`](close.r_sl.json) |
| `SL` | Stop-loss | [`close.sl.json`](close.sl.json) |
| `FLAT` | Close-all directive for the strategy (lightweight payload) | [`close.flat.json`](close.flat.json) |

## 2. `ADMIN` / `ADMIN.<market>.<gateway>.<account_id>` — admin actions (B→W)

Payload is an `AdminSignal`; `action` is one of `AdminActionEnum`: `FLAT`,
`BLOCK_ENTRIES`, `ALLOW_ENTRIES`. **Routing depends on scope:**

* **Account-scoped** (`account_id` set — `market`/`gateway` required with it) →
  published to the **private** per-account subject
  `ADMIN.<market>.<gateway>.<account_id>` (e.g. `ADMIN.FOREX.MT5.123456`).
  Only that one account's worker is subscribed, so no other worker ever learns
  the `account_id`.
* **Broadcast** (no `account_id`) → published to the shared `ADMIN` subject and
  fanned out to every worker, which filters for itself.

| Action | Scope | Subject | Example |
| ------ | ----- | ------- | ------- |
| `FLAT` | one account | `ADMIN.FOREX.MT5.123456` (private) | [`admin.flat.json`](admin.flat.json) |
| `FLAT` | strategy/symbol | `ADMIN` (broadcast) | [`admin.flat.broadcast.json`](admin.flat.broadcast.json) |
| `FLAT` | everything | `ADMIN` (broadcast) | [`admin.flat.all.json`](admin.flat.all.json) |
| `BLOCK_ENTRIES` | one account | `ADMIN.FOREX.MT5.123456` (private) | [`admin.block_entries.json`](admin.block_entries.json) |
| `ALLOW_ENTRIES` | one account | `ADMIN.FOREX.MT5.123456` (private) | [`admin.allow_entries.json`](admin.allow_entries.json) |

## 3. `SYSTEM` — broker ⇄ worker configuration & handshake

`SystemSignal` subclasses; `action` is one of `SystemActionEnum`. The
handshake replies (`*_ACK`, `*_ERROR`, `RETRY_SIGNALS`) are usually sent on the
request's reply inbox rather than the shared `SYSTEM` subject.

| Action | Dir | Meaning | Example |
| ------ | --- | ------- | ------- |
| `WORKER_CONNECTED` | W→B | Worker announces itself on connect | [`system.worker_connected.json`](system.worker_connected.json) |
| `WORKER_CONNECTED_ACK` | B→W | Handshake accepted, no extra config | [`system.worker_connected_ack.json`](system.worker_connected_ack.json) |
| `WORKER_CONNECTED_ERROR` | B→W | Handshake received but config could not be built | [`system.worker_connected_error.json`](system.worker_connected_error.json) |
| `CRYPTO_LEVERAGE_INIT` | B→W | Push allowed symbols + max leverage to a crypto worker | [`system.crypto_leverage_init.json`](system.crypto_leverage_init.json) |
| `RETRY_SIGNALS` | B→W | Replay of recent signals to a reconnecting worker | [`system.retry_signals.json`](system.retry_signals.json) |

`account_id` on the SYSTEM subject is the worker id in
`<market>-<gateway>-<account_id>` form (e.g. `CRYPTO-BINANCE-7654321`).

## 4. `TRADE` — position events (W→B)

Payload is a `PositionEvent` published by a worker whenever a row in its local
`positions` table is inserted (`event: CREATED`) or updated (`event: UPDATED`).
`status` is the worker position status; the broker maps it onto a trade
lifecycle state (see `broker/domain/trade_status.py`).

| `event` | `status` | Broker trade status | Example |
| ------- | -------- | ------------------- | ------- |
| `CREATED` | `OPENED` | OPENED | [`trade.created.opened.json`](trade.created.opened.json) |
| `CREATED` | `REJECTED` | REJECTED (carries `reject_reason`) | [`trade.created.rejected.json`](trade.created.rejected.json) |
| `UPDATED` | `TP1` | PARTIALLY_CLOSED | [`trade.updated.tp1.json`](trade.updated.tp1.json) |
| `UPDATED` | `TP2` | CLOSED | [`trade.updated.tp2.json`](trade.updated.tp2.json) |
| `UPDATED` | `SL` | CLOSED | [`trade.updated.sl.json`](trade.updated.sl.json) |
| `UPDATED` | `R_SL` | CLOSED | [`trade.updated.r_sl.json`](trade.updated.r_sl.json) |
| `UPDATED` | `TERMINAL_CLOSED` | CLOSED | [`trade.updated.terminal_closed.json`](trade.updated.terminal_closed.json) |
| `UPDATED` | `FORCED_CLOSED` | CLOSED | [`trade.updated.forced_closed.json`](trade.updated.forced_closed.json) |
| `UPDATED` | `FLATTED` | FLAT | [`trade.updated.flatted.json`](trade.updated.flatted.json) |

## 5. `SIGNALS.<strategy>` — durable webhook buffer (B→B, JetStream)

The webhook endpoint enqueues the raw TradingView envelope onto the JetStream
stream `SIGNALS` (subject `SIGNALS.<strategy>`); the broker's own `SignalWorker`
consumes it and fans out to `{strategy}` above. The envelope wraps a
`WebhookPayload` under `payload`.

| Message | Example |
| ------- | ------- |
| Webhook envelope | [`signals.webhook_envelope.json`](signals.webhook_envelope.json) |
