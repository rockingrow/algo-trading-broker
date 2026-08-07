"""
app/commands.py — Command lists + which menu a given user should see.

Three lists:
- START_ONLY_COMMANDS  → what an *unlinked* user gets: /start, nothing else.
- USER_COMMANDS        → the full enduser menu, once an account is linked.
- ADMIN_EXTRA_COMMANDS → appended for the ids in TELEGRAM_ADMIN_IDS.

``menu_for`` picks between them. Applying a menu to a chat — and keeping it in
step with the user's link status — is ``app/services/menu.py``'s job.
"""

from __future__ import annotations

from aiogram.types import BotCommand

# The only command that works without an account behind it, so the only one an
# unlinked user is shown (see ``menu_for``). Shared with USER_COMMANDS below so
# the entry can't drift between the two menus.
START_COMMAND = BotCommand(command="start", description="Link account")

START_ONLY_COMMANDS = [START_COMMAND]

USER_COMMANDS = [
  START_COMMAND,
  BotCommand(command="status", description="Account info + open positions"),
  BotCommand(command="trades", description="Recent trades"),
  BotCommand(command="flat", description="Close all positions"),
  BotCommand(command="prevent", description="Block new orders"),
  BotCommand(command="allow", description="Allow new orders"),
  BotCommand(command="myaccounts", description="List linked accounts"),
  BotCommand(command="link", description="Add another account"),
  BotCommand(command="switch", description="Change active account"),
  BotCommand(command="unlink", description="Unlink active account"),
  BotCommand(command="subscribe", description="Get live trade alerts"),
  BotCommand(command="unsubscribe", description="Stop live trade alerts"),
  BotCommand(command="help", description="Help"),
]

# Visual separator between the user commands and the admin commands in the
# admin menu. Telegram command names may only contain [a-z0-9_] (a literal
# "-----—" divider isn't a valid command), so the divider is a real command —
# ``/admin_help``, which lists the admin commands — carrying a dashed
# description that reads as a section header in the menu.
ADMIN_DIVIDER = BotCommand(
  command="admin_help", description="───────── ADMIN ─────────"
)

# Admin commands are prefixed ``admin_`` so they group under the divider and
# read as a distinct set. The handlers also accept the legacy un-prefixed names
# (see app/handlers/admin.py) so existing muscle memory keeps working; only the
# prefixed form is advertised in the menu.
ADMIN_EXTRA_COMMANDS = [
  ADMIN_DIVIDER,
  BotCommand(command="admin_accounts", description="[ADMIN] Account list"),
  BotCommand(command="admin_newaccount", description="[ADMIN] Register a new account"),
  BotCommand(command="admin_trades", description="[ADMIN] Trades for an account"),
  BotCommand(command="admin_flat", description="[ADMIN] FLAT system-wide / account"),
  BotCommand(command="admin_rotate", description="[ADMIN] Rotate token + unlink users"),
  BotCommand(command="admin_settings", description="[ADMIN] Broker settings"),
  BotCommand(command="admin_magicmap", description="[ADMIN] Edit strategy magic map"),
  BotCommand(
    command="admin_crypto_symbols", description="[ADMIN] Crypto allowed symbols"
  ),
  BotCommand(
    command="admin_crypto_leverage", description="[ADMIN] Crypto max leverage"
  ),
  BotCommand(
    command="admin_public_chats", description="[ADMIN] Public broadcast chats"
  ),
  BotCommand(command="admin_linkaccount", description="[ADMIN] Link a Telegram user"),
  BotCommand(command="admin_invite_url", description="[ADMIN] One-tap invite link"),
]

ADMIN_COMMANDS = USER_COMMANDS + ADMIN_EXTRA_COMMANDS


def menu_for(*, linked: bool, is_admin: bool) -> list[BotCommand]:
  """The command menu a user should be seeing right now.

  Every user command needs an account behind it — an unlinked user who taps one
  only ever gets "you haven't linked an account yet", /help included, since the
  help text is a tour of commands they cannot run. So until they link, the menu
  is trimmed to the one command that gets them somewhere: /start.

  The admin extras are not trimmed: admin commands never required a linked
  account (see handlers/admin.py), so an unlinked admin keeps them and loses
  only the user half of their menu.
  """
  commands = list(USER_COMMANDS if linked else START_ONLY_COMMANDS)
  if is_admin:
    commands += ADMIN_EXTRA_COMMANDS
  return commands
