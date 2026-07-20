"""Unit tests for app/utils/timezone.py."""

from __future__ import annotations

from app.utils.timezone import (
  DEFAULT_OFFSET_HOURS,
  SHORT_TIME_FMT,
  format_local_time,
  format_utc_label,
  offset_hours_from_payload,
)


def test_offset_hours_from_payload_reads_value():
  assert offset_hours_from_payload({"setting": "notification_timezone", "value": "9"}) == 9.0


def test_offset_hours_from_payload_falls_back_when_none():
  assert offset_hours_from_payload(None) == DEFAULT_OFFSET_HOURS


def test_offset_hours_from_payload_falls_back_when_malformed():
  assert offset_hours_from_payload({"value": "not-a-number"}) == DEFAULT_OFFSET_HOURS


def test_format_utc_label_positive_and_negative():
  assert format_utc_label(7.0) == "UTC+7"
  assert format_utc_label(-5.5) == "UTC-5.5"
  assert format_utc_label(0.0) == "UTC+0"


def test_format_local_time_converts_and_labels():
  out = format_local_time("2026-01-01T00:00:00Z", 7.0)
  assert out == "2026-01-01 07:00:00 (UTC+7)"


def test_format_local_time_handles_naive_string_as_utc():
  out = format_local_time("2026-01-01T00:00:00", 7.0)
  assert out == "2026-01-01 07:00:00 (UTC+7)"


def test_format_local_time_negative_offset_crosses_day_boundary():
  out = format_local_time("2026-01-01T00:00:00Z", -5.0)
  assert out == "2025-12-31 19:00:00 (UTC-5)"


def test_format_local_time_without_label_omits_the_zone_suffix():
  out = format_local_time("2026-01-01T00:00:00Z", 7.0, with_label=False)
  assert out == "2026-01-01 07:00:00"


def test_format_local_time_short_format_for_table_rows():
  out = format_local_time("2026-01-01T00:00:00Z", 7.0, fmt=SHORT_TIME_FMT, with_label=False)
  assert out == "01-01 07:00"


def test_format_local_time_unparseable_value_is_passed_through():
  assert format_local_time("garbage", 7.0) == "garbage (UTC+7)"
  assert format_local_time("garbage", 7.0, with_label=False) == "garbage"
