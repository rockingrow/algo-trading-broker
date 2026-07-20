"""
app/constants.py — Bot-side constants.

``GATEWAYS_BY_MARKET`` mirrors ``broker/schemas/account_schema.py``'s
``GATEWAYS_BY_MARKET`` for keyboard display only. The broker is the source of
truth and re-validates the combination server-side (``POST /admin/accounts``
returns 422 for an invalid pair) — this copy only needs to stay roughly in
sync so the picker doesn't offer a gateway the broker would reject.
"""

from __future__ import annotations

MARKETS: list[str] = ["FOREX", "CRYPTO"]

GATEWAYS_BY_MARKET: dict[str, list[str]] = {
  "FOREX": ["MT5"],
  "CRYPTO": ["BINANCE"],
}

# ── Page sizes ──────────────────────────────────────────────────────
# Every command that renders a table pages through it. Telegram caps a message
# at 4096 characters and rejects the whole send once a table crosses it, so an
# unpaginated list doesn't degrade — it fails outright the day the account or
# trade count grows.
#
# These live here rather than in the environment because the right number
# follows from how wide the table is, which is a property of the code and not
# of the deployment: the more columns a row spends, the fewer rows fit.

# Seven columns (symbol → time), the widest table the bot renders.
TRADES_PER_PAGE: int = 10

# Four short columns, and in /switch each row also gets its own inline button.
ACCOUNTS_PER_PAGE: int = 10

# Six columns, including a free-text account name.
ADMIN_ACCOUNTS_PER_PAGE: int = 50
