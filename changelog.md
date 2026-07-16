# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.8] - Unreleased

### Fixed

- **TradingView webhook: `server closed the connection unexpectedly`** — The
  `/secret/webhook` response now carries `Connection: close`, forcing
  TradingView to open a fresh TCP connection per alert. Previously, when the
  gap between alerts on a strategy (e.g. 15-minute timeframe) exceeded
  `WEBHOOK_KEEPALIVE_TIMEOUT` (120s default), TradingView reused a socket
  that uvicorn had already closed and the delivery failed. Simply raising the
  keep-alive timeout only postponed the race; signalling close per response
  removes it. Costs one TCP handshake per alert, negligible at TradingView's
  alert cadence.

### Added

- **`REJECTED` trade status** — Workers now emit a `TRADE` (`PositionEvent`) with
  `status: "REJECTED"` when they refuse to place an order, e.g. when their
  **MAX ORDER** limit is reached. The order is still persisted worker-side, so
  the broker records a terminal, non-running trade instead of dropping the event
  as an unknown status. `TradeStatusPolicy` maps `REJECTED` →
  `TradeStatusEnum.REJECTED` (ranked below every other status, so it never
  overwrites an existing trade for the same `ref_id`), and `is_running` is set
  to `false`.
- **`reject_reason` on the `TRADE` (`PositionEvent`) payload** — Optional field
  carrying why the worker rejected the order (e.g. `"MAX ORDER limit reached"`).
  `upsert_by_position_event` persists it onto the `trades.reject_reason` column
  on both insert and update.

## [1.0.7] - 2026-07-15

### Added

- **NATS JetStream ingestion for webhook events** — The `POST /secret/webhook`
  handler now only verifies the token, checks the block gate, persists the
  signal row (`status=QUEUED`) and pushes a durable envelope onto the
  JetStream stream `SIGNALS` (subject `SIGNALS.<strategy>`). A new background
  `SignalWorker` (`broker/services/signal_worker.py`) is a durable pull
  consumer (`broker_signal_handler`) that fetches envelopes, runs the
  parse/publish/notify pipeline in its own task, flips the row to `PUBLISHED`,
  and acks — or NAKs on failure so JetStream redelivers. TradingView therefore
  gets its `202` as soon as the message is durably queued, closing the
  `Webhook delivery failed — server closed the connection unexpectedly`
  failure mode caused by holding the HTTP request open across the fan-out.
  Docker-compose now runs `nats-server` with `-js -sd /data/jetstream` and a
  named `nats_data` volume so the stream survives restarts.
- **`signals.status` column** — Added via Alembic migration `a5e6f7b8c9d0`.
  Enum (`QUEUED`, `PUBLISHED`) that records where each webhook signal is in
  the JetStream pipeline. Stuck `QUEUED` rows surface signals JetStream failed
  to deliver so an operator can audit them without re-reading raw broker logs.
- **`WORKER_CONNECTED.strategies` field** — `SystemWorkerConnectedSignal` now
  carries a list of strategy subjects the worker subscribes to. Drives the
  new `SYSTEM.RETRY_SIGNAL` replay so the broker only replays signals the
  worker actually consumes.
- **`SYSTEM.RETRY_SIGNAL` action** — On every `WORKER_CONNECTED` handshake (in
  addition to the existing `CRYPTO_LEVERAGE_INIT` / `WORKER_CONNECTED_ACK`
  reply), the broker sends a `RETRY_SIGNAL` back to the worker: every signal
  persisted in the last `max_retry_timeout` seconds whose strategy the worker
  announced. Payload is a list of the same objects normally published on the
  strategy subject, so the worker can replay them through the same handler.
  Delivered on the request's reply inbox when the handshake used NATS
  request/reply, otherwise broadcast on the shared `SYSTEM` subject. Introduces
  the `SystemRetrySignal` schema and `NatsPublisher.publish_system_retry_signal`
  (plus the matching `SignalPublisher` protocol entry).
- **`max_retry_timeout` broker setting** — Seeded to `"60"` via Alembic
  migration `a5e6f7b8c9d0`. Time window (in seconds) used to select signals
  for the `RETRY_SIGNAL` replay. Missing/invalid values fall back to `60`.
- **`accounts.gateway` column** — Added via Alembic migration `f4d5e6a7b8c9`.
  Stores the exchange an account trades through (e.g. `MT5` for forex, `BINANCE`
  for crypto). Nullable; populated from the `WORKER_CONNECTED` handshake and the
  `TRADE` event, and surfaced on `GET /v1/accounts`. Combined with `market_type`
  and `account_id` it forms the `<market>-<gateway>-<account_id>` worker id used
  to address `SYSTEM` messages.
