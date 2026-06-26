# Project Audit — Algo Trading Broker

Audit of the FastAPI + NATS + PostgreSQL signal broker. Findings are split into
**Bugs / correctness risks** and **Enhancements (design patterns & best
practices)**. Each item lists the location, the impact, and a concrete fix.

Baseline at audit time: `pytest` → 18 passed, `ruff check` → clean.

Severity legend: 🔴 High · 🟠 Medium · 🟡 Low.

---

## 1. Bugs & correctness risks

### 🔴 B1 — Webhook secret is persisted in plaintext on every signal row
`broker/db/repository.py:123` stores `raw=json.loads(payload.model_dump_json())`,
and `WebhookPayload.token` (`broker/schemas/webhook_schema.py:118`) is the shared
webhook secret. Every row in `signals.raw` therefore contains the secret in
clear text. Anyone with read access to the DB (or a backup/dump) recovers the
credential that authorises signal injection.

**Fix:** strip `token` before persisting.
```python
raw = payload.model_dump(mode="json", exclude={"token"})
# ...
row = Signal(..., raw=raw)
```
Consider also excluding it from any debug log of the payload.

### 🔴 B2 — Worker `TRADE` events are silently dropped on any error (at-most-once)
`broker/services/nats_consumer.py:53-76` decodes JSON, validates `PositionEvent`,
and applies it. On `ValidationError` **or** any persistence exception it logs and
returns — the message is gone. The broker subscribes with core NATS (no
JetStream, no ack/redelivery), so a transient DB blip or one malformed field
permanently loses a trade event. For a trading audit log this is data loss.

**Fix (short term):** narrow the catch and let truly retryable failures surface;
**(real fix):** move the `TRADE` subject to **JetStream** with a durable
consumer + explicit `ack()`/`nak()` so failed events are redelivered. See E1.

### 🔴 B3 — `examples/worker/*.json` do not match the `PositionEvent` contract
None of the six example payloads validate against
`broker/schemas/trade_event_schema.py`. They are missing required fields
(`event`, `market_type`, `id`, `ref_source_id`, `volume`, `opened_price`) and use
different names (`price`→`opened_price`, `quantity`→`volume`, `ref_id`→
`ref_source_id`). Their `status` values (`CLOSED`, `REJECTED`, `PARTIALLY_CLOSED`,
`FLAT`) are also *broker* statuses, not the *worker* position statuses the policy
expects (`TP1`, `TP2`, `SL`, `R_SL`, `TERMINAL_CLOSED`, `FLATTED`). Only `OPENED`
overlaps. Verified: all 6 fail validation; if a real worker copied these shapes,
every event would be rejected by B2.

**Fix:** regenerate the examples from the current schema, and add a contract test
that loads each `examples/worker/*.json` through `PositionEvent` (see E10) so the
docs can never drift silently again.

### 🟠 B4 — `AdminSignal` declares `model_config` twice → `use_enum_values` is lost
`broker/schemas/publisher_schema.py:58` sets `ConfigDict(use_enum_values=True)`,
then line 66 reassigns `model_config = {...}`, silently discarding the first.
Verified: `AdminSignal(...).model_dump()["action"]` returns the enum member, not
`"FLAT"` — inconsistent with `TradingSignal`. JSON output happens to be correct
only because `str`-enums serialise by value.

**Fix:** merge into one config.
```python
model_config = ConfigDict(
    use_enum_values=True,
    from_attributes=True,
    json_schema_extra={"example": {...}},
)
```

### 🟠 B5 — Seed migration writes the wrong setting key (`prevent_signal`)
`alembic/.../54892682ef32_add_account_table_and_setting_table.py:128-131` seeds
`broker_settings` with key `prevent_signal`, but the app reads `signal_blocked`
(`broker/constants.py:1`). The seeded row is dead data; the key the code checks is
never created. (Functionally the default still resolves to "not blocked" because
`get()` returns `None`, but it is a latent rename bug and misleading state.)

**Fix:** seed `signal_blocked` (or drop the seed entirely and rely on the
`None`-means-unset default).

### 🟠 B6 — Multiple broker replicas double-process every `TRADE` event
`broker/services/nats_consumer.py:39` subscribes **without a queue group**. With
core NATS fan-out, every broker instance receives every `TRADE` message, so
running >1 replica (or restarting with overlap) double-applies upserts and races
on `account_balance` / `last_activity_at`.

**Fix:** subscribe with a queue group so exactly one instance handles each event.
```python
self._sub = await self._conn.nc.subscribe(
    self._conn.LISTEN_SUBJECT.value, queue="broker-trade-workers",
    cb=self.handle_subject_trade,
)
```

