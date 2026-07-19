"""
app/utils/timezone.py — Render UTC timestamps from the broker API in the
broker's configured notification display timezone (``notification_timezone``
broker setting, a UTC offset in hours).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

FULL_TIME_FMT = "%Y-%m-%d %H:%M:%S"
# For table rows, where the year is noise and the zone is stated once in the
# header rather than repeated on every line.
SHORT_TIME_FMT = "%m-%d %H:%M"

# Mirrors broker.settings.Settings.DEFAULT_NOTIFICATION_TIMEZONE_OFFSET_HOURS —
# used only when the broker call for the live value fails.
DEFAULT_OFFSET_HOURS = 7.0


def offset_hours_from_payload(payload: Optional[dict[str, Any]]) -> float:
  """Pull the UTC offset (hours) out of a ``get_notification_timezone()``
  response, falling back to the default when the call failed or the value
  is malformed."""
  if payload is not None:
    try:
      return float(payload.get("value"))
    except (TypeError, ValueError):
      pass
  return DEFAULT_OFFSET_HOURS


def format_utc_label(hours: float) -> str:
  """Render an hour offset as a "UTC+N" / "UTC-N" label."""
  sign = "+" if hours >= 0 else ""
  return f"UTC{sign}{hours:g}"


def format_local_time(
  value: Any,
  offset_hours: float,
  fmt: str = FULL_TIME_FMT,
  with_label: bool = True,
) -> str:
  """Render a broker timestamp (ISO-8601 string or datetime) in the given
  UTC offset — by default "YYYY-MM-DD HH:MM:SS (UTC+N)".

  Pass ``with_label=False`` when the zone is already stated elsewhere (a table
  header, say) so it isn't repeated on every row.
  """
  suffix = f" ({format_utc_label(offset_hours)})" if with_label else ""

  if isinstance(value, datetime):
    dt = value
  else:
    try:
      dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
      # Unparseable: show what the broker sent rather than dropping the value.
      return f"{value}{suffix}"

  dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
  local = dt.astimezone(timezone(timedelta(hours=offset_hours)))
  return f"{local.strftime(fmt)}{suffix}"
