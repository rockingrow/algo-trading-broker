# Telegram

Two things on this page: the interactive bot that owners and admins talk to,
and the broadcast messages the broker posts to chats as a trade cycle moves.

## Interactive bot

An interactive bot lives in [`bot/`](../bot/) (built with **aiogram v3**), serving
**two roles** from one process — endusers and admins. It is a **thin HTTP
client** of the broker — it never touches PostgreSQL or NATS directly, calling
broker endpoints with the broker `X-API-KEY`.

> 📖 **Full documentation — commands, rendering, architecture, configuration and
> local development — lives in [`bot/README.md`](../bot/README.md).** This page
> only covers what the *broker* side needs to know: the data model backing the
> bot and the endpoints it calls.

Command menus are role-aware **and** link-aware (Telegram command **scopes**): a
user with no linked account sees `/start` and nothing else — every other
command, `/help` included, needs an account behind it — and the full menu
appears the moment they link. Each id in `TELEGRAM_ADMIN_IDS` additionally gets
the admin menu (`/admin_accounts`, `/admin_newaccount`, `/admin_trades`,
`/admin_flat`, `/admin_rotate`, `/admin_settings`, `/admin_max_orders`, …),
which is not gated on linking.

### Onboarding / auth flow

1. Every account has at least one link token (UUID) in `account_link_tokens`. An
   admin reads it as `link_token` from `GET /v1/accounts` (or rotates it via
   `POST /admin/accounts/{account_id}/link-token/rotate`) and hands it to the user.
2. The user sends `/start` to the bot and pastes the token. The bot calls
   `POST /v1/telegram/link`, which records an `account_bot_links` row joining
   their Telegram id to the account.
3. Linked users can then query trades (`/trades`) and issue control commands.

### Many-to-many: accounts ↔ bot users

`account_bot_links` is a join table, so both directions are open:

- One user may link **several** accounts — typically one per market/gateway
  pair (e.g. an MT5 forex account and a Binance crypto account).
- One account may be linked by **several** users — e.g. an owner and an
  assistant. There is no role distinction yet: every linked user has the same
  rights over the account.

Linking never removes an existing link in either direction, and `/unlink` only
drops the caller's own.

### Active account

For each user, exactly one of their linked accounts is **active** at a time, and
every single-account command (`/status`, `/trades`, `/flat`, `/prevent`,
`/allow`, `/maxorders`, `/subscribe`, `/unsubscribe`, `/unlink`) acts on whichever
one that is.

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

| Command | Admin action | Persisted on the account | Notes |
| ------- | ------------ | ------------------------ | ----- |
| `/flat` | `FLAT` | — (an action, not a state) | Close positions for the **active** account. |
| `/prevent` | `BLOCK_SIGNAL` | `settings.signal_blocked = true` | Block new signals (worker must honor it). |
| `/allow` | `ALLOW_SIGNAL` | `settings.signal_blocked = false` | Re-enable new signals. |
| `/maxorders` | — (SYSTEM `WORKER_SETTINGS_UPDATE`, not ADMIN) | `settings.max_open_orders` | Cap on concurrently open positions for the **active** account; `0` = no cap, `-` clears it and inherits the broker-wide default. |

> `BLOCK_SIGNAL` / `ALLOW_SIGNAL` are scoped by `account_id` in the `AdminSignal`
> payload. Enforcement is the **worker's** responsibility — worker code lives
> outside this repo, so the bot/broker only publish the directive.

