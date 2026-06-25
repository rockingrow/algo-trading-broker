# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[Unreleased]: https://github.com/rockingrow/algo-trading-broker/compare/v1.0.5...dev
[1.0.5]: https://github.com/rockingrow/algo-trading-broker/compare/v1.0.4...v1.0.5
[1.0.4]: https://github.com/rockingrow/algo-trading-broker/compare/v1.0.3...v1.0.4
[1.0.3]: https://github.com/rockingrow/algo-trading-broker/compare/v1.0.2...v1.0.3
[1.0.2]: https://github.com/rockingrow/algo-trading-broker/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/rockingrow/algo-trading-broker/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/rockingrow/algo-trading-broker/releases/tag/v1.0.0
