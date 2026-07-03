"""
broker/helpers/emoji_constants.py — Centralized emoji constants via the `emoji` library.

All Telegram notification emoji are defined here so they are named, testable,
and easy to swap out without hunting through multiple files.
"""

import emoji

# ── Trading signal actions ────────────────────────────────────────────────────
LONG: str = emoji.emojize(":green_circle:")  # 🟢
SHORT: str = emoji.emojize(":red_circle:")  # 🔴
TP1: str = emoji.emojize(":bullseye:")  # 🎯
TP2: str = emoji.emojize(":rocket:")  # 🚀
R_SL: str = emoji.emojize(":shield:")  # 🛡️
SL: str = emoji.emojize(":cross_mark:")  # ❌
FLAT: str = emoji.emojize(":white_flag:")  # 🏳️
DEFAULT_SIGNAL: str = emoji.emojize(":satellite_antenna:")  # 📡

# ── Broker lifecycle ──────────────────────────────────────────────────────────
BROKER_STARTED: str = emoji.emojize(":green_circle:")  # 🟢
BROKER_STOPPED: str = emoji.emojize(":stop_sign:")  # 🛑
ENDPOINT: str = emoji.emojize(":globe_with_meridians:")  # 🌐
PLUG: str = emoji.emojize(":electric_plug:")  # 🔌

# ── NATS connection ───────────────────────────────────────────────────────────
NATS_DISCONNECTED: str = emoji.emojize(":red_circle:")  # 🔴
NATS_RECONNECTED: str = emoji.emojize(":electric_plug:")  # 🔌
PUBLISH: str = emoji.emojize(":outbox_tray:")  # 📤
LISTEN: str = emoji.emojize(":inbox_tray:")  # 📥

# ── Error / log alerts ────────────────────────────────────────────────────────
ERROR_ALERT: str = emoji.emojize(":police_car_light:")  # 🚨

# ── Admin / settings ─────────────────────────────────────────────────────────
GEAR: str = emoji.emojize(":gear:")  # ⚙️
ADMIN_FLAT: str = emoji.emojize(":shield:")  # 🛡️
BLOCKED: str = emoji.emojize(":prohibited:")  # 🚫

# ── Position flags ────────────────────────────────────────────────────────────
FLAG_ON: str = emoji.emojize(":green_circle:")  # 🟢
FLAG_OFF: str = emoji.emojize(":red_circle:")  # 🔴
