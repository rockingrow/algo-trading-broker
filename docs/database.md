# PostgreSQL Schema

## `signals` table

| Column | Type | Description |
| ------------------ | ---------------- | --------------------------------------- |
| `id` | UUID (PK) | Unique record identifier |
| `strategy` | String(50) | Strategy name that generated the signal |
| `signal_uxid` | String(16) (Nullable) | Cycle correlator — 16-char lowercase-hex short uuid shared by every signal of one trade (entry + TPs + SL + FLAT). Ties a signal to its `broadcast_messages` row — together with the row's `symbol` and `strategy`, the cycle key — so the trade renders as one edited-in-place Telegram message, even while other trades run on the same symbol and strategy. |
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

## `trades` table

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

## `accounts` table

| Column | Type | Description |
| ------------------- | ------------ | ------------------------------------------ |
| `id` | UUID (PK) | Unique record identifier |
| `account_id` | String(50) | Worker's broker account ID (unique together with `market` + `gateway` — **not** unique alone; see note below) |
| `account_name` | String(255) | Display name of the account (nullable) |
| `account_balance` | Numeric(20,8) | Most recent account balance (nullable) |
| `market` | Enum | `FOREX` or `CRYPTO` |
| `gateway` | String(50) | Exchange the account trades through, e.g. `MT5`, `BINANCE` (nullable) |
| `last_activity_at` | DateTime | Timestamp of the last TRADE event received |
| `settings` | JSONB | What the owner set from the bot — `{"signal_blocked": true}` from `/prevent`, `{"max_open_orders": 3}` from `/maxorders`. `NOT NULL`, defaults to `{}`; sent to the worker in the `settings` block of every `WORKER_CONNECTED_ACK` — see [Account settings](nats.md#account-settings). A key absent (or `null`) means the account inherits the broker-wide default for that setting |
| `createdAt` | DateTime | Record insertion time |
| `updatedAt` | DateTime | Last update time |

**Why JSONB and not a column per toggle:** the set of bot commands grows, and each new toggle would otherwise cost a migration — while the whole thing travels to the worker as one object anyway. It also beats JSON-in-`TEXT` (what `broker_settings.value` uses) on two counts: Postgres can merge a single key server-side (`settings || '{"signal_blocked": true}'`), so two commands landing together can't drop each other's key the way a read-modify-write would, and the column stays queryable (`WHERE settings @> '{"signal_blocked": true}'`, GIN-indexable) if an admin view ever needs it.

**Unique constraint:** `(market, gateway, account_id)` — a bare `account_id` can exist under more than one market/gateway (two unrelated real accounts, e.g. an MT5 login and a Binance account, can coincidentally share a number). Endpoints and repository methods that take only `account_id` (`POST /admin/accounts/{account_id}/link-token/rotate`, `GET /v1/{account_id}/trades`, the `account_id` scope on `POST /admin/flat`) resolve/match on that bare id and can be ambiguous if it's reused across gateways — avoid deliberately reusing an `account_id` across gateways until those callers are updated to also pass `market`/`gateway`.

> The `accounts` table deliberately carries **no** bot/chat-platform columns: an
> account is a trading domain object. Who may drive it from a bot lives in
> `account_bot_links`, the invite secrets in `account_link_tokens`, and the
> per-user active selection in `bot_sessions`. All three are keyed by
> `platform` (`BotPlatformTypeEnum`, currently only `TELEGRAM`) so adding
> Discord/Slack is a new enum member, not a migration.

## `account_bot_links` table

Many-to-many join between accounts and chat-platform users: an account may be
managed by several people, and a person may hold several accounts. There is no
role/permission column — every linked user has the same rights.

| Column | Type | Description |
| ------- | ------------ | --------------------------------------- |
| `id` | UUID (PK) | Unique record identifier |
| `account_id` | UUID | FK → `accounts.id`, `ON DELETE CASCADE` |
| `platform` | Enum | `TELEGRAM` |
| `platform_user_id` | String(64) | The bot user's platform id. Stored as text, not a number: Telegram/Discord ids are numeric but Slack/Matrix ids are opaque strings |
| `trade_cards_enabled` | Boolean | `false` by default. The user's opt-in to the [live trade card](#how-a-card-lives) **for this account** — flipped by the bot's `/subscribe` / `/unsubscribe` on the user's active account only, or on every link the user holds by `/subscribeall` / `/unsubscribeall` |
| `createdAt` | DateTime | Record insertion time |
| `updatedAt` | DateTime | Last update time |

**Unique constraint:** `(platform, platform_user_id, account_id)`.

The opt-in lives on the link rather than in a table of its own because it is a
statement about one `(user, account)` pair: with accounts A and B a user can have
cards on for A and off for B. It also ties the preference's lifetime to the
access it belongs to — unlinking (or rotating the account's token) deletes the
row, so a link made later starts off again.

## `account_link_tokens` table

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

## `bot_sessions` table

One row per `(platform, bot user)`, tracking which of their linked accounts is
currently **active** — the one every single-account bot command (`/status`,
`/trades`, `/flat`, `/prevent`, `/allow`, `/subscribe`, `/unsubscribe`, `/unlink`)
acts on. Kept separate from
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

## `trade_notifications` table

One row per **live trade card**: the Telegram message a subscriber was sent for
one trade, remembered so later status changes edit that same message instead of
posting a new one.

| Column | Type | Description |
| ------- | ------------ | --------------------------------------- |
| `id` | UUID (PK) | Unique record identifier |
| `trade_id` | UUID (FK) | `trades.id`, `ON DELETE CASCADE` |
| `platform` | Enum | `TELEGRAM` |
| `chat_id` | String(64) | Recipient's platform user id (their DM chat) |
| `message_id` | Integer | The Telegram message to edit |
| `status` | Enum | The trade status the card currently shows |
| `createdAt` | DateTime | Record insertion time |
| `updatedAt` | DateTime | Last update time |

**Unique constraint:** `(trade_id, platform, chat_id)` — one card per trade per
recipient.

The card's status line names the event that moved the trade, in brackets —
`Status: Closed (TP2)`, `Closed (SL)`, `Closed (R_SL)`,
`Closed (TERMINAL_CLOSED)`, `Closed (FORCED_CLOSED)` — since five worker events
collapse onto the one `CLOSED`, while the card's direction stays the entry
(`LONG` / `SHORT`) the trade was opened with. It comes from `trades.last_action`
(`TradeStatusPolicy.to_last_action`, which renames only `FLATTED` → `FLAT`), so
an admin FLAT reads `Status: Flatted`, not `Flatted (FLAT)`. It lives on the
trade row rather than being passed with the event because the card is
re-rendered later — by the bot, on a Detail tap — with only the row to go on.

### How a card lives

Every `TRADE` event, after the `trades` upsert, goes to `TradeCardService`:

1. **First sighting** — the broker posts the card to every owner who has the
   account's `account_bot_links.trade_cards_enabled` switched on, via the
   bot-service bot token
   (`BOT_TELEGRAM_TOKEN`) — the bot the user actually started, since a bot can
   only message users who started it. Below the body sit a **Detail** and an
   **Exit** button.
2. **Status change** — `OPENED` → `PARTIALLY_CLOSED` (TP1) → `CLOSED` (TP2 /
   SL / R_SL / TERMINAL_CLOSED / FORCED_CLOSED) / `FLAT` (an admin
   `POST /admin/flat`) / `REJECTED` — the broker calls `editMessageText` on the
   stored `message_id`, so the owner's chat keeps **one** message per trade
   rather than a stream of them.
3. **Terminal status** — the card is edited one last time and its buttons are
   dropped, since there is nothing left to act on. That final state *is* the
   completed-trade notification.

Nothing Telegram-shaped happens on the `TRADE` path. nats-py awaits the
consumer's callback before pulling the next message, so `handle_event` only
*queues* the trade and a single background drain task does the lookups and the
Bot API calls — the same reason the signal path hides behind `QueuedNotifier`,
which cannot serve here because a card needs the `message_id` its send returns.
One drain task means two events for the same trade are never applied out of
order, and a full queue drops the update with a warning rather than blocking
the bookkeeping it was meant to stay out of.

Two rules keep the Bot API traffic proportional to what a human would notice:

- **Only status transitions edit.** Workers re-emit `TRADE` events for changes a
  card doesn't show (an SL nudge, a sync tick); `status` here records what the
  message currently displays, so those are skipped.
- **Existing cards are refreshed even after unsubscribing.** The opt-in decides
  who gets a *new* card; once a card exists it keeps telling the truth, rather
  than freezing on `OPENED` with a live Exit button.

The *persisted* trade drives the card, not the event — the repository already
refuses a status downgrade, so a late or out-of-order event cannot reopen a card
that has closed. A subscriber who opts in mid-trade still gets a card on the next
event; if that event is the close, the card simply arrives final and buttonless.

When Telegram reports a message as permanently unreachable (the user deleted it,
or blocked the bot) the row is dropped so later events stop retrying. Transient
failures keep the row, and the next status change tries again.

A `FLATTED` event reports `closed_price=0` when the worker has no close price to
give. No instrument closes at 0, so the broker treats it as missing and keeps the
open price rather than persisting — and showing — a bogus `0`.

### The buttons

The card is sent with the bot service's token (`TradeCardNotifier`, which
reuses `BroadcastNotifier`'s send/edit machinery and only overrides the token,
the body formatting and which errors are unrecoverable), so taps arrive as
ordinary callback queries on the bot's existing long poll — no webhook, no
second connection. The bot answers them against two endpoints scoped to a single trade:

| Button | Endpoint | Effect |
| ------- | ------------ | --------------------------------------- |
| **Detail** / **Summary** | `GET /v1/telegram/{id}/trades/{trade_id}` | Re-renders the card with (or without) the strategy / account / leverage / risk block |
| **Exit** | `POST /v1/telegram/{id}/trades/{trade_id}/exit` | After a confirmation tap, publishes a FLAT scoped to that trade's `strategy` + `symbol` + account |

Both authorise against **every** account the caller is linked to, not just the
active one — a card outlives an account switch. `exit` answers `409` on a trade
that has already reached a terminal status, so a stale card cannot close a newer
position on the same symbol.

## `broker_settings` table

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
| `private_broadcast_reply_notify` | `"1"` (unset = enabled) | `POST` / `GET /admin/settings/private-reply-notify` | Whether the **private** broadcast posts a reply notice under the cycle's message on each new action (see "Broadcast update notices" above). `"0"` silences the reply only — the message itself keeps being edited in place. Bot: `/admin_private_reply_notify [enable\|disable]`. |
| `public_broadcast_reply_notify` | `"1"` (unset = enabled) | `POST` / `GET /admin/settings/public-reply-notify` | Same as above, for the **public** broadcast. Bot: `/admin_public_reply_notify [enable\|disable]`. |
| `max_retry_timeout` | `"60"` | — (edit directly) | Seconds of history included in the `retry_signals` replay sent to a freshly-connected worker |
| `max_open_orders` | `"5"` (unset = `5`) | `POST` / `GET /admin/settings/max-open-orders` | Broker-wide cap on concurrently open positions, inherited by every account whose `accounts.settings` carries no `max_open_orders` of its own. `"0"` disables the cap. Resolved per account and sent in the `settings` block of each `WORKER_CONNECTED_ACK`, and pushed live as `SYSTEM.WORKER_SETTINGS_UPDATE`. Bot: `/admin_max_orders`. |

## `broadcast_messages` table

One row per signal *cycle*: everything the broker has seen for one trade
(``symbol`` + ``strategy`` + ``signal_uxid``) rolled up into a single record. The row's
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
| `status` | Enum | `RUNNING` on the entry, `TP1` once the partial take-profit fills, `CLOSED` when a closing action arrives (TP2, R_SL, SL, FLAT). Never moves backwards — see [The three header states](telegram.md#the-three-header-states) |
| `events` | JSONB | Ordered list of stored action events — action, price/qty/levels, timestamp, attempt number, indicator/input dumps |
| `last_seq` | Integer | Sequence number of the most recent write-log entry against this cycle (see below) |
| `last_broadcast_at` | DateTime (Nullable) | Timestamp of the last successful dispatcher run for this cycle |
| `createdAt` / `updatedAt` | DateTime | Record insertion / last-update times |

**Unique constraint:** `(symbol, strategy, signal_uxid)` — the cycle key. The
symbol is part of it because one `symbol` + `strategy` pair can run several
cycles at once (see [POST `/secret/webhook`](api.md#post-secretwebhook)): the cycle id
is what separates the concurrent trades, and the symbol is what keeps an id
minted for one symbol from colliding with another's.

## `broadcast_message_chats` table

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
| `notified_event_count` | Integer (Nullable) | How many of the cycle's actions this chat has been told about with a reply notice under its message; `NULL` on rows written before notices existed (caught up silently on the next delivery) |
| `last_error` | String(500) (Nullable) | Last transient error seen when editing/sending, or `NULL` on success |
| `createdAt` / `updatedAt` | DateTime | Record insertion / last-update times |

**Unique constraint:** `(broadcast_message_id, chat_id)`.

## `broadcast_message_workers` table

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

## `broadcast_message_logs` table

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
