"""
app/formatters/messages.py — Render broker API payloads into Telegram HTML.

All dynamic values are HTML-escaped. Keep presentation here so handlers stay
about flow control, not string building.
"""

from __future__ import annotations

import html
from typing import Any, Optional

from app import emojis

COMMANDS_HINT = (
  "<b>Available commands</b>\n"
  "/trades — Recent trades\n"
  "/flat — Close all positions\n"
  "/prevent — Block new orders\n"
  "/allow — Allow new orders\n"
  "/status — Account info\n"
  "/unlink — Unlink account"
)

HELP_TEXT = (
  f"{emojis.ROBOT} <b>Trading Bot</b>\n\n"
  "This bot helps you track trades and control your account.\n\n"
  "Start with /start and enter the <b>UUID</b> code your admin gave you to link.\n\n"
  + COMMANDS_HINT
)

_STATUS_EMOJI = {
  "OPENED": emojis.GREEN_CIRCLE,
  "CLOSED": emojis.WHITE_CIRCLE,
  "FLAT": emojis.BLUE_CIRCLE,
  "REJECTED": emojis.RED_CIRCLE,
  "PARTIALLY_CLOSED": emojis.YELLOW_CIRCLE,
}


def _esc(value: Any) -> str:
  return html.escape(str(value)) if value is not None else "—"


def _fmt_num(value: Optional[float]) -> str:
  if value is None:
    return "—"
  try:
    return f"{float(value):,.2f}"
  except (TypeError, ValueError):
    return _esc(value)


def format_account(account: dict[str, Any]) -> str:
  linked = "linked" if account.get("telegram_user_id") else "not linked"
  return (
    f"<b>{emojis.FOLDER} Account</b>\n"
    f"• ID: <code>{_esc(account.get('account_id'))}</code>\n"
    f"• Name: {_esc(account.get('account_name'))}\n"
    f"• Balance: <b>{_fmt_num(account.get('account_balance'))}</b>\n"
    f"• Market: {_esc(account.get('market_type'))}\n"
    f"• Status: {linked}"
  )


def _format_trade_line(trade: dict[str, Any]) -> str:
  status = str(trade.get("status", ""))
  emoji = _STATUS_EMOJI.get(status, "•")
  updated = str(trade.get("updatedAt", ""))[:19].replace("T", " ")
  return (
    f"{emoji} <b>{_esc(trade.get('symbol'))}</b> "
    f"{_esc(trade.get('action'))} · {_esc(status)}\n"
    f"   price {_fmt_num(trade.get('price'))} · qty {_fmt_num(trade.get('quantity'))} "
    f"· balance {_fmt_num(trade.get('account_balance'))}\n"
    f"   <i>{_esc(updated)}</i>"
  )


def format_trades(payload: dict[str, Any]) -> str:
  data = payload.get("data") or []
  page = payload.get("page") or {}
  total = page.get("total", len(data))

  if not data:
    return f"{emojis.EMPTY_MAILBOX} No trades yet."

  offset = int(page.get("offset", 0))
  start = offset + 1
  end = offset + len(data)

  header = f"<b>{emojis.CHART} Trades</b> ({start}–{end} / {total})"
  lines = [_format_trade_line(t) for t in data]
  return header + "\n\n" + "\n".join(lines)


def format_command_result(result: dict[str, Any]) -> str:
  action = _esc(result.get("action"))
  scope = _esc(result.get("scope"))
  return (
    f"{emojis.CHECK} Command <b>{action}</b> sent\n"
    f"Scope: <code>{scope}</code>\n\n"
    "<i>The command has been dispatched to the worker via the broker.</i>"
  )


# ── Admin ───────────────────────────────────────────────────────────

# setting key (from broker) → (display label, toggle endpoint slug).
# Single source shared with keyboards.settings_keyboard.
SETTING_META: dict[str, tuple[str, str]] = {
  "signal_blocked": ("Block signal", "block-signal"),
  "silent_signal": ("Mute notifications", "silent-signal"),
  "notification_include_signal_raw": ("Include raw in notification", "include-signal-raw"),
}


def format_accounts_admin(accounts: list[dict[str, Any]]) -> str:
  if not accounts:
    return f"{emojis.EMPTY_MAILBOX} No accounts yet."
  lines = [f"<b>{emojis.FOLDER} Accounts</b> ({len(accounts)})", ""]
  for a in accounts:
    linked = emojis.CHECK if a.get("telegram_user_id") else "—"
    lines.append(
      f"{linked} <b>{_esc(a.get('account_name'))}</b> "
      f"<code>{_esc(a.get('account_id'))}</code>\n"
      f"   balance {_fmt_num(a.get('account_balance'))} · {_esc(a.get('market_type'))}"
    )
    token = a.get("telegram_link_token")
    if token:
      # Hidden in a spoiler, wrapped in code for tap-to-copy to hand to the end user.
      lines.append(f"   token: <tg-spoiler><code>{_esc(token)}</code></tg-spoiler>")
  return "\n".join(lines)


def format_admin_trades(account_id: str, payload: dict[str, Any]) -> str:
  return f"<b>Account</b> <code>{_esc(account_id)}</code>\n\n" + format_trades(
    payload
  )


def format_settings(states: list[dict[str, Any]]) -> str:
  lines = [f"<b>{emojis.GEAR} Broker settings</b>", ""]
  for s in states:
    label = SETTING_META.get(str(s.get("setting")), (str(s.get("setting")), ""))[0]
    on = str(s.get("state")) == "ENABLED"
    dot = emojis.GREEN_CIRCLE if on else emojis.WHITE_CIRCLE
    lines.append(f"{dot} {_esc(label)}: <b>{_esc(s.get('state'))}</b>")
  lines.append("\n<i>Tap a button below to toggle.</i>")
  return "\n".join(lines)


def format_rotate_result(result: dict[str, Any]) -> str:
  return (
    f"{emojis.KEY} New link token for <code>{_esc(result.get('account_id'))}</code>:\n"
    f"<tg-spoiler><code>{_esc(result.get('telegram_link_token'))}</code></tg-spoiler>\n\n"
    "<i>The old token has been revoked. Send this new token to the end user.</i>"
  )