- **`gateway` on the `TRADE` (`PositionEvent`) payload** — Workers may report the
  account's exchange; the broker upserts it onto the `accounts` row.
- **`notification_timezone` broker setting** — Seeded to `"7"` via Alembic
  migration `e3c4d5f6a7b8`. UTC offset (in hours) applied to the `Time:` line
  of Telegram notifications; defaults to UTC+7.
- **`POST /admin/settings/notification-timezone`** — Sets `notification_timezone`.
  Requires `X-API-KEY`; `utc_offset_hours` must be between `-12` and `14`.

### Changed

- **Webhook pipeline split into enqueue + JetStream handler** — What used to
  run inline on `POST /secret/webhook` (persist → parse → publish to workers →
  notify) is now two hops. The route only runs the fast half (verify + block
  check + persist `QUEUED` + JetStream publish) and returns `202` with
  `status=queued`. The `SignalWorker` runs the second half from the JetStream
  consumer callback and flips the row to `PUBLISHED`. `SignalProcessingService`
  gains `handle_enqueued` for the consumer path; the existing `process` now
  handles only the enqueue path. `SignalRepository` grows two methods —
  `mark_published` and `list_recent_by_strategies` — used by the worker and
  the `RETRY_SIGNAL` replay respectively.
- **Admin crypto settings now push live to each crypto worker** — `POST
  /admin/settings/crypto-allowed-symbol` and `POST
  /admin/settings/crypto-max-leverage` send a `SYSTEM` `CRYPTO_LEVERAGE_INIT`
  after persisting the change, addressed per crypto account by its
  `<market>-<gateway>-<account_id>` worker id (built from the `accounts` row),
  so the new symbols/leverage apply immediately instead of only on the next
  `WORKER_CONNECTED` handshake. Each message carries both settings read back
  from the DB; delivery is best-effort — an account with no `gateway`, a missing
  or invalid complementary setting, or a failed publish is skipped and logged,
  and the endpoint still returns `200`.
- **Telegram `Time:` line now shows its timezone** — Signal and FLAT
  notifications render as `Time: <time> (UTC+N)` instead of a bare
  timestamp. The signal's timestamp is normalised to UTC first (naive
  TradingView timestamps are treated as UTC; aware timestamps are converted),
  then shifted to the configured `notification_timezone` (default UTC+7)
  before formatting.
- **`WORKER_CONNECTED` now records the worker's market/gateway** — Every valid
  handshake upserts the announced `market`/`gateway` onto the worker's
  `accounts` row (inserting the row if the account is unknown), keyed by the
  bare `account_id` recovered from the `<market>-<gateway>-<account_id>` worker
  id. Best-effort: a DB failure is logged and the worker still gets its reply.

### Fixed

- **Admin crypto push no longer skipped with `gateway not set`** — `gateway` was
  only ever written from a `TRADE` event, so an account that had not traded
  since migration `f4d5e6a7b8c9` kept a NULL `gateway` and was skipped by the
  `CRYPTO_LEVERAGE_INIT` push from `POST /admin/settings/crypto-*`, even though
  its worker announced `gateway` on every `WORKER_CONNECTED`. The handshake now
  persists it, so the column is filled as soon as the worker connects.

## [1.0.6] - 2026-07-05

### Added

- **`position.tp1_percent`** — New optional float field on the webhook
  `position` block, forwarded as-is onto the NATS `SIGNAL` payload. Allows
  the strategy to specify the percentage of the position to close at TP1 at
  signal time.
- **`position.move_sl_to_be`** — New optional boolean field on the webhook
  `position` block, forwarded onto the NATS `SIGNAL` payload. Signals the
  worker to move the stop loss to break-even after TP1 is hit.
- Display on notification these fields.
- **`position.risk_percent`** read from position and show to notifcation.
   Previously, field is read from input object.
- **`SYSTEM` NATS subject** — New subject shared by broker and workers.
  Publishes `CRYPTO_LEVERAGE_INIT` (broker → worker) and consumes
  `WORKER_CONNECTED` announcements (worker → broker). Introduces
  `SystemActionEnum` and `SystemSignal` schemas, plus
  `NatsPublisher.publish_system_signal` and the matching
  `SignalPublisher.publish_system_signal` protocol entry.
- **`SystemEventConsumer`** — Subscribes to `SYSTEM`, reacts to a worker's
  `WORKER_CONNECTED` announcement (identified by `account_id` in
  `<market>-<account_id>` format), loads the two crypto BrokerSetting rows
  below, and publishes back a `CRYPTO_LEVERAGE_INIT` `SystemSignal`. The
  broker's own outgoing `CRYPTO_LEVERAGE_INIT` messages are filtered out by
  action so the loop terminates on the worker.
