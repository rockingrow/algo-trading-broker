"""Unit tests for the message presenters."""

from __future__ import annotations

from app import emojis
from app.presenters import messages
from app.utils.table import ACTIVE_MARK


def test_format_account_shows_id_and_balance():
  out = messages.UserMessages.format_account(
    {
      "account_id": "acc-1",
      "account_name": "Main",
      "account_balance": 1234.5,
      "market": "FOREX",
    }
  )
  assert "acc-1" in out
  assert "1,234.50" in out
  assert "FOREX" in out


def test_format_account_escapes_html():
  out = messages.UserMessages.format_account(
    {"account_id": "<b>x</b>", "account_name": None, "market": "FOREX"}
  )
  assert "<b>x</b>" not in out
  assert "&lt;b&gt;x&lt;/b&gt;" in out


def test_format_trades_empty():
  assert "No trades yet" in messages.UserMessages.format_trades({"data": [], "page": {}}, 7.0)


def test_format_trades_lists_rows_with_header():
  payload = {
    "data": [
      {
        "symbol": "XAUUSD",
        "action": "LONG",
        "status": "OPENED",
        "price": 100.0,
        "quantity": 1.0,
        "account_balance": 1010.0,
        "updatedAt": "2026-01-01T00:00:00Z",
      }
    ],
    "page": {"total": 1, "limit": 5, "offset": 0},
  }
  out = messages.UserMessages.format_trades(payload, 7.0)
  assert "XAUUSD" in out
  assert "LONG" in out
  assert "1 / 1" in out  # header range "(1–1 / 1)"


def test_format_trades_converts_to_local_time_with_zone_in_header():
  payload = {
    "data": [
      {
        "symbol": "XAUUSD",
        "action": "LONG",
        "status": "OPENED",
        "price": 100.0,
        "quantity": 1.0,
        "account_balance": 1010.0,
        "updatedAt": "2026-01-01T00:00:00Z",
      }
    ],
    "page": {"total": 1, "limit": 5, "offset": 0},
  }
  out = messages.UserMessages.format_trades(payload, 7.0)
  # 00:00 UTC -> 07:00 at UTC+7. The zone is stated once in the header, so the
  # row carries a compact time with no per-row label.
  assert "times in UTC+7" in out
  assert "01-01 07:00" in out


def test_format_trades_renders_a_table_with_abbreviated_status():
  payload = {
    "data": [
      {
        "symbol": "BTCUSDT",
        "action": "SHORT",
        "status": "PARTIALLY_CLOSED",
        "price": 65000.0,
        "quantity": 1.0,
        "account_balance": 10250.75,
        "updatedAt": "2026-01-02T13:45:00Z",
      }
    ],
    "page": {"total": 1, "limit": 5, "offset": 0},
  }
  out = messages.UserMessages.format_trades(payload, 7.0)
  assert "<pre>" in out
  assert "SYMBOL" in out and "BALANCE" in out
  # PARTIALLY_CLOSED would blow the column width out on every row.
  assert "PARTIAL" in out
  assert "PARTIALLY_CLOSED" not in out


def test_format_trades_numeric_columns_are_right_aligned():
  rows = [
    {
      "symbol": "A",
      "action": "LONG",
      "status": "OPENED",
      "price": p,
      "quantity": 1.0,
      "account_balance": 1.0,
      "updatedAt": "2026-01-01T00:00:00Z",
    }
    for p in (1.0, 65000.0)
  ]
  out = messages.UserMessages.format_trades(
    {"data": rows, "page": {"total": 2, "limit": 5, "offset": 0}}, 7.0
  )
  # Both prices end at the same column.
  assert "      1.00" in out
  assert "  65,000.00" in out


def test_format_accounts_list_renders_table_marking_active():
  out = messages.UserMessages.format_accounts_list(
    [
      {"id": "a1", "account_id": "acc-1", "market": "FOREX", "gateway": "MT5", "is_active": True},
      {"id": "a2", "account_id": "acc-2", "market": "CRYPTO", "gateway": "BINANCE", "is_active": False},
    ]
  )
  assert "<pre>" in out
  # Columns are padded to a common width, so each field stands on its own.
  assert f"{ACTIVE_MARK}  FOREX   MT5      acc-1" in out
  assert "   CRYPTO  BINANCE  acc-2" in out
  # The emoji star would break monospace alignment inside the table.
  assert emojis.STAR not in out


