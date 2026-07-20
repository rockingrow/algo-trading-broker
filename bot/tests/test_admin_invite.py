"""Tests for /admin_invite_url — turning a link token into a t.me deep link."""

from __future__ import annotations

from aiogram.filters.command import CommandObject

from app.handlers.admin import cb_invite_url_pick, cmd_invite_url
from app.presenters.messages import AdminMessages

DASHED = "b5dc0374-9639-4861-acf4-2d239aa5c1b4"
HEX = "b5dc037496394861acf42d239aa5c1b4"

ACCOUNT = {
  "id": "11111111-2222-3333-4444-555555555555",
  "account_id": "acc-1",
  "market": "crypto",
  "gateway": "binance",
  "link_token": DASHED,
}


class FakeMe:
  username = "my_test_bot"


class FakeBot:
  async def me(self):
    return FakeMe()


class FakeMessage:
  def __init__(self):
    self.bot = FakeBot()
    self.answers = []
    self.keyboards = []

  async def answer(self, text, reply_markup=None, **kwargs):
    self.answers.append(text)
    self.keyboards.append(reply_markup)

  @property
  def last(self):
    return self.answers[-1]


class FakeCall:
  def __init__(self, data):
    self.data = data
    self.message = FakeMessage()
    self.alerts = []

  async def answer(self, text=None, show_alert=False):
    self.alerts.append(text)


class FakeAdminBroker:
  def __init__(self, accounts):
    self.accounts = accounts

  async def admin_list_accounts(self):
    return self.accounts


async def test_invite_url_from_an_explicit_code():
  message = FakeMessage()
  await cmd_invite_url(
    message, CommandObject(command="admin_invite_url", args=DASHED), FakeAdminBroker([])
  )
  # Bare hex payload, so the shared URL stays as short as Telegram allows.
  assert f"https://t.me/my_test_bot?start={HEX}" in message.last


async def test_invite_url_rejects_a_non_uuid_code():
  message = FakeMessage()
  await cmd_invite_url(
    message, CommandObject(command="admin_invite_url", args="junk"), FakeAdminBroker([])
  )
  assert "not a valid UUID code" in message.last
  assert "t.me" not in message.last


async def test_invite_url_without_an_arg_offers_the_account_picker():
  message = FakeMessage()
  await cmd_invite_url(
    message, CommandObject(command="admin_invite_url", args=None), FakeAdminBroker([ACCOUNT])
  )
  assert "Choose an account" in message.last
  buttons = message.keyboards[-1].inline_keyboard
  # The picker carries the row UUID — never the link token, which is a bearer
  # secret and would otherwise sit in the client's update history.
  assert buttons[0][0].callback_data == f"ainv:{ACCOUNT['id']}"
  assert DASHED not in buttons[0][0].callback_data


async def test_invite_url_picker_resolves_the_token_and_names_the_account():
  call = FakeCall(f"ainv:{ACCOUNT['id']}")
  await cb_invite_url_pick(call, FakeAdminBroker([ACCOUNT]))
  text = call.message.last
  assert f"https://t.me/my_test_bot?start={HEX}" in text
  assert "acc-1" in text
  assert "crypto/binance" in text


async def test_invite_url_picker_on_a_vanished_account():
  call = FakeCall("ainv:does-not-exist")
  await cb_invite_url_pick(call, FakeAdminBroker([ACCOUNT]))
  assert call.message.answers == []
  assert call.alerts == ["Account not found"]


def test_format_invite_url_spoilers_the_link_and_omits_unknown_accounts():
  text = AdminMessages.format_invite_url("https://t.me/b?start=abc")
  # The URL carries the token, so the whole link is the secret.
  assert "<tg-spoiler>https://t.me/b?start=abc</tg-spoiler>" in text
  assert "Account:" not in text