- **`crypto_allowed_symbol` broker setting** — Seeded to `"BTC,ETH"` via
  Alembic migration `c1a2b3d4e5f6`. Comma-separated list of crypto symbols
  advertised to workers.
- **`crypto_max_leverage` broker setting** — Seeded to `"10"` via Alembic
  migration `d2b3c4e5f6a7`. Default leverage advertised to workers.
- Startup / reconnect Telegram notifications now list every subscribed
  subject (both `TRADE` and `SYSTEM`).
- **Telegram error-log forwarding** — When `TELEGRAM_ENABLED` and
  `TELEGRAM_LOG_ERRORS_ENABLED` are both set, log records at `ERROR` level or
  above are forwarded to a Telegram chat. `TelegramLogHandler` (a
  `logging.Handler`) hands each record to the event loop and a background
  worker — started and stopped in the app lifespan — performs the async send,
  so `emit` never blocks the event loop or raises. Three safeguards keep it
  production-safe: a filter drops records emitted by the send path itself (no
  feedback loop), identical messages are suppressed within
  `TELEGRAM_LOG_DEDUP_WINDOW` seconds (no spam), and the queue is bounded,
  dropping records under an error storm rather than growing unbounded.
- **Dedicated log bot/chat** — `TELEGRAM_LOG_BOT_TOKEN` and
  `TELEGRAM_LOG_CHAT_ID` route forwarded error logs through a bot and private
  chat kept separate from the main signal bot, so an outage or ban on one never
  affects the other. Both fall back to `TELEGRAM_BOT_TOKEN` /
  `TELEGRAM_CHAT_ID` when left empty.
- `ERROR_ALERT` (🚨) emoji constant prefixing each forwarded error log.
- **`SYSTEM` handshake request/reply** — Workers may announce themselves with
  NATS request/reply; the broker now replies directly on the request's inbox
  with the handshake outcome instead of only broadcasting. Adds the
  `WORKER_CONNECTED_ACK` (non-crypto workers) and `WORKER_CONNECTED_ERROR`
  (missing/invalid settings, carrying a `reason`) actions with matching
  `SystemWorkerConnectedAck` / `SystemWorkerConnectedError` schemas, plus
  `NatsPublisher.publish_system_ack` / `publish_system_error` and their
  `SignalPublisher` protocol entries. This lets a worker that connected while
  the broker was down time out and retry (the handshake is idempotent) rather
  than silently missing its configuration, and gives every outcome explicit
  feedback. Example payloads: `examples/nats/system.worker_connected_ack.json`,
  `examples/nats/system.worker_connected_error.json`.

### Changed

- **Service module consolidation** — `nats_consumer.py` and
  `nats_system_consumer.py` are merged into a single `nats_service.py`
  (exporting `TradeEventConsumer` and `SystemEventConsumer`), and the Telegram
  error-log handler moves from `telegram_log_handler.py` into
  `notification_service.py` alongside the other Telegram channels.
- **`WORKER_CONNECTED` now requires `market` and `gateway`** — The inbound
  `SYSTEM` schema is split into `SystemCryptoLeverageInitSignal` (outbound)
  and `SystemWorkerConnectedSignal` (inbound), the latter adding required
  `market` (`MarketEnum`) and `gateway` fields alongside `account_id`.
  Messages missing any of the three are rejected by validation and logged
  instead of raising. The `account_id` format changes from
  `<market>-<account_id>` to `<market>-<gateway>-<account_id>` (e.g.
  `CRYPTO-BINANCE-7654321`) across schemas, examples, and tests.
- **`CRYPTO_LEVERAGE_INIT` gated by market** — `SystemEventConsumer` only
  publishes the leverage-init response when `market == CRYPTO`; other
  markets' `WORKER_CONNECTED` announcements are logged and otherwise
  ignored.
- `SystemEventConsumer` now peeks at the `action` field before validating,
  so its own echoed `CRYPTO_LEVERAGE_INIT` messages no longer log a
  validation error.
- **`CRYPTO_LEVERAGE_INIT` delivered to the requester when possible** —
  `NatsPublisher.publish_system_signal` takes an optional `subject`; when a
  `WORKER_CONNECTED` arrives via request/reply the response is sent to that
  worker's reply inbox instead of fanning out on the shared `SYSTEM` subject.
  Fire-and-forget announcements still broadcast on `SYSTEM` unchanged.
