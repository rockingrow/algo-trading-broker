"""
app/presenters/messages.py — Render broker API payloads into Telegram HTML.

All dynamic values are HTML-escaped. Keep presentation here so handlers stay
about flow control, not string building.
"""

from __future__ import annotations

import html
from typing import Any, Optional

from app import emojis
from app.utils.table import ACTIVE_MARK, render_table
from app.utils.timezone import SHORT_TIME_FMT, format_local_time, format_utc_label

# Broker wire action → the bot command that triggered it. Users type /prevent,
# not BLOCK_ENTRIES; echoing the enum back leaks broker vocabulary at them.
_ACTION_LABEL = {
  "FLAT": "Flat",
  "BLOCK_ENTRIES": "Prevent",
  "ALLOW_ENTRIES": "Allow",
}

# Table cells must stay single-width, so the status shows as an abbreviation
# rather than the colour-coded circle a free-form line could afford.
_STATUS_LABEL = {
  "OPENED": "OPEN",
  "CLOSED": "CLOSED",
  "FLAT": "FLAT",
  "REJECTED": "REJECT",
  "PARTIALLY_CLOSED": "PARTIAL",
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


def _trade_row(trade: dict[str, Any], tz_offset_hours: float) -> tuple[str, ...]:
  """One trade as raw table cells — render_table escapes and pads them."""
  status = str(trade.get("status", ""))
  return (
    str(trade.get("symbol") or "—"),
    str(trade.get("action") or "—"),
    _STATUS_LABEL.get(status, status),
    _fmt_num(trade.get("price")),
    _fmt_num(trade.get("quantity")),
    _fmt_num(trade.get("account_balance")),
    format_local_time(
      trade.get("updatedAt"), tz_offset_hours, fmt=SHORT_TIME_FMT, with_label=False
    ),
  )


def format_command_result(result: dict[str, Any]) -> str:
  # Unknown actions fall through escaped, so a new broker enum still displays.
  action = _ACTION_LABEL.get(str(result.get("action")), _esc(result.get("action")))
  scope = _esc(result.get("scope"))
  return (
    f"{emojis.CHECK} Command <b>{action}</b> done\n"
    f"Scope: <code>{scope}</code>\n\n"
    "<i>The command has been dispatched to the worker via the broker.</i>"
  )


class UserMessages:
  """Message presenters for end-user (non-admin) handlers."""

  COMMANDS_HINT = (
    "<b>Available commands</b>\n"
    "/trades — Recent trades\n"
    "/flat — Close all positions\n"
    "/prevent — Block new orders\n"
    "/allow — Allow new orders\n"
    "/status — Account info\n"
    "/myaccounts — List linked accounts\n"
    "/link — Add another account\n"
    "/switch — Change active account\n"
    "/unlink — Unlink active account"
  )

  HELP_TEXT = (
    f"{emojis.ROBOT} <b>Trading Bot</b>\n\n"
    "This bot helps you track trades and control your account.\n\n"
    "Start with /start and enter the <b>UUID</b> code your admin gave you to link.\n\n"
    + COMMANDS_HINT
  )

  @staticmethod
  def format_account(account: dict[str, Any]) -> str:
    # No link status line: this only ever renders an account the caller is
    # already linked to, so it could never say anything but "linked".
    return (
      f"<b>{emojis.FOLDER} Account</b>\n"
      f"• ID: <code>{_esc(account.get('account_id'))}</code>\n"
      f"• Name: {_esc(account.get('account_name'))}\n"
      f"• Balance: <b>{_fmt_num(account.get('account_balance'))}</b>\n"
      f"• Market: {_esc(account.get('market'))}"
    )

  @staticmethod
  def format_accounts_list(accounts: list[dict[str, Any]], with_switch_hint: bool = True) -> str:
    """One table row per linked account, starring the currently active one.

    ``with_switch_hint`` appends the "tap to switch" line, relevant only when
    the message is paired with the /switch inline picker keyboard.
    """
    if not accounts:
      return f"{emojis.EMPTY_MAILBOX} No linked accounts."
    table = render_table(
      headers=(ACTIVE_MARK, "MARKET", "GATEWAY", "ACCOUNT"),
      rows=[
        (
          ACTIVE_MARK if a.get("is_active") else "",
          a.get("market"),
          a.get("gateway") or "?",
          a.get("account_id"),
        )
        for a in accounts
      ],
      # ACCOUNT is capped because ids can be long (an email, say) — the rest
      # are short enums that size themselves.
      max_widths=(1, None, None, 24),
    )
    lines = [f"<b>{emojis.FOLDER} Your accounts</b> ({len(accounts)})", "", table]
    if with_switch_hint:
      lines.append("\n<i>Tap an account below to make it active.</i>")
    return "\n".join(lines)

  @staticmethod
  def format_trades(payload: dict[str, Any], tz_offset_hours: float) -> str:
    data = payload.get("data") or []
    page = payload.get("page") or {}
    total = page.get("total", len(data))

    if not data:
      return f"{emojis.EMPTY_MAILBOX} No trades yet."

    offset = int(page.get("offset", 0))
    start = offset + 1
    end = offset + len(data)

    # The zone is stated once here rather than repeated on every row.
    header = (
      f"<b>{emojis.CHART} Trades</b> ({start}–{end} / {total}) · "
      f"times in {format_utc_label(tz_offset_hours)}"
    )
    table = render_table(
      headers=("SYMBOL", "ACTION", "STATUS", "PRICE", "QTY", "BALANCE", "TIME"),
      rows=[_trade_row(t, tz_offset_hours) for t in data],
      aligns=("l", "l", "l", "r", "r", "r", "l"),
      max_widths=(12, 6, 7, None, None, None, None),
    )
    return header + "\n\n" + table


class AdminMessages:
  """Message presenters for admin-only handlers."""

  # setting key (from broker) → (display label, toggle endpoint slug).
  # Single source shared with keyboards.settings_keyboard.
  SETTING_META: dict[str, tuple[str, str]] = {
    "signal_blocked": ("Block signal", "block-signal"),
    "silent_signal": ("Mute notifications", "silent-signal"),
    "notification_include_signal_raw": ("Include raw in notification", "include-signal-raw"),
  }

  @staticmethod
  def format_accounts_admin(accounts: list[dict[str, Any]]) -> str:
    if not accounts:
      return f"{emojis.EMPTY_MAILBOX} No accounts yet."
    lines = [f"<b>{emojis.FOLDER} Accounts</b> ({len(accounts)})", ""]
    for a in accounts:
      # An account may be managed by several people now, so show how many
      # rather than a yes/no mark.
      user_count = len(a.get("linked_user_ids") or [])
      linked = f"{emojis.CHECK}{user_count}" if user_count else "—"
      lines.append(
        f"{linked} <b>{_esc(a.get('account_name'))}</b> "
        f"<code>{_esc(a.get('account_id'))}</code>\n"
        f"   balance {_fmt_num(a.get('account_balance'))} · {_esc(a.get('market'))}"
      )
      token = a.get("link_token")
      if token:
        # Hidden in a spoiler, wrapped in code for tap-to-copy to hand to the end user.
        lines.append(f"   token: <tg-spoiler><code>{_esc(token)}</code></tg-spoiler>")
    return "\n".join(lines)

  @staticmethod
  def format_admin_trades(
    account_id: str, payload: dict[str, Any], tz_offset_hours: float
  ) -> str:
    return f"<b>Account</b> <code>{_esc(account_id)}</code>\n\n" + UserMessages.format_trades(
      payload, tz_offset_hours
    )

  @staticmethod
  def format_settings(states: list[dict[str, Any]]) -> str:
    lines = [f"<b>{emojis.GEAR} Broker settings</b>", ""]
    for s in states:
      label = AdminMessages.SETTING_META.get(str(s.get("setting")), (str(s.get("setting")), ""))[0]
      on = str(s.get("state")) == "ENABLED"
      dot = emojis.GREEN_CIRCLE if on else emojis.WHITE_CIRCLE
      lines.append(f"{dot} {_esc(label)}: <b>{_esc(s.get('state'))}</b>")
    lines.append("\n<i>Tap a button below to toggle.</i>")
    return "\n".join(lines)

  @staticmethod
  def format_account_created(account: dict[str, Any]) -> str:
    return (
      f"{emojis.CHECK} <b>Account created</b>\n"
      f"• ID: <code>{_esc(account.get('account_id'))}</code>\n"
      f"• Market: {_esc(account.get('market'))}\n"
      f"• Gateway: {_esc(account.get('gateway'))}\n\n"
      f"{emojis.KEY} Link token:\n"
      f"<tg-spoiler><code>{_esc(account.get('link_token'))}</code></tg-spoiler>\n\n"
      "<i>Send this token to the end user so they can link via /start.</i>"
    )

  @staticmethod
  def format_rotate_result(result: dict[str, Any]) -> str:
    return (
      f"{emojis.KEY} New link token for <code>{_esc(result.get('account_id'))}</code>:\n"
      f"<tg-spoiler><code>{_esc(result.get('link_token'))}</code></tg-spoiler>\n\n"
      "<i>Previous tokens have been revoked. Send this new token to the end "
      "user. Anyone already linked keeps their access.</i>"
    )
