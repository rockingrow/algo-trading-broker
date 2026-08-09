"""Unit tests for the /admin_magicmap input parser and its presenters."""

from __future__ import annotations

import html

from app.handlers.admin import _parse_magic_map_input
from app.presenters import messages


def test_parse_valid_object():
  parsed, error = _parse_magic_map_input('{"MT5_GOLD_M5_V1": 20260409, "X": 1}')
  assert error is None
  assert parsed == {"MT5_GOLD_M5_V1": 20260409, "X": 1}


def test_parse_rejects_invalid_json():
  parsed, error = _parse_magic_map_input("not json")
  assert parsed is None
  assert "Invalid JSON" in error


def test_parse_rejects_non_object():
  parsed, error = _parse_magic_map_input("[1, 2, 3]")
  assert parsed is None
  assert "JSON object" in error


def test_parse_rejects_empty_object():
  parsed, error = _parse_magic_map_input("{}")
  assert parsed is None
  assert "at least one" in error


def test_parse_rejects_non_integer_value():
  parsed, error = _parse_magic_map_input('{"A": 1.5}')
  assert parsed is None
  assert "must be an integer" in error


def test_parse_rejects_boolean_value():
  # bool is an int subclass, but a magic number is never true/false.
  parsed, error = _parse_magic_map_input('{"A": true}')
  assert parsed is None
  assert "must be an integer" in error


def test_parse_error_html_escapes_key():
  parsed, error = _parse_magic_map_input('{"<b>": 1.5}')
  assert parsed is None
  assert "<b>" not in error
  assert "&lt;b&gt;" in error


def test_format_magic_map_prompt_shows_current_value():
  out = messages.AdminMessages.format_magic_map_prompt('{"A": 1}')
  assert "Strategy magic map" in out
  # The value is rendered inside <code>, HTML-escaped.
  assert html.escape('{"A": 1}') in out


def test_format_magic_map_prompt_escapes_current_value():
  out = messages.AdminMessages.format_magic_map_prompt("<b>x</b>")
  assert "<b>x</b>" not in out
  assert "&lt;b&gt;x&lt;/b&gt;" in out


def test_format_magic_map_updated_shows_value():
  out = messages.AdminMessages.format_magic_map_updated('{"A": 1}')
  assert "updated" in out.lower()
  assert html.escape('{"A": 1}') in out
