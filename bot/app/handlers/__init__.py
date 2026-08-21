"""
app/handlers/__init__.py — Router aggregation.

- admin router: IsAdmin-gated (attached in admin.py), NO AuthMiddleware — admins
  don't need a linked account.
- start / link: onboarding, public — /start is the only command an unlinked user
  is offered, so it must stay reachable without one.
- help / trades / commands / account: get AuthMiddleware so handlers always
  receive a resolved ``account`` and unlinked users are turned away. /help is in
  there because it describes commands that all need an account; showing it to
  someone who has none only advertises what they can't run.
- trade_card: NO AuthMiddleware — its buttons act on a specific trade, which
  the broker authorises against every account the caller is linked to rather
  than the active one. See the router's docstring.
"""

from __future__ import annotations

from aiogram import Router

from app.handlers import account, admin, commands, link, start, trade_card, trades
from app.middlewares.auth import AuthMiddleware


def get_routers() -> list[Router]:
  auth = AuthMiddleware()
  for protected in (
    start.help_router,
    trades.router,
    commands.router,
    account.router,
  ):
    protected.message.middleware(auth)
    protected.callback_query.middleware(auth)

  # Admin first (its own IsAdmin gate), then onboarding, then user features.
  return [
    admin.router,
    start.router,
    start.help_router,
    link.router,
    trade_card.router,
    trades.router,
    commands.router,
    account.router,
  ]
