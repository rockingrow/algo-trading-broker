"""Unit tests for the message formatters."""

from __future__ import annotations

from app.formatters import messages


def test_format_account_shows_id_and_balance():
  out = messages.format_account(
    {
      "account_id": "acc-1",
      "account_name": "Main",
      "account_balance": 1234.5,
      "market_type": "FOREX",
      "telegram_user_id": 7,
    }
  )
  assert "acc-1" in out
  assert "1,234.50" in out
  assert "đã liên kết" in out


def test_format_account_escapes_html():
  out = messages.format_account(
    {"account_id": "<b>x</b>", "account_name": None, "market_type": "FOREX"}
  )
  assert "<b>x</b>" not in out
  assert "&lt;b&gt;x&lt;/b&gt;" in out


def test_format_trades_empty():
  assert "Chưa có giao dịch" in messages.format_trades({"data": [], "page": {}})


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
  out = messages.format_trades(payload)
  assert "XAUUSD" in out
  assert "LONG" in out
  assert "1 / 1" in out  # header range "(1–1 / 1)"


def test_format_command_result():
  out = messages.format_command_result({"action": "FLAT", "scope": "account=acc-1"})
  assert "FLAT" in out
  assert "account=acc-1" in out
