"""Unit tests for the message presenters."""

from __future__ import annotations

from app import emojis
from app.presenters import messages


def test_format_account_shows_id_and_balance():
  out = messages.UserMessages.format_account(
    {
      "account_id": "acc-1",
      "account_name": "Main",
      "account_balance": 1234.5,
      "market": "FOREX",
      "telegram_user_id": 7,
    }
  )
  assert "acc-1" in out
  assert "1,234.50" in out
  assert "Status: linked" in out


def test_format_account_escapes_html():
  out = messages.UserMessages.format_account(
    {"account_id": "<b>x</b>", "account_name": None, "market": "FOREX"}
  )
  assert "<b>x</b>" not in out
  assert "&lt;b&gt;x&lt;/b&gt;" in out


def test_format_trades_empty():
  assert "No trades yet" in messages.UserMessages.format_trades({"data": [], "page": {}})


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
  out = messages.UserMessages.format_trades(payload)
  assert "XAUUSD" in out
  assert "LONG" in out
  assert "1 / 1" in out  # header range "(1–1 / 1)"


def test_format_accounts_list_marks_active():
  out = messages.UserMessages.format_accounts_list(
    [
      {"id": "a1", "account_id": "acc-1", "market": "FOREX", "gateway": "MT5", "is_active": True},
      {"id": "a2", "account_id": "acc-2", "market": "CRYPTO", "gateway": "BINANCE", "is_active": False},
    ]
  )
  assert "FOREX-MT5-acc-1" in out
  assert "CRYPTO-BINANCE-acc-2" in out
  assert emojis.STAR in out


def test_format_accounts_list_empty():
  assert "No linked accounts" in messages.UserMessages.format_accounts_list([])


def test_format_command_result():
  out = messages.format_command_result({"action": "FLAT", "scope": "account=acc-1"})
  assert "FLAT" in out
  assert "account=acc-1" in out


# ── admin presenters ────────────────────────────────────────────────


def test_format_accounts_admin():
  out = messages.AdminMessages.format_accounts_admin(
    [
      {
        "account_name": "Main",
        "account_id": "acc-1",
        "account_balance": 100.0,
        "market": "FOREX",
        "telegram_user_id": 5,
        "telegram_link_token": "tok-123",
      },
      {
        "account_name": None,
        "account_id": "acc-2",
        "market": "CRYPTO",
        "telegram_user_id": None,
        "telegram_link_token": "tok-456",
      },
    ]
  )
  assert "acc-1" in out and "acc-2" in out
  assert emojis.CHECK in out  # linked
  assert "—" in out  # unlinked
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
    {"account_id": "acc-1", "telegram_link_token": "new-tok"}
  )
  assert "new-tok" in out
  assert "acc-1" in out
  assert "revoked" in out


def test_format_account_created():
  out = messages.AdminMessages.format_account_created(
    {
      "account_id": "7654321",
      "market": "CRYPTO",
      "gateway": "BINANCE",
      "telegram_link_token": "new-tok",
    }
  )
  assert "7654321" in out
  assert "CRYPTO" in out
  assert "BINANCE" in out
  assert "new-tok" in out