def test_format_accounts_list_empty():
  assert "No linked accounts" in messages.UserMessages.format_accounts_list([])


def test_format_accounts_list_truncates_long_account_id():
  out = messages.UserMessages.format_accounts_list(
    [
      {
        "account_id": "algotradingworker_virtual@ei0i6seknoemail.com",
        "market": "CRYPTO",
        "gateway": "BINANCE",
        "is_active": True,
      }
    ],
    with_switch_hint=False,
  )
  assert "algotradingworker_virtu…" in out
  assert "ei0i6seknoemail.com" not in out


def test_format_accounts_list_without_switch_hint():
  out = messages.UserMessages.format_accounts_list(
    [{"id": "a1", "account_id": "acc-1", "market": "FOREX", "gateway": "MT5", "is_active": True}],
    with_switch_hint=False,
  )
  assert "acc-1" in out
  assert "Tap an account" not in out


def test_format_command_result():
  out = messages.format_command_result({"action": "FLAT", "scope": "account=acc-1"})
  assert "Flat" in out
  assert "account=acc-1" in out


def test_format_command_result_names_the_command_not_the_wire_enum():
  # The user typed /prevent; BLOCK_ENTRIES is broker vocabulary.
  prevent = messages.format_command_result({"action": "BLOCK_ENTRIES", "scope": "account=a"})
  assert "Prevent" in prevent
  assert "BLOCK_ENTRIES" not in prevent

  allow = messages.format_command_result({"action": "ALLOW_ENTRIES", "scope": "account=a"})
  assert "Allow" in allow
  assert "ALLOW_ENTRIES" not in allow


def test_format_command_result_unknown_action_falls_through():
  out = messages.format_command_result({"action": "NEW_THING", "scope": "account=a"})
  assert "NEW_THING" in out


# ── admin presenters ────────────────────────────────────────────────


def test_format_accounts_admin():
  out = messages.AdminMessages.format_accounts_admin(
    [
      {
        "account_name": "Main",
        "account_id": "acc-1",
        "account_balance": 100.0,
        "market": "FOREX",
        "linked_user_ids": ["5", "6"],
        "link_token": "tok-123",
      },
      {
        "account_name": None,
        "account_id": "acc-2",
        "market": "CRYPTO",
        "linked_user_ids": [],
        "link_token": "tok-456",
      },
    ]
  )
  assert "acc-1" in out and "acc-2" in out
  # An account can have several managers now, so the mark carries the count.
  assert f"{emojis.CHECK}2" in out
  assert "—" in out  # unclaimed
  assert "tok-123" in out
  assert "tg-spoiler" in out


def test_format_accounts_admin_empty():
  assert "No accounts yet" in messages.AdminMessages.format_accounts_admin([])


def test_format_settings():
  out = messages.AdminMessages.format_settings(
    [
      {"setting": "signal_blocked", "value": "1", "state": "ENABLED"},
      {"setting": "silent_signal", "value": "0", "state": "DISABLED"},
    ]
  )
  assert "Block signal" in out
  assert "ENABLED" in out and "DISABLED" in out


def test_format_rotate_result():
  out = messages.AdminMessages.format_rotate_result(
    {"account_id": "acc-1", "link_token": "new-tok"}
  )
  assert "new-tok" in out
  assert "acc-1" in out
  assert "revoked" in out


def test_format_admin_trades_converts_to_local_time():
  payload = {
    "data": [
      {
        "symbol": "XAUUSD",
        "action": "LONG",
        "status": "OPENED",
        "price": 100.0,
        "quantity": 1.0,
        "account_balance": 1010.0,
        "updatedAt": "2026-01-01T00:00:00Z",
      }
    ],
    "page": {"total": 1, "limit": 5, "offset": 0},
  }
  out = messages.AdminMessages.format_admin_trades("acc-1", payload, -5.0)
  assert "acc-1" in out
  # 00:00 UTC -> 19:00 the previous day at UTC-5.
  assert "12-31 19:00" in out
  assert "times in UTC-5" in out


def test_format_account_created():
  out = messages.AdminMessages.format_account_created(
    {
      "account_id": "7654321",
      "market": "CRYPTO",
      "gateway": "BINANCE",
      "link_token": "new-tok",
    }
  )
  assert "7654321" in out
  assert "CRYPTO" in out
  assert "BINANCE" in out
  assert "new-tok" in out