### 🟠 B7 — Upsert is read-then-write → race + `IntegrityError` under load
`broker/db/repository.py:170-258` (`upsert_by_position_event`) and
`_upsert_account` do `SELECT` then conditional `INSERT`. Two events for the same
`(account_id, ref_source_id)` (or a new account) can both miss the row and both
insert, violating `uq_trades_account_ref_id` / `uq_accounts_account_id`. The
exception is then swallowed by B2 and the event is lost. The same TOCTOU exists in
`SqlAlchemySettingRepository.set` (`repository.py:66-82`).

**Fix:** use PostgreSQL atomic upsert (`INSERT ... ON CONFLICT ... DO UPDATE`) via
`sqlalchemy.dialects.postgresql.insert`. See E2.

### 🟠 B8 — Worker `REJECTED` status is never recorded; `reject_reason` is dead
`broker/domain/trade_status.py:20-29` has no mapping for `REJECTED` (nor the
example's `CLOSED`/`PARTIALLY_CLOSED`/`FLAT`), so `to_trade_status()` returns
`None` and the event is dropped with a warning. Meanwhile `reject_reason` is
hard-coded to `None` on insert (`repository.py:246`) and never updated, even though
the column, the `TradeStatusEnum.REJECTED` value, and a `reject_reason` field on
the wire all exist. Rejections from workers are silently lost.

**Fix:** decide the canonical worker status vocabulary (align with B3), add a
`REJECTED` mapping, and persist `event.reject_reason`/`message` onto the row.

### 🟡 B9 — Entry price is overwritten by close price on partial-close updates
`broker/db/repository.py:180` computes `price = closed_price or opened_price` and
line 206 assigns `row.price = price` on every update. On a `TP1`
(PARTIALLY_CLOSED) event carrying a `closed_price`, the original **entry** price is
overwritten while the position is still open. If `price` is meant to be the entry,
this is data loss; if it is meant to be "last execution price", the column is
mis-named. Clarify the intended semantics and either keep `entry_price` separate or
rename.

### 🟡 B10 — Non-constant-time secret comparison (timing side-channel)
`broker/services/signal_processing_service.py:81` (`payload.token !=
self._webhook_secret`) and `broker/security/ensure_api_key.py:24` (`api_key !=
settings.BROKER_API_KEY`) use plain `==`/`!=`. Use `hmac.compare_digest` for
credential checks.

### 🟡 B11 — README advertises HMAC `X-Signature` validation that does not exist
The README "Features" / webhook docs describe optional HMAC `X-Signature`
verification, but `_verify_token` only does a plaintext equality check on the body
`token`. Either implement the HMAC check (read the `X-Signature` header, compare
`hmac_sha256(body, secret)`) or correct the docs. (Plaintext token in the body is
also weaker than a header HMAC and ties into B1.)

### 🟡 B12 — `Numeric` money columns are serialised back to `float` at the API
Migration `922c6a79dffc` deliberately moved financial columns to `NUMERIC(20,8)`,
but the response models (`TradeResponse`, `AccountResponse`) type these fields as
`float`, so SQLAlchemy `Decimal`s are coerced to float on the way out —
reintroducing binary-float rounding at the boundary the migration was meant to
fix. Prefer `condecimal`/`Decimal` (with `json_encoders` or `model_config`) end to
end for monetary values.

### 🟡 B13 — `PositionEvent.action` is an unvalidated `str`
`broker/schemas/trade_event_schema.py:42` types `action: str`, but it is written
into the `Enum(SignalActionEnum)` column as `event.action.upper()`
(`repository.py:237`). An unexpected action passes schema validation, then fails at
the DB layer where the failure is swallowed (B2). Validate `action` against
`SignalActionEnum` in the schema so bad input is rejected loudly and early.

### 🟡 B14 — Repositories swallow DB errors and return empty/zero
`get_all`, `list_by_account`, `count_by_account`, `SettingRepository.get`
(`repository.py:37-64, 260-301`) catch `Exception` and return `[]`/`0`/`None`. A
database outage is then indistinguishable from "no data": `/v1/accounts` returns
`200 []` while the DB is down, and a failed `signal_blocked` read silently behaves
as "not blocked" (fail-open on a safety switch). Let these propagate to the global
handler (→ 500) or fail-closed where safety matters.

---

## 2. Enhancements — design patterns & best practices

### E1 — Reliable messaging: NATS JetStream + idempotent consumer (Reliability)
Core NATS is fire-and-forget. For an audit/trade ledger, promote `TRADE` (and
arguably signals) to **JetStream** with a durable pull/push consumer, explicit
`ack`/`nak`, and a max-deliver + DLQ. Combined with an **idempotency key**
(`account_id`+`ref_source_id`+`status`) this gives at-least-once with safe
replays. Resolves B2/B6 structurally.

### E2 — Atomic upsert (Persistence)
Replace the read-then-write blocks with PostgreSQL upsert:
```python
from sqlalchemy.dialects.postgresql import insert
stmt = insert(Account).values(...).on_conflict_do_update(
    index_elements=["account_id"],
    set_={"account_balance": ..., "last_activity_at": ...},
)
await session.execute(stmt)
```
Removes the B7 race and shrinks the method considerably.

### E3 — Transactional Outbox (Consistency between DB and NATS)
`SignalProcessingService.process` commits the signal, *then* publishes to NATS
(`signal_processing_service.py:67-126`). A crash between the two persists a signal
that no worker ever sees (or publishes one that isn't logged). Write an `outbox`
row in the same transaction as the signal and have a relay publish it — guarantees
log-and-publish atomicity.

### E4 — Unit-of-Work / session-per-request (Testability & transactions)
Every repository method opens its own `get_session()`, so a single request spans
several independent transactions and can't be rolled back as a unit, and repos
can't be unit-tested without a live engine. Inject an `AsyncSession` (FastAPI
dependency yielding a per-request session) into repositories and let the service
own the transaction boundary.

### E5 — One typed message factory for NATS payloads (Consistency)
`publish_flat` hand-builds a `json.dumps({...})` (`nats_publisher.py:44-55`) while
`publish`/`publish_admin_signal` use Pydantic models. Give FLAT its own schema and
route everything through `model_dump_json()` so all wire formats are typed,
validated, and self-documenting. Note `PublishTopicEnum.SIGNAL` is defined but
unused (signals publish to the dynamic `strategy` subject) — remove or document.

### E6 — Cache/typed facade for broker settings (Performance & clarity)
Each webhook reads `signal_blocked` and `notification_include_signal_raw` from the
DB on the hot path. Wrap `SettingRepository` in a small TTL cache (settings change
rarely, via the admin endpoints which can invalidate it), and expose a typed
`BrokerSettings` facade (`is_signal_blocked`, `include_signal_raw`) instead of
stringly-typed `"0"/"1"` comparisons scattered across services.

### E7 — Split liveness vs readiness probes (Operability)
`/v1/health` only proves the process is up. Add a readiness endpoint that checks
PostgreSQL (`SELECT 1`) and `nats_client.nc.is_connected`, so orchestrators don't
route traffic before dependencies are ready. Keep liveness dependency-free.

### E8 — Fail-fast config validation (Robustness)
`WEBHOOK_SECRET` empty currently yields a per-request `500` deep in the pipeline
(`_verify_token`). Validate critical secrets at startup (pydantic-settings
validator) and refuse to boot misconfigured, rather than failing every webhook at
runtime. Also redact secrets from logs.

### E9 — Structured logging + correlation IDs (Observability)
Thread the `signal_id` (and a per-request id) through the whole pipeline and emit
structured (JSON) logs. Today it's hard to correlate a webhook → publish →
worker `TRADE` → trade row across services.

### E10 — Close the test gaps (Quality)
Current suite covers helpers/policy/service-with-fakes well, but there is **no**
test for the consumer, the repositories, or the example contracts. Add:
- a contract test loading every `examples/**/*.json` through its schema (would have
  caught B3);
- consumer tests (malformed JSON, unknown status, downgrade-ignored, happy path)
  with a fake `TradeRepository`;
- repository tests against an ephemeral Postgres (testcontainers) or `aiosqlite`.

### E11 — Decimal end-to-end for money (Correctness)
Pair with B12: use `Decimal` from ingestion (Pydantic `condecimal`) through the
ORM (`Numeric`) to the API response, with explicit JSON encoding, so prices/sizes
never round-trip through float.

### E12 — Minor cleanups
- `nats.py` `nc` property is typed `NATSClient` but returns `Optional`; guard or
  type as Optional to avoid a confusing `AttributeError` if used pre-connect.
- `main.py` docstring still says `init_db` "creates tables" — migrations do that
  now; update the comment.
- `accounts` list endpoint has no pagination/limit; bound it for large datasets.
- DB `updatedAt` is maintained by both an ORM `onupdate` and a SQL trigger; keep
  one source of truth.

---

## 3. Suggested priority order

1. **B1** (secret in DB), **B3/B2** (event loss + contract drift) — correctness &
   security first.
2. **B6, B7** (concurrency) and **E1/E2** — reliability of the trade ledger.
3. **B5, B8, B13, B14** — data integrity / fail-open behaviours.
4. **B4, B10, B11, B12** + **E3–E12** — polish, hardening, and quality.
</content>
</invoke>
