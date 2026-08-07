"""
app/emojis.py — Named emoji constants, resolved via the `emoji` library.

Call sites reference these by name instead of embedding raw glyphs in source.
"""

from __future__ import annotations

from emoji import emojize


def _e(shortcode: str) -> str:
  return emojize(shortcode, language="en")


ROBOT = _e(":robot:")
WAVE = _e(":waving_hand:")
WARNING = _e(":warning:")
CHECK = _e(":check_mark_button:")
CROSS = _e(":cross_mark:")
CANCEL = _e(":multiply:")
KEY = _e(":key:")
LINK = _e(":link:")
GEAR = _e(":gear:")
FOLDER = _e(":open_file_folder:")
CHART = _e(":bar_chart:")
EMPTY_MAILBOX = _e(":open_mailbox_with_lowered_flag:")
ARROW_LEFT = _e(":left_arrow:")
ARROW_RIGHT = _e(":right_arrow:")
STAR = _e(":star:")

GREEN_CIRCLE = _e(":green_circle:")
WHITE_CIRCLE = _e(":white_circle:")
BLUE_CIRCLE = _e(":blue_circle:")
RED_CIRCLE = _e(":red_circle:")
YELLOW_CIRCLE = _e(":yellow_circle:")

# ── Live trade card ─────────────────────────────────────────────────
# Mirrors broker/helpers/emoji_constants.py: the broker posts the card and the
# bot re-renders it on Detail/Summary, so the two must pick the same glyphs or
# the message visibly changes character when a button is tapped.
TRADE_OPENED = _e(":blue_circle:")
TRADE_PARTIALLY_CLOSED = _e(":bullseye:")
TRADE_CLOSED = _e(":chequered_flag:")
TRADE_FLAT = _e(":white_flag:")
TRADE_REJECTED = _e(":prohibited:")
SATELLITE = _e(":satellite_antenna:")

DETAIL = _e(":magnifying_glass_tilted_left:")
COLLAPSE = _e(":up_arrow:")
EXIT = _e(":stop_sign:")
PENDING = _e(":hourglass_not_done:")
