@AGENTS.md

# Claude Code

The line above imports [AGENTS.md](AGENTS.md), the shared instruction file every
coding agent in this repository follows — project layout, the navigation table,
architecture invariants, the rules and verification. Claude Code does not read
`AGENTS.md` on its own; that import is what puts it in context.

**Do not copy shared content into this file.** Anything both Codex and Claude
need goes in `AGENTS.md`; this file holds only what is specific to Claude Code.
Two files describing the same repository is how they start contradicting each
other.

## Before handing work back

- `/code-review` on the diff before the branch is proposed for a PR into `dev`.
- `/security-review` when the change touches the webhook, the API-key guard,
  `.env` handling, Telegram tokens, or anything published to NATS.
- Push to the working branch and report the commands you ran, including the
  test baseline comparison AGENTS.md asks for.

## Where to slow down

Use plan mode, and confirm the approach, before editing:

- `broker/api/webhook.py` and `broker/services/signal_processing_service.py` —
  the delivery path; a mistake here drops or duplicates real signals, and
  TradingView never re-sends.
- `broker/schemas/{webhook,publisher}_schema.py` — a contract change for every
  worker and for QTE, neither of which this repository can see.
- `alembic/versions/` — one chain, applied to a live database.
- `broker/services/broadcast_service.py` — its events are the only source a
  message body is re-rendered from; a missing field is invisible until an edit
  drops it.

## Session hygiene

- Durable project facts belong in `AGENTS.md`, not in auto memory: Codex has to
  see them too.
- If `/context` does not list both `CLAUDE.md` and `AGENTS.md` under **Memory
  files**, the import broke — check that the first line of this file is
  `@AGENTS.md` outside any code fence.
- The `.claude/hooks/session-start.sh` hook runs `uv sync --group dev` in
  remote sessions only; locally, run `make install-dev` yourself before
  expecting `ruff`/`pytest` to exist.
