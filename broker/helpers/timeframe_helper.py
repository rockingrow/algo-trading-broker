"""
broker/helpers/timeframe_helper.py — Utilities for timeframe conversion and formatting.
"""


def format_timeframe(minutes: int | str) -> str:
  """
  Converts a number of minutes (as int or str) into a formatted timeframe string.

  Format:
  - If divisible by 1440 (minutes in a day), returns 'D<days>'
  - Else if divisible by 60 (minutes in an hour), returns 'H<hours>'
  - Otherwise returns 'M<minutes>'

  Examples:
      5 -> "M5"
      "60" -> "H1"
      120 -> "H2"
      1440 -> "D1"
  """
  try:
    m = int(minutes)
  except (ValueError, TypeError):
    # If it's already a string like "M5" or "H1", return as is
    return str(minutes)

  if m <= 0:
    return f"M{m}"

  if m % 1440 == 0:
    return f"D{m // 1440}"
  elif m % 60 == 0:
    return f"H{m // 60}"
  else:
    return f"M{m}"
