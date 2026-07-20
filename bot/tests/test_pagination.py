"""Unit tests for app/utils/pagination.py and the account list keyboards."""

from __future__ import annotations

from app.constants import ACCOUNTS_PER_PAGE
from app.keyboards import inline
from app.utils.pagination import build_pagination_keyboard, build_pagination_row, paginate


def _callbacks(markup) -> list[str]:
  return [b.callback_data for row in markup.inline_keyboard for b in row]


# ── paginate ────────────────────────────────────────────────────────


def test_paginate_slices_and_describes_the_page():
  rows, page = paginate(list(range(23)), limit=8, offset=8)
  assert rows == list(range(8, 16))
  assert page == {"total": 23, "limit": 8, "offset": 8}


def test_paginate_last_page_is_short():
  rows, page = paginate(list(range(23)), limit=8, offset=16)
  assert rows == list(range(16, 23))
  assert page["total"] == 23


def test_paginate_clamps_an_out_of_range_offset_to_the_first_page():
  # A Next button on a message left open while the list shrank would otherwise
  # land the user on an empty table with no way back.
  rows, page = paginate(list(range(5)), limit=8, offset=40)
  assert rows == list(range(5))
  assert page["offset"] == 0


def test_paginate_empty_list():
  rows, page = paginate([], limit=8, offset=0)
  assert rows == []
  assert page == {"total": 0, "limit": 8, "offset": 0}


# ── Prev/Next buttons ───────────────────────────────────────────────


def test_no_nav_row_when_one_page_covers_everything():
  assert build_pagination_row({"total": 3, "limit": 8, "offset": 0}, lambda o: f"x:{o}") == []
  assert build_pagination_keyboard({"total": 3, "limit": 8, "offset": 0}, lambda o: f"x:{o}") is None


def test_first_page_offers_next_only():
  row = build_pagination_row({"total": 23, "limit": 8, "offset": 0}, lambda o: f"x:{o}")
  assert [b.callback_data for b in row] == ["x:8"]


def test_middle_page_offers_both_directions():
  row = build_pagination_row({"total": 23, "limit": 8, "offset": 8}, lambda o: f"x:{o}")
  assert [b.callback_data for b in row] == ["x:0", "x:16"]


def test_last_page_offers_prev_only():
  row = build_pagination_row({"total": 23, "limit": 8, "offset": 16}, lambda o: f"x:{o}")
  assert [b.callback_data for b in row] == ["x:8"]


# ── list keyboards ──────────────────────────────────────────────────


def test_every_table_listing_has_a_pagination_keyboard():
  # The point of the constants: no table command renders a bare list.
  page = {"total": 100, "limit": 5, "offset": 0}
  assert inline.trades_pagination(page) is not None
  assert inline.accounts_pagination(page) is not None
  assert inline.admin_accounts_pagination(page) is not None
  assert inline.admin_trades_pagination("acc-1", page) is not None


def test_switch_picker_carries_its_nav_below_the_account_buttons():
  accounts = [
    {"id": f"uuid-{i}", "account_id": f"acc-{i}", "market": "FOREX", "gateway": "MT5"}
    for i in range(ACCOUNTS_PER_PAGE)
  ]
  markup = inline.linked_accounts_picker(
    accounts, {"total": 23, "limit": ACCOUNTS_PER_PAGE, "offset": 0}
  )
  # One row per account, then the nav row last.
  assert len(markup.inline_keyboard) == ACCOUNTS_PER_PAGE + 1
  assert _callbacks(markup)[-1] == f"swpg:{ACCOUNTS_PER_PAGE}"
  # Selecting still addresses the account by row UUID, not by page index.
  assert _callbacks(markup)[0] == "swacc:uuid-0"


def test_switch_picker_omits_the_nav_row_on_a_single_page():
  accounts = [{"id": "uuid-1", "account_id": "acc-1", "market": "FOREX", "gateway": "MT5"}]
  markup = inline.linked_accounts_picker(accounts, {"total": 1, "limit": 8, "offset": 0})
  assert len(markup.inline_keyboard) == 1
  assert _callbacks(markup) == ["swacc:uuid-1"]
