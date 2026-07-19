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