- **Crypto settings read atomically and cached briefly** — nats-py runs one
  task per subscription and awaits each callback to completion before
  pulling the next message, so a reconnect storm serializes
  `WORKER_CONNECTED` handshakes on `SYSTEM` rather than running them in
  parallel. `SettingRepository` gains `get_many(keys)`, implemented as a
  single `WHERE key IN (...)` query; `SystemEventConsumer._get_crypto_settings`
  uses it to fetch `crypto_allowed_symbol` and `crypto_max_leverage` in one
  round trip instead of two, and caches the result (hit or miss) for
  `CRYPTO_SETTINGS_CACHE_TTL_SECONDS` (30s) so a burst of simultaneous
  reconnects reads the DB once instead of once per worker. The single query
  also makes the read atomic — both values reflect the same snapshot even if
  an admin call lands between what used to be two separate reads.
- **Reject non-positive `crypto_max_leverage`** — `SystemEventConsumer` now
  treats a parsed `default_leverage <= 0` the same as a non-integer value:
  it skips the `CRYPTO_LEVERAGE_INIT` publish and, on request/reply, answers
  with `WORKER_CONNECTED_ERROR` instead of pushing a nonsensical leverage to
  a crypto worker.
- **Admin endpoints for the crypto settings** —
  `POST /admin/settings/crypto-allowed-symbol` (body: `{"symbols": [...]}`,
  upper-cased/trimmed/de-duplicated, rejects an empty list) and
  `POST /admin/settings/crypto-max-leverage` (body:
  `{"default_leverage": <int>}`, rejects non-positive values) let these two
  settings be changed without touching the DB directly, matching the
  `block-signal` / `silent-signal` / `include-signal-raw` pattern. Adds
  `CryptoAllowedSymbolRequest`, `CryptoMaxLeverageRequest`, and
  `SettingValueResponse` to `admin_schema.py`.

### Fixed

- **`broker_settings.value` widened to `Text`** — Was `String(255)`, too
  narrow for a growing `crypto_allowed_symbol` list. Edited directly in the
  `54892682ef32` migration that creates the table (not a new migration) since
  the column has never held more than a handful of characters in practice and
  the target database is being wiped and re-migrated from scratch; the ORM
  model is updated to match. A deployment that already ran this migration and
  needs to keep its data would need a real `ALTER COLUMN` migration instead.

## [1.0.5] - 2026-06-25

### Added

- **Position scaling signals** — The webhook `position` block now accepts an `is_scale_position` boolean and a `scaling` object (`tp`, `sl`, `quantity`) to describe a scale-in. These fields are propagated onto the NATS `SIGNAL` payload consumed by workers, but only when `is_scale_position` is `true`; otherwise they are omitted.

## [1.0.4] - 2026-06-19

### Fixed

- **Tolerant `TRADE` ingestion** — `account_leverage` is now nullable on `Trade`. Workers and gateways that omit this field no longer fail to persist a trade. The `0001` migration drops the `NOT NULL` constraint, the ORM model is updated to `nullable=True`, and the `upsert_by_position_event` guard that rejected events without `account_leverage` has been removed.

## [1.0.3] - 2026-06-17

### Changed

- **Centralized emoji constants** — All Telegram notification emoji are now defined as named constants in `broker/helpers/emoji_constants.py` via the `emoji` library, replacing hardcoded Unicode literals scattered across multiple files.

### Added

- `emoji>=2.0.0` production dependency.

## [1.0.2] - 2026-06-16

### Changed

- **`Trade` table realigned to worker PositionEvent v2** — On the `trades` table, `ticket` → `ref_id` (now `VARCHAR(255)`), `magic` → `strategy_code`, and a new `gateway_return_code` column is added. The unique constraint `uq_trades_account_ticket` becomes `uq_trades_account_ref_id`, and the `magic` index becomes a `strategy_code` index. The `0001` migration was updated in place — existing databases must be re-migrated.
- Completed the `ticket → ref_id` / `magic → strategy_code` rename across docs, consumer logs, Bruno collections, and worker examples.
- README updates.

### Added

- **PositionEvent v2 ingestion** — The broker now consumes the worker's v2 `TRADE` event shape. `source_ticket`/`ticket` → `ref_source_id`/`ref_id`, `mt5_retcode` → `gateway_return_code`, `magic` → `strategy_code`; `market_type` is now a typed `MarketTypeEnum`; `account_name` is optional; and signal-derived fields (`sl`, `tp1`, `tp2`, `risk_percent`, `signal_id`) are promoted to first-class fields.
- **Persist `gateway_return_code`** — The gateway return code carried on each `TRADE` event is now stored on `Trade` and exposed in the `GET /v1/{account_id}/trades` response (`TradeResponse`).