> A command that changes state writes it to `accounts.settings` **before**
> publishing, and the endpoint answers `500` (publishing nothing) if that write
> fails — a command whose intent was not recorded has not taken effect, since
> the ADMIN publish only reaches a worker that is connected right now. The
> stored blob is replayed to the worker in the `settings` block of every
> `WORKER_CONNECTED_ACK` (see [Account settings](nats.md#account-settings)) and echoed
> back to the bot in the command response's `settings` field.

**Presentation** — list commands reply with monospace tables, and every
timestamp is rendered in the `notification_timezone` broker setting (read over
HTTP from `GET /admin/settings/notification-timezone`, since the bot has no DB
access, falling back to UTC+7). Details in
[`bot/README.md` → Rendering](../bot/README.md#rendering).

`/status` additionally shows an **Open positions** count for the active
account (backed by `GET /v1/telegram/{telegram_user_id}/positions`, which
returns the account's currently running — `is_running` — trades), and when
that count is above zero, the same trade table below it, filtered to just
those open positions.

Run it with the stack: `docker compose up -d bot`. See
[`bot/README.md`](../bot/README.md) for the full command reference, configuration
and local development.

## Broadcast update notices: the reply under an edited message

A trade's broadcast message is **edited in place** as the trade progresses, and
Telegram sends no notification for an edit: a reader who saw the entry would
never find out that it hit TP1 or closed unless they happened to scroll back.
So every new action of a cycle does two things in **both** audiences (private
and public):

1. the cycle's message is rewritten with the full body (levels, timeline,
   worker table);
2. a two-line notice is posted as a **reply** to that same message:

```
[🏁CLOSED]
🚀 TP2
```

That is the whole notice — the cycle's status at that point (`RUNNING` /
`TP1` / `CLOSED`, see below) and the action that produced it (`TP1`, `TP2`,
`SL`, `R_SL`, `FLAT`, …). It notifies, and being a reply it points straight
back at the full body, so nothing is duplicated.

Notes:

- The **first** message of a cycle carries no notice — the message that just
  arrived is the news.
- One notice per *action*, not per delivery: a dispatch that coalesces two
  actions posts one reply each, and the intermediate one keeps the status it
  had at the time (a TP1 announced before the close is not back-dated to
  `CLOSED`).
- A worker execution updates the body (the execution table) but announces
  nothing — no new action happened.
- `broadcast_message_chats.notified_event_count` records how many actions each
  chat has been told about, so a redelivery, a sweeper pass or a retried edit
  never repeats a notice. A reply that fails is simply retried on the next
  pass, and a notice whose message was meanwhile deleted is sent as a plain
  message (`allow_sending_without_reply`) rather than lost.
- Either audience can turn this reply off on its own — the
  `private_broadcast_reply_notify` / `public_broadcast_reply_notify` broker
  settings, both **enabled by default**. The cycle's message keeps being
  edited in place either way; disabling one only silences the reply notice for
  that audience. Editable via `GET`/`POST /admin/settings/private-reply-notify`
  and `.../public-reply-notify`, or the bot's `/admin_private_reply_notify` and
  `/admin_public_reply_notify [enable|disable]` commands. A notice skipped this
  way still counts as delivered (`notified_event_count` advances), so
  re-enabling later does not replay the trade's backlog into the chat.

## The three header states

The `[STATUS]` bracket at the top of a cycle's message — and of the owner's
trade card — carries one of three states, so a glance at the header says where
the trade stands:

| Header | When | Why not the next one |
| --- | --- | --- |
| `[⏳RUNNING]` | the entry (`LONG` / `SHORT`) is live and nothing has come off it | — |
| `[🎯TP1]` | `TP1` filled: part of the position is booked, the rest is still open | not `CLOSED`, because the trade is still in the market |
| `[🏁CLOSED]` | a closing action arrived — `TP2`, `SL`, `R_SL` or `FLAT` | `TP2` takes the rest of the position off, so it closes the cycle rather than getting a header of its own |

The status only ever moves forward. `CLOSED` is terminal, so a replayed or
late `TP1` cannot advertise the position as live again; `TP1` is sticky the
same way, so a scale-in entry arriving after the partial fill does not take
the header back to `RUNNING`. The owner-facing trade card renders the same
three words from the *trade's* own status instead (`PARTIALLY_CLOSED`, or a
`TP1` last action, reads as `[🎯TP1]`).

The body's header is folded from the cycle's stored events as well as read off
`broadcast_messages.status`, the way the reply notices are, so a cycle recorded
before `TP1` was a status still renders `[🎯TP1]` instead of contradicting the
notice posted under it.
