"""
app/handlers/__init__.py — Router aggregation.

Public routers (start, link) handle onboarding. Protected routers (trades,
commands, account) get the AuthMiddleware attached so their handlers always
receive a resolved ``account`` and unlinked users are turned away.
"""

from __future__ import annotations

from aiogram import Router

from app.handlers import account, commands, link, start, trades
from app.middlewares.auth import AuthMiddleware


def get_routers() -> list[Router]:
  auth = AuthMiddleware()
  for protected in (trades.router, commands.router, account.router):
    protected.message.middleware(auth)
    protected.callback_query.middleware(auth)

  # Order matters: onboarding first, then the protected feature routers.
  return [
    start.router,
    link.router,
    trades.router,
    commands.router,
    account.router,
  ]