### Fixed

- **Tolerant `TRADE` ingestion** — `account_leverage`, `account_balance_init`, `account_balance`, and `risk_percent` are now nullable on `Trade`. Workers and gateways that omit these fields no longer fail to persist a trade. The `0001` migration drops the `NOT NULL` constraint on `account_leverage` to match.
- **Restored broker logging** — Running Alembic migrations in-process (`init_db` on app startup) no longer silences the broker's already-configured loggers. `fileConfig` is now called with `disable_existing_loggers=False`.
- **Repository cleanup** — Removed the `upsert_by_position_event` guard that rejected `TRADE` events without `account_leverage`, since the column is now nullable.

### Removed

- Obsolete ZMQ key-bootstrap script (`scripts/ensure_keys.py`); accounts/broker-settings migration consolidated.

## [1.0.1] - 2026-06-03

### Security

- **Secret URL prefix** — Added `BROKER_API_PREFIX` environment variable. When set, all routes are mounted under `/<prefix>/v1/...`, `/<prefix>/admin/...`, and `/<prefix>/secret/...`. An attacker who knows the server IP or domain cannot enumerate endpoints without the prefix. Leave blank to keep the default paths.

## [1.0.0] - 2026-06-03

First stable release of **Algo Trading Broker** — a high-performance, decentralized trading signal broker built with FastAPI and NATS.

### Added

- **Webhook Hub** — Receives and validates TradingView JSON alerts via `POST /secret/webhook`. Supports optional HMAC signature verification via `X-Signature` header.
- **Signal Distribution** — Fan-out signals over NATS. Each signal is published to the subject matching its `strategy` field so workers subscribe only to what they need.
- **Trade Feedback** — Workers report executed positions back to the broker via the NATS `TRADE` subject; no REST endpoint required.
- **Account Tracking** — Worker accounts are auto-upserted from every incoming `TRADE` event.
- **Persistence** — Every signal, trade, and account snapshot is logged to PostgreSQL via Alembic-managed migrations.
- API (prefix `/v1`):
  - `GET /v1/health` — Liveness probe, no auth required.
  - `GET /v1/accounts` — List all trading accounts ordered by last activity.
  - `GET /v1/{account_id}/trades` — Paginated trade list with `limit`, `offset`, `order`, and `order_by` query params. Runs list and count concurrently.
- Admin (prefix `/admin`):
  - `POST /admin/settings/block-signal` — Toggle `SIGNAL_BLOCKED`; pauses signal forwarding without restart.
  - `POST /admin/settings/silent-signal` — Toggle `SILENT_SIGNAL`; mutes Telegram notifications without disabling Telegram.
  - `POST /admin/settings/include-signal-raw` — Toggle `NOTIFICATION_INCLUDE_SIGNAL_RAW`; appends indicators/inputs blocks to signal notifications.
  - `POST /admin/flat` — Publish a FLAT directive to workers via NATS `ADMIN` subject; scope by strategy, symbol, and/or account_id.
- Optional Telegram alerts for broker lifecycle events (startup, setting changes) and published signals. Silent mode and raw signal inclusion controllable at runtime via broker settings.
- Docker Compose stack: PostgreSQL + NATS + Broker with hot-reload (`compose watch`).
- Alembic migration environment with Makefile helpers (`db-upgrade`, `db-downgrade`, `db-revision`).
- Ruff for linting and formatting.
- Bruno API client collection pre-configured for all endpoints.
- NATS signal simulator for end-to-end testing (`make simulate-nats`).

### Security

- `X-API-KEY` header authentication for all admin and API endpoints.
- In-payload `token` field for webhook ingestion.
- NATS token-based authentication shared between broker and workers.
- `DOCS_ENABLED` toggle to hide Swagger UI / ReDoc / OpenAPI schema in production (default `false`).

[1.0.7]: https://github.com/rockingrow/algo-trading-broker/compare/v1.0.6...v1.0.7
[1.0.6]: https://github.com/rockingrow/algo-trading-broker/compare/v1.0.5...v1.0.6
[1.0.5]: https://github.com/rockingrow/algo-trading-broker/compare/v1.0.4...v1.0.5
[1.0.4]: https://github.com/rockingrow/algo-trading-broker/compare/v1.0.3...v1.0.4
[1.0.3]: https://github.com/rockingrow/algo-trading-broker/compare/v1.0.2...v1.0.3
[1.0.2]: https://github.com/rockingrow/algo-trading-broker/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/rockingrow/algo-trading-broker/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/rockingrow/algo-trading-broker/releases/tag/v1.0.0
