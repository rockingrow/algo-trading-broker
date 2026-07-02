"""
app/formatters/messages.py — Render broker API payloads into Telegram HTML.

All dynamic values are HTML-escaped. Keep presentation here so handlers stay
about flow control, not string building.
"""

from __future__ import annotations

import html
from typing import Any, Optional

COMMANDS_HINT = (
  "<b>Lệnh khả dụng</b>\n"
  "/trades — Giao dịch gần đây\n"
  "/flat — Đóng toàn bộ vị thế\n"
  "/prevent — Chặn vào lệnh mới\n"
  "/allow — Cho phép vào lệnh mới\n"
  "/status — Thông tin tài khoản\n"
  "/unlink — Hủy liên kết tài khoản"
)

HELP_TEXT = (
  "🤖 <b>Trading Bot</b>\n\n"
  "Bot giúp bạn theo dõi giao dịch và điều khiển tài khoản của mình.\n\n"
  "Bắt đầu bằng /start và nhập mã <b>UUID</b> quản trị viên đã cấp để liên kết.\n\n"
  + COMMANDS_HINT
)

_STATUS_EMOJI = {
  "OPENED": "🟢",
  "CLOSED": "⚪️",
  "FLAT": "🔵",
  "REJECTED": "🔴",
  "PARTIALLY_CLOSED": "🟡",
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
  linked = "đã liên kết" if account.get("telegram_user_id") else "chưa liên kết"
  return (
    "<b>📂 Tài khoản</b>\n"
    f"• ID: <code>{_esc(account.get('account_id'))}</code>\n"
    f"• Tên: {_esc(account.get('account_name'))}\n"
    f"• Số dư: <b>{_fmt_num(account.get('account_balance'))}</b>\n"
    f"• Thị trường: {_esc(account.get('market_type'))}\n"
    f"• Trạng thái: {linked}"
  )


def _format_trade_line(trade: dict[str, Any]) -> str:
  status = str(trade.get("status", ""))
  emoji = _STATUS_EMOJI.get(status, "•")
  updated = str(trade.get("updatedAt", ""))[:19].replace("T", " ")
  return (
    f"{emoji} <b>{_esc(trade.get('symbol'))}</b> "
    f"{_esc(trade.get('action'))} · {_esc(status)}\n"
    f"   giá {_fmt_num(trade.get('price'))} · KL {_fmt_num(trade.get('quantity'))} "
    f"· số dư {_fmt_num(trade.get('account_balance'))}\n"
    f"   <i>{_esc(updated)}</i>"
  )


def format_trades(payload: dict[str, Any]) -> str:
  data = payload.get("data") or []
  page = payload.get("page") or {}
  total = page.get("total", len(data))

  if not data:
    return "📭 Chưa có giao dịch nào."

  offset = int(page.get("offset", 0))
  start = offset + 1
  end = offset + len(data)

  header = f"<b>📊 Giao dịch</b> ({start}–{end} / {total})"
  lines = [_format_trade_line(t) for t in data]
  return header + "\n\n" + "\n".join(lines)


def format_command_result(result: dict[str, Any]) -> str:
  action = _esc(result.get("action"))
  scope = _esc(result.get("scope"))
  return (
    f"✅ Đã gửi lệnh <b>{action}</b>\n"
    f"Phạm vi: <code>{scope}</code>\n\n"
    "<i>Lệnh đã được phát tới worker qua broker.</i>"
  )


# ── Admin ───────────────────────────────────────────────────────────

# setting key (from broker) → (nhãn hiển thị, slug endpoint toggle).
# Single source shared with keyboards.settings_keyboard.
SETTING_META: dict[str, tuple[str, str]] = {
  "signal_blocked": ("Chặn tín hiệu", "block-signal"),
  "silent_signal": ("Tắt thông báo", "silent-signal"),
  "notification_include_signal_raw": ("Kèm raw trong thông báo", "include-signal-raw"),
}


def format_accounts_admin(accounts: list[dict[str, Any]]) -> str:
  if not accounts:
    return "📭 Chưa có tài khoản nào."
  lines = [f"<b>📂 Tài khoản</b> ({len(accounts)})", ""]
  for a in accounts:
    linked = "✅" if a.get("telegram_user_id") else "—"
    lines.append(
      f"{linked} <b>{_esc(a.get('account_name'))}</b> "
      f"<code>{_esc(a.get('account_id'))}</code>\n"
      f"   số dư {_fmt_num(a.get('account_balance'))} · {_esc(a.get('market_type'))}"
    )
    token = a.get("telegram_link_token")
    if token:
      # Ẩn trong spoiler, bọc code để tap-copy đưa cho enduser.
      lines.append(f"   token: <tg-spoiler><code>{_esc(token)}</code></tg-spoiler>")
  return "\n".join(lines)


def format_admin_trades(account_id: str, payload: dict[str, Any]) -> str:
  return f"<b>Tài khoản</b> <code>{_esc(account_id)}</code>\n\n" + format_trades(
    payload
  )


def format_settings(states: list[dict[str, Any]]) -> str:
  lines = ["<b>⚙️ Cài đặt broker</b>", ""]
  for s in states:
    label = SETTING_META.get(str(s.get("setting")), (str(s.get("setting")), ""))[0]
    on = str(s.get("state")) == "ENABLED"
    lines.append(f"{'🟢' if on else '⚪️'} {_esc(label)}: <b>{_esc(s.get('state'))}</b>")
  lines.append("\n<i>Bấm nút bên dưới để bật/tắt.</i>")
  return "\n".join(lines)


def format_rotate_result(result: dict[str, Any]) -> str:
  return (
    f"🔑 Link token mới cho <code>{_esc(result.get('account_id'))}</code>:\n"
    f"<tg-spoiler><code>{_esc(result.get('telegram_link_token'))}</code></tg-spoiler>\n\n"
    "<i>Token cũ đã bị thu hồi. Gửi token mới này cho enduser.</i>"
  )
