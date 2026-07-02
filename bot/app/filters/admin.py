"""
app/filters/admin.py — Router-level admin gate.

Attached to the admin router's message + callback observers so non-admins never
reach admin handlers (their updates simply fall through to other routers).
"""

from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import TelegramObject

from app.config import settings


class IsAdmin(BaseFilter):
  async def __call__(self, event: TelegramObject) -> bool:
    user = getattr(event, "from_user", None)
    return user is not None and user.id in settings.admin_ids
