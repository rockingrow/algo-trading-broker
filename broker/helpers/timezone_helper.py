"""
broker/helpers/timezone_helper.py — Normalises signal timestamps to UTC and
renders them in the broker's configured notification display timezone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from broker.settings import settings

_TIME_FMT = "%Y-%m-%d %H:%M:%S"
_MAX_ABS_OFFSET_HOURS = 24  # datetime.timezone requires |offset| < 24h


def to_utc(dt: datetime) -> datetime:
  """Normalise *dt* to an aware UTC datetime.

  TradingView sends alert timestamps without a UTC offset (e.g.
  "2026-04-10 22:55:00"); such naive values already represent UTC wall-clock
  time, so they're stamped with UTC as-is. Aware datetimes are converted.
  """
  if dt.tzinfo is None:
    return dt.replace(tzinfo=timezone.utc)
  return dt.astimezone(timezone.utc)


def parse_offset_hours(raw: str | None) -> float:
  """Parse a `notification_timezone` setting value into an hour offset,
  falling back to the default (UTC+7) when missing, invalid, or outside the
  range `datetime.timezone` can represent."""
  if raw is not None and raw.strip():
    try:
      hours = float(raw)
    except ValueError:
      hours = None
    if hours is not None and abs(hours) < _MAX_ABS_OFFSET_HOURS:
      return hours
  return settings.notification.DEFAULT_TIMEZONE_OFFSET_HOURS


def format_offset_value(hours: float) -> str:
  """Render an hour offset as a compact numeral, e.g. 7.0 -> "7", -5.5 -> "-5.5"."""
  return f"{hours:g}"


def format_utc_label(hours: float) -> str:
  """Render an hour offset as a "UTC+N" / "UTC-N" label."""
  sign = "+" if hours >= 0 else ""
  return f"UTC{sign}{format_offset_value(hours)}"


def format_notification_time(dt: datetime, offset: str | None = None) -> str:
  """Render *dt* for a Telegram notification.

  *dt* is first normalised to UTC, then shifted to the configured display
  timezone (broker setting `notification_timezone`, default UTC+7), and
  formatted as "YYYY-MM-DD HH:MM:SS (UTC+N)".
  """
  hours = parse_offset_hours(offset)
  local = to_utc(dt).astimezone(timezone(timedelta(hours=hours)))
  return f"{local.strftime(_TIME_FMT)} ({format_utc_label(hours)})"
