# NATS subjects & example payloads

Every subject the broker and its workers exchange messages on, with a runnable
JSON example for **every action that can occur**. Files are named
`<subject-group>.<action>.json`; open the file linked next to each action.

The subjects are grouped by direction of flow:

- [**Broker → Worker**](#broker--worker) — signals, admin directives, and
  configuration the broker pushes down to workers.
- [**Worker → Broker**](#worker--broker) — position events and the connect
  handshake workers push up to the broker.
- [**Internal (Broker → Broker)**](#internal-broker--broker) — the durable
  JetStream webhook buffer the broker feeds itself.

`account_id` appears in two forms across these subjects: the **bare** id stored
in the `accounts` table (e.g. `123456`, used on `ADMIN`/`TRADE`) and the
**worker id** `<market>-<gateway>-<account_id>` (e.g. `CRYPTO-BINANCE-7654321`,
used on `SYSTEM`).

---

# Broker → Worker

## `{strategy}` — trade signals

Each signal is published on the subject **equal to its `strategy` field** (e.g.
`MT5_GOLD_M5_V1`) — there is one subject per strategy, and a worker subscribes
only to the strategies it handles, so it never sees another strategy's traffic.

- **Entry / target / stop payloads** are a full `TradingSignal`: `signal_id`,
  `timestamp`, `strategy`, `action`, `symbol`, `price`, `quantity`, plus the
  optional `sl` / `tp1` / `tp2` / `risk_percent` risk levels. A **scale-in**
  additionally sets `is_scale_position: true`, `scale_strategy`, and a `scaling`
  block (`tp` / `sl` / `quantity`) describing the add.
- **The FLAT directive** is a lighter payload carrying only `signal_id`,
  `strategy`, `timestamp`, `action`, `symbol` — no price/quantity, because it
  means "close everything on this strategy".

`signal_id` is the de-duplication key: a worker that sees a signal live and then
again inside a `SYSTEM.RETRY_SIGNALS` replay can drop the duplicate by id.
`action` is one of `SignalActionEnum`: `LONG`, `SHORT`, `TP1`, `TP2`, `R_SL`,
`SL`, `FLAT`.

| Action | Meaning | Example |
| ------ | ------- | ------- |
| `LONG` | Open a long entry | [`entry.long.json`](entry.long.json) |
| `SHORT` | Open a short entry | [`entry.short.json`](entry.short.json) |
| `LONG` (scale-in) | Add to an existing position (`is_scale_position` + `scaling` block) | [`entry.long.scale.json`](entry.long.scale.json) |
| `TP1` | First partial-close target hit | [`close.tp1.json`](close.tp1.json) |
| `TP2` | Second (final) close target hit | [`close.tp2.json`](close.tp2.json) |
| `R_SL` | Runner stop-loss (SL moved into profit) | [`close.r_sl.json`](close.r_sl.json) |
| `SL` | Stop-loss hit | [`close.sl.json`](close.sl.json) |
| `FLAT` | Close-all directive for the strategy (lightweight payload) | [`close.flat.json`](close.flat.json) |

## `ADMIN` / `ADMIN.<market>.<gateway>.<account_id>` — admin directives

Payload is an `AdminSignal` (`action`, `timestamp`, optional `strategy` /
`symbol` / `account_id` / `market` / `gateway`). `action` is one of
`AdminActionEnum`: `FLAT`, `BLOCK_ENTRIES`, `ALLOW_ENTRIES`.

**Routing depends on whether the directive is scoped to a single account:**

- **Account-scoped** — `account_id` is set, and `market` + `gateway` are
  **required** with it (an account id is only unique within a market/gateway
  pair). The directive is published to the **private** per-account subject
  `ADMIN.<market>.<gateway>.<account_id>` (e.g. `ADMIN.FOREX.MT5.123456`) that
  **only that one account's worker is subscribed to**. No other worker ever sees
  the message — nor the `account_id` — so each worker stays isolated to its own
  account.
- **Broadcast** — no `account_id` (a strategy/symbol-scoped or flat-everything
  directive). Published to the shared `ADMIN` subject and fanned out to **every**
  connected worker, which filters for itself client-side.

`BLOCK_ENTRIES` / `ALLOW_ENTRIES` are always account-scoped today (a user
toggling new-entry blocking for their own account), so they always go to the
private subject.

| Action | Scope | Subject | Example |
| ------ | ----- | ------- | ------- |
| `FLAT` | one account | `ADMIN.FOREX.MT5.123456` (private) | [`admin.flat.json`](admin.flat.json) |
| `FLAT` | strategy / symbol | `ADMIN` (broadcast) | [`admin.flat.broadcast.json`](admin.flat.broadcast.json) |
| `FLAT` | everything | `ADMIN` (broadcast) | [`admin.flat.all.json`](admin.flat.all.json) |
| `BLOCK_ENTRIES` | one account | `ADMIN.FOREX.MT5.123456` (private) | [`admin.block_entries.json`](admin.block_entries.json) |
| `ALLOW_ENTRIES` | one account | `ADMIN.FOREX.MT5.123456` (private) | [`admin.allow_entries.json`](admin.allow_entries.json) |

## `SYSTEM` — configuration & handshake replies (broker side)

The broker's outgoing half of the `SYSTEM` conversation. Each payload is a
`SystemSignal` subclass keyed by `action` (`SystemActionEnum`) and addressed to
a worker by its worker id (`account_id` in `<market>-<gateway>-<account_id>`
form). The three handshake replies below are normally sent on the request's
**reply inbox** (from the worker's NATS `request`) rather than the shared
`SYSTEM` subject, so they reach only the worker that asked.

| Action | Sent | Meaning | Example |
| ------ | ---- | ------- | ------- |
| `WORKER_CONNECTED_ACK` | reply inbox | Handshake accepted; no extra config needed (e.g. a non-crypto worker) | [`system.worker_connected_ack.json`](system.worker_connected_ack.json) |
| `WORKER_CONNECTED_ERROR` | reply inbox | Handshake received but the broker could not build the initial config (carries `reason`) | [`system.worker_connected_error.json`](system.worker_connected_error.json) |
| `CRYPTO_LEVERAGE_INIT` | reply inbox or `SYSTEM` | Push allowed crypto `symbols` + `default_leverage` to a crypto worker (on connect, or when an admin changes the setting) | [`system.crypto_leverage_init.json`](system.crypto_leverage_init.json) |
| `RETRY_SIGNALS` | reply inbox | Replay of every SIGNAL persisted in the last `max_retry_timeout` seconds for the strategies the worker announced, so a reconnecting worker catches up | [`system.retry_signals.json`](system.retry_signals.json) |

---

# Worker → Broker

## `TRADE` — position events

Payload is a `PositionEvent`, published by a worker whenever a row in its local
`positions` table is inserted (`event: CREATED`) or updated (`event: UPDATED`).
Besides the trade fields (`symbol`, `action`, `volume`, `opened_price`,
`closed_price`, `sl`/`tp1`/`tp2`, …) it carries an **account snapshot**
(`account_id`, `account_name`, `gateway`, `account_leverage`,
`account_balance`) the broker needs to create/upsert the trade and address the
worker later. `ref_id` is the gateway's own order/ticket reference; `signal_id`
ties the event back to the originating signal.

`status` is the **worker** position status; the broker maps it onto its own
trade lifecycle state via `broker/domain/trade_status.py` (shown in the last
column). A `REJECTED` event carries a `reject_reason` (e.g. the worker's MAX
ORDER limit was hit).

| `event` | `status` | → Broker trade status | Example |
| ------- | -------- | --------------------- | ------- |
| `CREATED` | `OPENED` | `OPENED` | [`trade.created.opened.json`](trade.created.opened.json) |
| `CREATED` | `REJECTED` | `REJECTED` (has `reject_reason`) | [`trade.created.rejected.json`](trade.created.rejected.json) |
| `UPDATED` | `TP1` | `PARTIALLY_CLOSED` | [`trade.updated.tp1.json`](trade.updated.tp1.json) |
| `UPDATED` | `TP2` | `CLOSED` | [`trade.updated.tp2.json`](trade.updated.tp2.json) |
| `UPDATED` | `SL` | `CLOSED` | [`trade.updated.sl.json`](trade.updated.sl.json) |
| `UPDATED` | `R_SL` | `CLOSED` | [`trade.updated.r_sl.json`](trade.updated.r_sl.json) |
| `UPDATED` | `TERMINAL_CLOSED` | `CLOSED` | [`trade.updated.terminal_closed.json`](trade.updated.terminal_closed.json) |
| `UPDATED` | `FORCED_CLOSED` | `CLOSED` | [`trade.updated.forced_closed.json`](trade.updated.forced_closed.json) |
| `UPDATED` | `FLATTED` | `FLAT` | [`trade.updated.flatted.json`](trade.updated.flatted.json) |

## `SYSTEM` — connect announcement (worker side)

The worker's outgoing half of the `SYSTEM` conversation. Right after it connects
to NATS the worker publishes a single `WORKER_CONNECTED` announcing itself and
requesting initial configuration. The broker replies on the request's reply
inbox with one of the `SYSTEM` messages in the Broker → Worker section above.

| Action | Meaning | Example |
| ------ | ------- | ------- |
| `WORKER_CONNECTED` | Worker announces its `account_id` (worker id), `market`, `gateway`, and the `strategies` it subscribes to; drives the `RETRY_SIGNALS` replay | [`system.worker_connected.json`](system.worker_connected.json) |

---

# Internal (Broker → Broker)

## `SIGNALS.<strategy>` — durable webhook buffer (JetStream)

Not exchanged with workers. The webhook endpoint (`POST /secret/webhook`)
enqueues the raw TradingView envelope onto the JetStream stream `SIGNALS`
(subject `SIGNALS.<strategy>`) and returns `202` immediately. The broker's own
`SignalWorker` then consumes each envelope and fans it out to the matching
`{strategy}` subject in the Broker → Worker section. The envelope wraps a
`WebhookPayload` (strategy, symbol, timeframe, the `position` block, and
optional `indicators` / `inputs`) under a `payload` key.

| Message | Example |
| ------- | ------- |
| Webhook envelope | [`signals.webhook_envelope.json`](signals.webhook_envelope.json) |
