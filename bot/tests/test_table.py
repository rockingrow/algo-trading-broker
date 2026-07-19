"""Unit tests for app/utils/table.py."""

from __future__ import annotations

from app.utils.table import ACTIVE_MARK, render_table


def _body(out: str) -> list[str]:
  """Strip the <pre> wrapper and split into lines."""
  assert out.startswith("<pre>") and out.endswith("</pre>")
  return out[len("<pre>") : -len("</pre>")].split("\n")


def test_columns_are_padded_to_a_common_width():
  lines = _body(render_table(("A", "B"), [("x", "1"), ("longer", "2")]))
  assert lines[0] == "A       B"
  assert lines[2] == "x       1"
  assert lines[3] == "longer  2"


def test_header_rule_spans_the_full_table_width():
  lines = _body(render_table(("A", "B"), [("longer", "2")]))
  assert set(lines[1]) == {"─"}
  assert len(lines[1]) == len("longer") + 2 + len("B")


def test_column_width_accounts_for_the_header():
  lines = _body(render_table(("MARKET", "X"), [("a", "b")]))
  assert lines[2] == "a       b"


def test_right_alignment():
  lines = _body(render_table(("N", "V"), [("a", "1"), ("b", "1000")], aligns=("l", "r")))
  assert lines[2] == "a     1"
  assert lines[3] == "b  1000"


def test_max_width_truncates_with_ellipsis():
  lines = _body(render_table(("A",), [("abcdefghij",)], max_widths=(5,)))
  assert lines[2] == "abcd…"


def test_max_width_leaves_short_values_alone():
  lines = _body(render_table(("A",), [("ab",)], max_widths=(20,)))
  assert lines[2] == "ab"


def test_none_and_missing_cells_render_empty():
  lines = _body(render_table(("A", "B"), [(None, "x"), ("y",)]))
  assert lines[2] == "   x"
  assert lines[3] == "y"


def test_trailing_whitespace_is_stripped():
  lines = _body(render_table(("A", "B"), [("x", ""), ("y", "zz")]))
  assert lines[2] == "x"


def test_cells_are_html_escaped():
  out = render_table(("A",), [("<b>&</b>",)])
  assert "<b>" not in _body(out)[2]
  assert "&lt;b&gt;&amp;&lt;/b&gt;" in out


def test_no_rows_renders_header_and_rule_only():
  lines = _body(render_table(("A", "B"), []))
  assert len(lines) == 2
  assert lines[0] == "A  B"


def test_active_mark_is_single_width_text_not_emoji():
  # An emoji marker would be double-width and skew every column after it.
  assert len(ACTIVE_MARK) == 1
