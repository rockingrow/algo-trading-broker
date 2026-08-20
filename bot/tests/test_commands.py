"""Tests for the command lists and the menu each kind of user gets.

Applying a menu to a chat is ``CommandMenu``'s job — see test_menu.py.
"""

from __future__ import annotations

import re

from app.commands import (
  ADMIN_COMMANDS,
  ADMIN_EXTRA_COMMANDS,
  START_ONLY_COMMANDS,
  USER_COMMANDS,
  menu_for,
)


def _names(commands):
  return [c.command for c in commands]


def test_command_list_sizes():
  assert len(USER_COMMANDS) == 13
  # 14 admin commands + 1 divider/header row.
  assert len(ADMIN_EXTRA_COMMANDS) == 15
  # Admin sees user commands plus the extras.
  assert len(ADMIN_COMMANDS) == len(USER_COMMANDS) + len(ADMIN_EXTRA_COMMANDS)
  user_names = set(_names(USER_COMMANDS))
  assert user_names.issubset(set(_names(ADMIN_COMMANDS)))
  # User commands include the completed-trade broadcast opt-in.
  assert {"subscribe", "unsubscribe"} <= user_names
  # Admin commands are prefixed and grouped after the divider.
  admin_names = set(_names(ADMIN_COMMANDS))
  assert {
    "admin_accounts",
    "admin_newaccount",
    "admin_trades",
    "admin_flat",
    "admin_rotate",
    "admin_settings",
    "admin_magicmap",
    "admin_crypto_symbols",
    "admin_crypto_leverage",
    "admin_public_chats",
    "admin_private_reply_notify",
    "admin_public_reply_notify",
    "admin_linkaccount",
    "admin_invite_url",
  } <= admin_names
  # The divider is the first admin extra so it separates the two groups.
  assert ADMIN_EXTRA_COMMANDS[0].command == "admin_help"
  # All admin command names are valid Telegram commands ([a-z0-9_], 1-32).
  assert all(re.fullmatch(r"[a-z0-9_]{1,32}", c.command) for c in ADMIN_COMMANDS)


def test_start_only_menu_is_start_and_nothing_else():
  assert _names(START_ONLY_COMMANDS) == ["start"]
  # /start carries the same description in both menus (it is the same object).
  assert START_ONLY_COMMANDS[0] is USER_COMMANDS[0]


def test_menu_for_unlinked_user_hides_every_command_but_start():
  assert _names(menu_for(linked=False, is_admin=False)) == ["start"]
  # /help included: it is a tour of commands that all need an account.
  assert "help" not in _names(menu_for(linked=False, is_admin=False))


def test_menu_for_linked_user_is_the_full_user_menu():
  assert _names(menu_for(linked=True, is_admin=False)) == _names(USER_COMMANDS)


def test_menu_for_unlinked_admin_keeps_the_admin_half():
  names = _names(menu_for(linked=False, is_admin=True))
  # Admin commands never needed a linked account, so only the user half goes.
  assert names == ["start"] + _names(ADMIN_EXTRA_COMMANDS)


def test_menu_for_linked_admin_is_the_full_admin_menu():
  assert _names(menu_for(linked=True, is_admin=True)) == _names(ADMIN_COMMANDS)


def test_menu_for_returns_a_copy_callers_cannot_corrupt():
  menu = menu_for(linked=True, is_admin=False)
  menu.clear()
  assert len(USER_COMMANDS) == 13
