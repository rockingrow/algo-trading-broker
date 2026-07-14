from datetime import datetime, timedelta, timezone

from broker.helpers.timezone_helper import (
  format_notification_time,
  format_offset_value,
  format_utc_label,
  parse_offset_hours,
  to_utc,
)
from broker.settings import settings

DEFAULT_NOTIFICATION_TIMEZONE_OFFSET_HOURS = (
  settings.DEFAULT_NOTIFICATION_TIMEZONE_OFFSET_HOURS
)


# ── to_utc ────────────────────────────────────────────────────────────


def test_to_utc_stamps_naive_datetime_as_utc():
  dt = to_utc(datetime(2026, 1, 1, 12, 30, 0))
  assert dt == datetime(2026, 1, 1, 12, 30, 0, tzinfo=timezone.utc)


def test_to_utc_converts_aware_datetime_to_utc():
  jst = timezone(timedelta(hours=9))
  dt = to_utc(datetime(2026, 1, 1, 21, 30, 0, tzinfo=jst))
  assert dt == datetime(2026, 1, 1, 12, 30, 0, tzinfo=timezone.utc)


# ── parse_offset_hours ────────────────────────────────────────────────


def test_parse_offset_hours_defaults_when_none():
  assert parse_offset_hours(None) == DEFAULT_NOTIFICATION_TIMEZONE_OFFSET_HOURS


def test_parse_offset_hours_defaults_when_blank():
  assert parse_offset_hours("  ") == DEFAULT_NOTIFICATION_TIMEZONE_OFFSET_HOURS


def test_parse_offset_hours_defaults_when_invalid():
  assert (
    parse_offset_hours("not-a-number") == DEFAULT_NOTIFICATION_TIMEZONE_OFFSET_HOURS
  )


def test_parse_offset_hours_defaults_when_out_of_range():
  assert parse_offset_hours("24") == DEFAULT_NOTIFICATION_TIMEZONE_OFFSET_HOURS
  assert parse_offset_hours("-100") == DEFAULT_NOTIFICATION_TIMEZONE_OFFSET_HOURS


def test_parse_offset_hours_parses_valid_values():
  assert parse_offset_hours("7") == 7.0
  assert parse_offset_hours("-5.5") == -5.5
  assert parse_offset_hours("0") == 0.0


# ── format_offset_value / format_utc_label ─────────────────────────────


def test_format_offset_value_drops_trailing_zero():
  assert format_offset_value(7.0) == "7"
  assert format_offset_value(-5.5) == "-5.5"
  assert format_offset_value(0.0) == "0"


def test_format_utc_label_signs_positive_and_negative():
  assert format_utc_label(7.0) == "UTC+7"
  assert format_utc_label(-5.5) == "UTC-5.5"
  assert format_utc_label(0.0) == "UTC+0"


# ── format_notification_time ───────────────────────────────────────────


def test_format_notification_time_defaults_to_utc_plus_7():
  dt = datetime(2026, 1, 1, 12, 30, 0, tzinfo=timezone.utc)
  assert format_notification_time(dt) == "2026-01-01 19:30:00 (UTC+7)"


def test_format_notification_time_honors_explicit_offset():
  dt = datetime(2026, 1, 1, 12, 30, 0, tzinfo=timezone.utc)
  assert format_notification_time(dt, "-5") == "2026-01-01 07:30:00 (UTC-5)"


def test_format_notification_time_normalises_naive_input_as_utc():
  dt = datetime(2026, 1, 1, 12, 30, 0)
  assert format_notification_time(dt) == "2026-01-01 19:30:00 (UTC+7)"


def test_format_notification_time_normalises_other_timezones_first():
  jst = timezone(timedelta(hours=9))
  dt = datetime(2026, 1, 1, 21, 30, 0, tzinfo=jst)  # same instant as 12:30 UTC
  assert format_notification_time(dt) == "2026-01-01 19:30:00 (UTC+7)"
