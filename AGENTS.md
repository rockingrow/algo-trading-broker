# Repository Instructions

Shared instructions for every coding agent working in this repository. Codex
reads this file directly; Claude Code reads it through the `@AGENTS.md` import
at the top of `CLAUDE.md`. **Keep shared content here** — anything specific to
one tool goes in that tool's own file, so the two never drift apart.

Follow a more specific `AGENTS.md` in a subdirectory when one exists.

## The project in five lines

Trading signal broker. Producers (TradingView alerts, and
[Quant-Trading-Engine](https://github.com/rockingrow/quant-trading-engine))
POST one `WebhookPayload` to `/secret/webhook`; the broker buffers it on the
JetStream `SIGNALS` stream, persists it, and fans it out on the per-strategy
NATS subject to MT5/Binance workers, which report trades back on `TRADE`.
Telegram broadcasts and trade cards ride on the same events. Python 3.13, uv,
FastAPI, NATS, PostgreSQL. The `bot/` directory is a separate uv project.

## Rules

1. **Commits**: only when the user asks. English, imperative, 72-char subject
   at most. **No AI attribution of any kind** — no `Generated with Claude
   Code`, no `Co-Authored-By: Claude`, no ChatGPT/Codex footer, no session link.
2. **English** for comments, docstrings, identifiers, log and exception strings,
   and every committed Markdown file.
3. **Names**: explicit and domain-flavoured (`position`, `broadcast_chat`, not
   `p`/`bc`). Exceptions for names fixed by an external contract (`tp1`, `sl`,
   `wt1`) and whole-word market terms. Do not rename legacy variables the task
   does not touch.
4. **Pull requests always target `dev`** — never `master`/`main`. Opening the
   PR is the user's call.
5. **Payload changes are contract changes.** A field added to
   `broker/schemas/` is a promise to TradingView alert templates, to QTE and to
   every worker: update `examples/`, `bruno/`, the README section that
   documents it and `changelog.md` in the same change.
6. **Never** expose, commit or copy secrets from `.env`, tokens, chat ids or
   account identifiers. `.env.example` carries placeholders only.

## Working approach

- Read the relevant source, tests, configuration and documentation before
  editing.
- Inspect `git status` first. Preserve every unrelated change and untracked
  file in the working tree.
- Make the smallest coherent change that solves the problem and matches the
  existing architecture.
- Do not add or upgrade production dependencies unless the task requires it;
  say why when you do.

## Commands

```bash
make install-dev    # uv sync (dev group: ruff, pytest, pytest-asyncio)
make lint / format  # ruff check . / ruff format .   (make fix = both, with --fix)
make run            # uv run python -m broker.main
make dev            # docker compose up --build -d + compose watch (hot reload)
make logs / logging # docker logs (last 500 / follow)
make help           # every target, one line each

uv run pytest -q                      # broker suite (tests/)
uv run pytest tests/test_schemas.py -q # one file while iterating
cd bot && uv run pytest -q            # the bot is a separate uv project

# Alembic — runs inside the broker container, so the stack must be up
make db-upgrade / db-downgrade / db-history / db-current
make db-revision m="add x"
```

## Repository navigation

Route the task with the table before searching. Start inside the owning
package; never scan from the repository root.

1. Table below, to find the owning module.
2. `sed -n '1,25p' <file>` — most modules open with a docstring stating their
   job and their trade-offs. That usually answers "does this file do X".
3. `rg -n "<symbol>" broker bot/app tests` — scope the search.
4. `tests/test_<topic>.py` — the suite is organised by topic and reads as the
   executable spec for that module.

| Task or concept | Primary location |
| --- | --- |
| Webhook receiver, HMAC + token check, enqueue/deferral | `broker/api/webhook.py` |
| Public v1 API, admin API, Telegram callbacks | `broker/api/{api,admin,telegram}.py`, `broker/router.py` |
| Inbound payload schema (`WebhookPayload`, `PositionSchema`) | `broker/schemas/webhook_schema.py` |
| Outbound NATS schemas (`TradingSignal`, ADMIN/SYSTEM) | `broker/schemas/publisher_schema.py` |
| Worker → broker trade events, accounts, enums | `broker/schemas/{trade_event_schema,account_schema,core}.py` |
| Payload → `TradingSignal` normalisation | `broker/helpers/signal_helper.py` |
| JetStream pipeline, block gate, publish, retries | `broker/services/{signal_processing_service,signal_retry_job}.py` |
| NATS connection, subjects, consumers | `broker/nats.py`, `broker/services/nats_service.py` |
| Telegram broadcast cycles and their write log | `broker/services/broadcast_service.py`, `broker/domain/broadcast_status.py` |
| Message rendering (broadcasts, trade cards) | `broker/helpers/{message_formatter,trade_card,emoji_constants}.py`, `broker/services/trade_card_service.py` |
| Models, repositories, LISTEN/NOTIFY | `broker/db/{models,repository,engine,listener}.py` |
| Migrations (single Alembic chain) | `alembic/versions/` |
| Settings and environment variables | `broker/settings.py`, `.env.example` |
| API-key guard (`X-API-KEY`) | `broker/security/` |
| DI protocols (db, notifier, publisher) | `broker/interfaces/` |
| Telegram bot (separate uv project, HTTP client only) | `bot/app/`, and read `bot/README.md` first |
| Canonical payload samples | `examples/webhook/`, `examples/nats/` (+ `examples/nats/subjects.md`) |
| Manual API requests | `bruno/{webhook,admin,telegram}/` |
| Broker payload contract as QTE relies on it | [`quant-trading-engine/docs/broker-contract.md`](https://github.com/rockingrow/quant-trading-engine/blob/main/docs/broker-contract.md) |

`README.md` is ~89KB — never read it whole. Run `rg -n '^#{1,3} ' README.md`
for the section index, then read only that range. `bot/README.md` documents the
bot's roles, commands and rendering.

Do not scan `.venv/`, `uv.lock`, `__pycache__/`, `.pytest_cache/` or `logs/`.

## Architecture invariants

- **The webhook does the least possible.** It verifies the token/HMAC and
  enqueues onto JetStream, then answers `202`. Persistence, block gate,
  fan-out, notifications and retries belong to `SignalWorker` and
  `SignalRetryJob` — nothing new goes back into the request path, because
  TradingView times out before it complains.
- **One payload schema, no per-producer branching.** TradingView and QTE send
  the same `WebhookPayload`; QTE may publish it straight to
  `SIGNALS.<strategy>`, so anything the HTTP path alone does is invisible to
  it.
- **Two ids, two jobs**: `signal_id` (per persisted signal) is the worker's
  de-duplication key; `signal_uxid` (per trade cycle) correlates an entry with
  its TP/SL/FLAT and keys the single Telegram broadcast message. Never swap
  them.
- **The handshake reply is one message.** `WORKER_CONNECTED_ACK` carries the
  whole initial configuration (magic map, replay signals, account settings,
  crypto config) because a NATS reply inbox accepts exactly one reply.
- **A broadcast body is rebuilt from its stored events**, never from the
  `signals` table — anything the message must render has to be captured in the
  event when it is written.
- **Schema changes go through the single Alembic chain** under
  `alembic/versions/` (`make db-revision m="..."`). No ad-hoc DDL, and a model
  change without a migration is an unfinished change.

## Code style

- Python 3.13, `uv`. Run Python tooling through `uv run`.
- Ruff: line length **88**, **2-space indent**, double quotes, rules
  `E4,E7,E9,F`. Run `make format` before committing.
- Async throughout; pytest runs with `asyncio_mode = "auto"`.
- Every non-trivial module opens with a docstring saying what it does and why.
  Comments explain the trade-off, not the syntax — match the density around
  you.
- Prefer explicit types and domain terminology over clever, compressed code.
- Cover behaviour changes with focused tests, including failure paths and
  boundary cases.

## Verification

- Run the narrowest relevant tests while iterating.
- Before handing back a code change: `uv run ruff check .` and
  `uv run pytest -q`; add `cd bot && uv run pytest -q` when `bot/` changed.
- The suite has **pre-existing failures**. Before blaming (or excusing) your
  change, get the baseline: stash and re-run, or run the same command on the
  base branch, and compare. Never leave a new failure behind on that argument.
- If `broker/db/models.py` changed, add the Alembic revision and check it
  applies (`make db-upgrade` against the local stack).
- If the webhook or NATS payload changed, re-check `examples/` and `bruno/`
  against the schema.
- Report every command you ran and every failure or skipped check. Never claim
  a check passed without running it.

## Trading and destructive operations

- Do not point the broker at a live NATS cluster, change `BROKER_API_KEY`,
  webhook tokens or Telegram chat ids, or publish ADMIN `FLAT` from a session
  without an explicit request and confirmation of the target environment.
- `make stop`, `docker compose down -v`, `make db-downgrade` and history
  rewrites are destructive: verify the exact target and get explicit approval
  immediately before running them.
- A published signal is not recallable. Treat `simulate` scripts, admin
  endpoints and the bot's Exit button as live actions unless the environment is
  proven local.
