"""
app/handlers/link.py — FSM step: receive the UUID and link the account.
"""

from __future__ import annotations

import uuid

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.formatters import messages
from app.services.broker_client import BrokerClient
from app.states import LinkAccount

router = Router(name="link")


# Only treat non-command text as a candidate token while onboarding.
@router.message(LinkAccount.waiting_for_token, F.text & ~F.text.startswith("/"))
async def receive_token(
  message: Message, state: FSMContext, broker: BrokerClient
) -> None:
  raw = (message.text or "").strip()
  try:
    token = str(uuid.UUID(raw))
  except ValueError:
    await message.answer(
      "⚠️ Mã không hợp lệ. Vui lòng gửi đúng <b>UUID</b> quản trị viên đã cấp."
    )
    return

  account = await broker.link(token, message.from_user.id)
  if account is None:
    await message.answer(
      "❌ Không tìm thấy tài khoản với mã này. Kiểm tra lại hoặc liên hệ quản trị viên."
    )
    return

  await state.clear()
  await message.answer(
    "✅ <b>Liên kết thành công!</b>\n\n"
    + messages.format_account(account)
    + "\n\n"
    + messages.COMMANDS_HINT
  )


# Non-text messages (ảnh, sticker…) while onboarding: nhắc gửi UUID dạng text.
# Commands (text starting with "/") are excluded here so they fall through to
# their own handlers.
@router.message(LinkAccount.waiting_for_token, ~F.text)
async def prompt_text_token(message: Message) -> None:
  await message.answer(
    "⚠️ Vui lòng gửi mã <b>UUID</b> dưới dạng văn bản (không phải ảnh/sticker)."
  )
