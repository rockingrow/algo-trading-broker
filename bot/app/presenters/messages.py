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
# not BLOCK_SIGNAL; echoing the enum back leaks broker vocabulary at them.
_ACTION_LABEL = {
  "FLAT": "Flat",
  "BLOCK_SIGNAL": "Prevent",
  "ALLOW_SIGNAL": "Allow",
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


def _page_range(page: dict[str, Any], shown: int) -> str:
  """``"1–8 / 23"`` — which slice of a paged list this message is showing.

  Every table header carries one, so a user looking at page 3 can tell that
  the rows above aren't the whole story.
  """
  offset = int(page.get("offset", 0))
  total = int(page.get("total", shown))
  return f"{offset + 1}–{offset + shown} / {total}"


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
    "/unlink — Unlink active account\n"
    "/subscribe — Receive trade broadcasts\n"
    "/unsubscribe — Stop trade broadcasts"
  )

  @staticmethod
  def format_broadcast_subscription(subscribed: bool) -> str:
    if subscribed:
      return (
        f"{emojis.CHECK} <b>Subscribed.</b>\n\n"
        "You'll now get a DM here whenever one of your linked accounts "
        "completes (closes) a trade. Use /unsubscribe to stop."
      )
    return (
      f"{emojis.CHECK} <b>Unsubscribed.</b>\n\n"
      "You'll no longer get completed-trade alerts. Use /subscribe to turn "
      "them back on."
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
  def format_accounts_list(
    accounts: list[dict[str, Any]],
    page: dict[str, Any],
    with_switch_hint: bool = True,
  ) -> str:
    """One table row per linked account, starring the currently active one.

    *accounts* is one page's worth of rows and *page* the metadata describing
    it (see ``utils.pagination``) — the header states the range so a starred
    account on another page isn't mistaken for missing.

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
    lines = [
      f"<b>{emojis.FOLDER} Your accounts</b> ({_page_range(page, len(accounts))})",
      "",
      table,
    ]
    if with_switch_hint:
      lines.append("\n<i>Tap an account below to make it active.</i>")
    return "\n".join(lines)

  @staticmethod
  def format_trades(payload: dict[str, Any], tz_offset_hours: float) -> str:
    data = payload.get("data") or []
    page = payload.get("page") or {}

    if not data:
      return f"{emojis.EMPTY_MAILBOX} No trades yet."

    # The zone is stated once here rather than repeated on every row.
    header = (
      f"<b>{emojis.CHART} Trades</b> ({_page_range(page, len(data))}) · "
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
  def format_accounts_admin(
    accounts: list[dict[str, Any]], page: dict[str, Any]
  ) -> str:
    """One page of the full account table (see ``utils.pagination``)."""
    if not accounts:
      return f"{emojis.EMPTY_MAILBOX} No accounts yet."
    table = render_table(
      headers=("MARKET", "GATEWAY", "ACCOUNT", "NAME", "BALANCE", "USERS"),
      rows=[
        (
          a.get("market"),
          a.get("gateway") or "?",
          a.get("account_id"),
          a.get("account_name") or "—",
          _fmt_num(a.get("account_balance")),
          # An account may be managed by several people now, so show how many
          # rather than a yes/no mark.
          len(a.get("linked_user_ids") or []),
        )
        for a in accounts
      ],
      aligns=("l", "l", "l", "l", "r", "r"),
      max_widths=(None, None, 24, 20, None, None),
    )
    lines = [
      f"<b>{emojis.FOLDER} Accounts</b> ({_page_range(page, len(accounts))})",
      "",
      table,
    ]

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
    if not states:
      return f"{emojis.EMPTY_MAILBOX} No settings found."
    table = render_table(
      headers=("STATE", "SETTING"),
      rows=[
        (
          s.get("state"),
          AdminMessages.SETTING_META.get(str(s.get("setting")), (str(s.get("setting")), ""))[0],
        )
        for s in states
      ],
    )
    return (
      f"<b>{emojis.GEAR} Broker settings</b>\n\n{table}\n\n"
      "<i>Tap a button below to toggle.</i>"
    )

  @staticmethod
  def format_account_created(
    account: dict[str, Any], invite_url: Optional[str] = None
  ) -> str:
    """Fresh account plus its link token, and — when the deep link could be
    built — the one-tap invite URL, saving a trip through /admin_invite_url.

    *invite_url* is None when the bot username wasn't reachable; the raw token
    above still works, so the line is simply omitted rather than erroring."""
    invite = (
      f"{emojis.LINK} Invite link:\n"
      f"<tg-spoiler>{_esc(invite_url)}</tg-spoiler>\n\n"
      if invite_url
      else ""
    )
    return (
      f"{emojis.CHECK} <b>Account created</b>\n"
      f"• ID: <code>{_esc(account.get('account_id'))}</code>\n"
      f"• Market: {_esc(account.get('market'))}\n"
      f"• Gateway: {_esc(account.get('gateway'))}\n\n"
      f"{emojis.KEY} Link token:\n"
      # No <code> inside the spoiler: Telegram renders code/pre entities through
      # the spoiler overlay, so the token stays readable before it is tapped.
      f"<tg-spoiler>{_esc(account.get('link_token'))}</tg-spoiler>\n\n"
      f"{invite}"
    )

  @staticmethod
  def format_rotate_result(result: dict[str, Any]) -> str:
    return (
      f"{emojis.KEY} New link token for <code>{_esc(result.get('account_id'))}</code>:\n"
      f"<tg-spoiler><code>{_esc(result.get('link_token'))}</code></tg-spoiler>\n\n"
      "<i>Previous tokens have been revoked and every Telegram user that was "
      "linked to this account has been unlinked. Send this new token to whoever "
      "should have access now — the new token is the only way back in.</i>"
    )

  @staticmethod
  def format_invite_url(url: str, account: Optional[dict[str, Any]] = None) -> str:
    """One-tap invite link. The link token rides inside the URL, so the URL is
    itself the bearer secret — spoilered like the raw tokens above.

    *account* is omitted when the admin passed a bare code, since nothing was
    looked up to name it."""
    who = (
      f"• Account: <code>{_esc(account.get('account_id'))}</code> "
      f"({_esc(account.get('market'))}/{_esc(account.get('gateway'))})\n"
      if account is not None
      else ""
    )
    return (
      f"{emojis.LINK} <b>Invite link</b>\n"
      f"{who}\n"
      f"<tg-spoiler>{_esc(url)}</tg-spoiler>\n\n"
      "<i>Send this to the end user. Opening it starts the bot and links the "
      "account straight away — no UUID to type. It carries the link token, so "
      "share it privately; /admin_rotate revokes it.</i>"
    )

  ADMIN_HELP = (
    f"{emojis.GEAR} <b>Admin commands</b>\n"
    "/admin_accounts — Account list\n"
    "/admin_newaccount — Register a new account\n"
    "/admin_trades — Trades for an account\n"
    "/admin_flat — FLAT system-wide / account\n"
    "/admin_rotate — Rotate token + unlink users\n"
    "/admin_settings — Broker settings\n"
    "/admin_linkaccount — Link a Telegram user to an account\n"
    "/admin_invite_url — One-tap invite link for an account"
  )

  @staticmethod
  def format_linked_account(account: dict[str, Any], telegram_user_id: int) -> str:
    """Confirmation after an admin binds a Telegram user to an account."""
    users = account.get("linked_user_ids") or []
    return (
      f"{emojis.CHECK} <b>Telegram user linked</b>\n"
      f"• Account: <code>{_esc(account.get('account_id'))}</code> "
      f"({_esc(account.get('market'))}/{_esc(account.get('gateway'))})\n"
      f"• Telegram user: <code>{_esc(telegram_user_id)}</code>\n"
      f"• Linked users now: {_esc(len(users))}"
    )
