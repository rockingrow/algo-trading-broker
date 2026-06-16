# Changelog

## [v1.0.2] — 2026-06-16

### Breaking Changes

- **`Trade` table realigned to worker PositionEvent v2** — On the `trades` table, `ticket` → `ref_id` (now `VARCHAR(255)`), `magic` → `strategy_code`, and a new `gateway_return_code` column is added. The unique constraint `uq_trades_account_ticket` becomes `uq_trades_account_ref_id`, and the `magic` index becomes a `strategy_code` index. The `0001` migration was updated in place — existing databases must be re-migrated.

### Features

- **PositionEvent v2 ingestion** — The broker now consumes the worker's v2 `TRADE` event shape. `source_ticket`/`ticket` → `ref_source_id`/`ref_id`, `mt5_retcode` → `gateway_return_code`, `magic` → `strategy_code`; `market_type` is now a typed `MarketTypeEnum`; `account_name` is optional; and signal-derived fields (`sl`, `tp1`, `tp2`, `risk_percent`, `signal_id`) are promoted to first-class fields.
- **Persist `gateway_return_code`** — The gateway return code carried on each `TRADE` event is now stored on `Trade` and exposed in the `GET /v1/{account_id}/trades` response (`TradeResponse`).

### Bug Fixes

- **Tolerant `TRADE` ingestion** — `account_leverage`, `account_balance_init`, `account_balance`, and `risk_percent` are now nullable on `Trade`. Workers and gateways that omit these fields no longer fail to persist a trade. The `0001` migration drops the `NOT NULL` constraint on `account_leverage` to match.
- **Restored broker logging** — Running Alembic migrations in-process (`init_db` on app startup) no longer silences the broker's already-configured loggers. `fileConfig` is now called with `disable_existing_loggers=False`.
- **Repository cleanup** — Removed the `upsert_by_position_event` guard that rejected `TRADE` events without `account_leverage`, since the column is now nullable.

### Documentation & Housekeeping

- Completed the `ticket → ref_id` / `magic → strategy_code` rename across docs, consumer logs, Bruno collections, and worker examples.
- Removed the obsolete ZMQ key-bootstrap script (`scripts/ensure_keys.py`) and consolidated the accounts/broker-settings migration.
- README updates.

---

## [v1.0.1] — 2026-06-03

### Security Hardening

- **Secret URL prefix** — Added `BROKER_API_PREFIX` environment variable. When set, all routes are mounted under `/<prefix>/v1/...`, `/<prefix>/admin/...`, and `/<prefix>/secret/...`. An attacker who knows the server IP or domain cannot enumerate endpoints without the prefix. Leave blank to keep the default paths.

---

## [v1.0.0] — 2026-06-03

First stable release of **Algo Trading Broker** — a high-performance, decentralized trading signal broker built with FastAPI and NATS.

### Core Features

- **Webhook Hub** — Receives and validates TradingView JSON alerts via `POST /secret/webhook`. Supports optional HMAC signature verification via `X-Signature` header.
- **Signal Distribution** — Fan-out signals over NATS. Each signal is published to the subject matching its `strategy` field so workers subscribe only to what they need.
- **Trade Feedback** — Workers report executed positions back to the broker via the NATS `TRADE` subject; no REST endpoint required.
- **Account Tracking** — Worker accounts are auto-upserted from every incoming `TRADE` event.
- **Persistence** — Every signal, trade, and account snapshot is logged to PostgreSQL via Alembic-managed migrations.

### API (prefix `/v1`)

- `GET /v1/health` — Liveness probe, no auth required.
- `GET /v1/accounts` — List all trading accounts ordered by last activity.
- `GET /v1/{account_id}/trades` — Paginated trade list with `limit`, `offset`, `order`, and `order_by` query params. Runs list and count concurrently.

### Admin (prefix `/admin`)

- `POST /admin/settings/block-signal` — Toggle `SIGNAL_BLOCKED`; pauses signal forwarding without restart.
- `POST /admin/settings/silent-signal` — Toggle `SILENT_SIGNAL`; mutes Telegram notifications without disabling Telegram.
- `POST /admin/settings/include-signal-raw` — Toggle `NOTIFICATION_INCLUDE_SIGNAL_RAW`; appends indicators/inputs blocks to signal notifications.
- `POST /admin/flat` — Publish a FLAT directive to workers via NATS `ADMIN` subject; scope by strategy, symbol, and/or account_id.

### Security

- `X-API-KEY` header authentication for all admin and API endpoints.
- In-payload `token` field for webhook ingestion.
- NATS token-based authentication shared between broker and workers.
- `DOCS_ENABLED` toggle to hide Swagger UI / ReDoc / OpenAPI schema in production (default `false`).

### Notifications

- Optional Telegram alerts for broker lifecycle events (startup, setting changes) and published signals.
- Silent mode and raw signal inclusion controllable at runtime via broker settings.

### Infrastructure

- Docker Compose stack: PostgreSQL + NATS + Broker with hot-reload (`compose watch`).
- Alembic migration environment with Makefile helpers (`db-upgrade`, `db-downgrade`, `db-revision`).
- Ruff for linting and formatting.
- Bruno API client collection pre-configured for all endpoints.
- NATS signal simulator for end-to-end testing (`make simulate-nats`).
