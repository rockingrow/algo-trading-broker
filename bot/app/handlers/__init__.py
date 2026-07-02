"""
app/handlers/__init__.py — Router aggregation.

- admin router: IsAdmin-gated (attached in admin.py), NO AuthMiddleware — admins
  don't need a linked account.
- start / link: onboarding, public.
- trades / commands / account: get AuthMiddleware so handlers always receive a
  resolved ``account`` and unlinked users are turned away.
"""

from __future__ import annotations

from aiogram import Router

from app.handlers import account, admin, commands, link, start, trades
from app.middlewares.auth import AuthMiddleware


def get_routers() -> list[Router]:
  auth = AuthMiddleware()
  for protected in (trades.router, commands.router, account.router):
    protected.message.middleware(auth)
    protected.callback_query.middleware(auth)

  # Admin first (its own IsAdmin gate), then onboarding, then user features.
  return [
    admin.router,
    start.router,
    link.router,
    trades.router,
    commands.router,
    account.router,
  ]
