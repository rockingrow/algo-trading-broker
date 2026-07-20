"""
app/utils/table.py — Fixed-width text tables for Telegram messages.

Telegram has no table markup, so a table is a monospace ``<pre>`` block whose
columns are space-padded to a common width. Two constraints shape this module:

- **Every character must be single-width**, or the columns skew. Emoji are
  double-width and vary by platform, so row markers use text-presentation
  glyphs (``ACTIVE_MARK`` is U+2605 ★, not ``:star:``). Keep emoji outside the
  table, in the surrounding message.
- **Cells are escaped, never markup.** Callers pass raw values; the renderer
  pads first (padding is computed on the visible text) and escapes last.

Long values are truncated to ``max_widths`` with an ellipsis rather than
wrapped, so every row stays one line — Telegram scrolls a ``<pre>`` block
horizontally, which reads better than a ragged wrap.
"""

from __future__ import annotations

import html
from typing import Any, Iterable, Optional, Sequence

# Text-presentation star (U+2605), single-width in a monospace font — unlike
# emojis.STAR, which would push everything after it out of alignment.
ACTIVE_MARK = "★"

_ELLIPSIS = "…"
_COL_SEP = "  "
_RULE_CHAR = "─"


def _truncate(text: str, width: Optional[int]) -> str:
  if width is None or width <= 0 or len(text) <= width:
    return text
  if width == 1:
    return _ELLIPSIS
  return text[: width - 1] + _ELLIPSIS


def render_table(
  headers: Sequence[str],
  rows: Iterable[Sequence[Any]],
  aligns: Optional[Sequence[str]] = None,
  max_widths: Optional[Sequence[Optional[int]]] = None,
) -> str:
  """Render *rows* as a monospace table wrapped in ``<pre>``.

  ``aligns`` is one of ``"l"``/``"r"`` per column (default all left); use
  ``"r"`` for numbers. ``max_widths`` caps a column's width, truncating longer
  values with an ellipsis (``None`` for a column = uncapped).

  Each column is sized to its widest cell, header included, so a table of
  short values stays compact instead of padding out to the caps.
  """
  n = len(headers)
  caps = list(max_widths) if max_widths else [None] * n

  def _row_cells(row: Sequence[Any]) -> list[str]:
    # Pad short rows so a caller may omit trailing cells.
    values = list(row[:n]) + [None] * (n - len(row[:n]))
    return [_truncate("" if v is None else str(v), caps[i]) for i, v in enumerate(values)]

  head = _row_cells(headers)
  cells = [_row_cells(row) for row in rows]

  widths = [max(len(r[i]) for r in [head, *cells]) for i in range(n)]
  right = [(list(aligns) + ["l"] * n)[i] == "r" if aligns else False for i in range(n)]

  def _line(values: Sequence[str]) -> str:
    parts = [
      values[i].rjust(widths[i]) if right[i] else values[i].ljust(widths[i])
      for i in range(n)
    ]
    # rstrip so a short trailing cell leaves no dead whitespace on the line.
    return _COL_SEP.join(parts).rstrip()

  rule = _RULE_CHAR * (sum(widths) + len(_COL_SEP) * (n - 1))
  body = "\n".join([_line(head), rule, *(_line(r) for r in cells)])
  return f"<pre>{html.escape(body, quote=False)}</pre>"
