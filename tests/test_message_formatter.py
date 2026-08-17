from datetime import datetime, timezone
from types import SimpleNamespace

from broker.helpers import emoji_constants as em
from broker.helpers.message_formatter import (
  format_blocked_message,
  format_broadcast_message,
  worker_label,
)
from broker.schemas.account_schema import MarketTypeEnum
from broker.schemas.trade_schema import TradeStatusEnum
from broker.schemas.core import BroadcastStatusEnum, SignalActionEnum
from broker.schemas.webhook_schema import PositionSchema, WebhookPayload


def _payload(**overrides) -> WebhookPayload:
  base = dict(
    strategy="strat",
    symbol="OANDA:XAUUSD",
    timeframe="60",
    timestamp=datetime(2026, 1, 1, 12, 30, 0, tzinfo=timezone.utc),
    position=PositionSchema(
      action=SignalActionEnum.LONG,
      price=100.0,
      quantity=1.0,
      sl=95.0,
      tp1=110.0,
      tp2=120.0,
    ),
    token="secret",
  )
  base.update(overrides)
  return WebhookPayload(**base)


def _event(action="LONG", **overrides) -> dict:
  base = {
    "action": action,
    "price": 100.0,
    "quantity": 1.0,
    "sl": 95.0,
    "tp1": 110.0,
    "tp2": 120.0,
    "risk_percent": 2.0,
    "timestamp": "2026-01-01T12:30:00+00:00",
    "attempt": None,
  }
  base.update(overrides)
  return base


def _record(events=None, status=BroadcastStatusEnum.RUNNING, **overrides):
  """Stand-in for a ``broadcast_messages`` row (the formatter is duck-typed)."""
  base = dict(
    symbol="OANDA:XAUUSD",
    timeframe="60",
    strategy="strat",
    signal_uxid="9f2c4b7e18a3d605",
    status=status,
    events=events if events is not None else [_event()],
  )
  base.update(overrides)
  return SimpleNamespace(**base)


# ── Header ─────────────────────────────────────────────────────────


def test_header_carries_entry_emoji_symbol_and_timeframe():
  msg = format_broadcast_message(_record())
  assert em.LONG in msg
  assert "<b>OANDA:XAUUSD</b>" in msg
  assert "(H1)" in msg  # timeframe "60" -> H1


def test_header_shows_strategy_and_cycle_id():
  msg = format_broadcast_message(_record(), include_meta=True)
  assert "Strategy: <b>strat</b>" in msg
  assert "Signal: <code>9f2c4b7e18a3d605</code>" in msg


def test_header_omits_strategy_and_cycle_id_without_include_meta():
  """The public audience gets the bare body — no strategy internals."""
  msg = format_broadcast_message(_record())
  assert "Strategy:" not in msg
  assert "Signal:" not in msg


def test_running_cycle_shows_the_running_icon():
  msg = format_broadcast_message(_record())
  assert em.CYCLE_RUNNING in msg
  assert "<b>RUNNING</b>" in msg


def test_closed_cycle_shows_the_closed_icon():
  msg = format_broadcast_message(
    _record(events=[_event(), _event("SL")], status=BroadcastStatusEnum.CLOSED)
  )
  assert em.CYCLE_CLOSED in msg
  assert "<b>CLOSED</b>" in msg


def test_header_keeps_the_entry_emoji_after_the_cycle_closes():
  """The icon identifies the trade direction, so a SL must not turn a long
  cycle's header red."""
  msg = format_broadcast_message(
    _record(events=[_event("SHORT"), _event("TP2")], status=BroadcastStatusEnum.CLOSED)
  )
  assert msg.startswith(em.SHORT)


def test_missing_timeframe_is_omitted():
  msg = format_broadcast_message(_record(timeframe=None))
  assert "()" not in msg


# ── Timeline ───────────────────────────────────────────────────────


def test_every_event_appears_in_order():
  msg = format_broadcast_message(
    _record(events=[_event(), _event("TP1"), _event("SL")])
  )
  assert msg.index("<b>LONG</b>") < msg.index("<b>TP1</b>") < msg.index("<b>SL</b>")


def test_entry_event_renders_price_size_risk_and_levels():
  msg = format_broadcast_message(_record())
  assert "@ <code>100</code>" in msg
  assert "× <code>1</code>" in msg
  assert "Risk: <code>2%</code>" in msg
  assert "SL: <code>95</code> | TP1: <code>110</code> | TP2: <code>120</code>" in msg


def test_follow_up_event_does_not_repeat_the_levels():
  msg = format_broadcast_message(_record(events=[_event("TP1", price=110.0)]))
  assert "TP2: <code>120</code>" not in msg
  assert "@ <code>110</code>" in msg


def test_position_flags_render_on_the_entry():
  msg = format_broadcast_message(
    _record(
      events=[
        _event(
          tp1_percent=50.0,
          move_sl_to_be=False,
          is_running=True,
          is_scale_position=True,
          scale_strategy="pullback",
        )
      ]
    )
  )
  assert "TP1%: 50%" in msg
  assert f"SL→BE: {em.FLAG_OFF}" in msg
  assert f"Running: {em.FLAG_ON}" in msg
  assert "pullback" in msg


def test_flags_absent_when_the_strategy_sent_none():
  msg = format_broadcast_message(_record())
  assert "Running:" not in msg
  assert "SL→BE:" not in msg


def test_unknown_action_falls_back_to_the_default_emoji():
  msg = format_broadcast_message(_record(events=[_event("SOMETHING_NEW")]))
  assert em.DEFAULT_SIGNAL in msg
  assert "<b>SOMETHING_NEW</b>" in msg


def test_empty_cycle_still_renders_a_header():
  msg = format_broadcast_message(_record(events=[]))
  assert "<b>OANDA:XAUUSD</b>" in msg


# ── Time formatting ────────────────────────────────────────────────


def test_time_defaults_to_utc_plus_7():
  msg = format_broadcast_message(_record())
  assert "2026-01-01 19:30:00 (UTC+7)" in msg


def test_time_honors_custom_timezone_offset():
  msg = format_broadcast_message(_record(), timezone_offset="-5")
  assert "2026-01-01 07:30:00 (UTC-5)" in msg


def test_naive_timestamp_is_read_as_utc():
  msg = format_broadcast_message(
    _record(events=[_event(timestamp="2026-01-01T12:30:00")])
  )
  assert "2026-01-01 19:30:00 (UTC+7)" in msg


def test_unparseable_timestamp_is_shown_as_is():
  msg = format_broadcast_message(_record(events=[_event(timestamp="whenever")]))
  assert "whenever" in msg


# ── Retry marker ───────────────────────────────────────────────────


def test_no_retry_marker_on_a_first_attempt():
  msg = format_broadcast_message(_record())
  assert em.CYCLE_RETRY not in msg


def test_retry_marker_shows_the_attempt_number():
  msg = format_broadcast_message(_record(events=[_event(attempt=3)]))
  assert f"{em.CYCLE_RETRY} attempt 3" in msg


# ── Raw section (indicators / inputs) ──────────────────────────────


def test_raw_section_excluded_by_default():
  msg = format_broadcast_message(
    _record(events=[_event(indicators={"wt1": 1.23}, inputs={"bb_len": 20})])
  )
  assert "Indicators:" not in msg
  assert "Inputs:" not in msg


def test_raw_section_included_when_flag_on():
  msg = format_broadcast_message(
    _record(events=[_event(indicators={"wt1": 1.23}, inputs={"bb_len": 20})]),
    include_raw=True,
    include_meta=True,
  )
  assert "wt1: 1.23" in msg
  assert "bb_len: 20" in msg


def test_raw_section_needs_include_meta_too():
  """The raw dump is operator-facing; the public body never carries it,
  ``include_raw`` alone is not enough."""
  msg = format_broadcast_message(
    _record(events=[_event(indicators={"wt1": 1.23}, inputs={"bb_len": 20})]),
    include_raw=True,
  )
  assert "wt1: 1.23" not in msg


def test_raw_section_skips_none_values():
  msg = format_broadcast_message(
    _record(events=[_event(indicators={"wt1": 1.0, "wt2": None})]),
    include_raw=True,
    include_meta=True,
  )
  assert "wt1: 1.0" in msg
  assert "wt2" not in msg


def test_raw_section_uses_only_the_latest_event():
  """A cycle would otherwise repeat a full indicator dump per action."""
  msg = format_broadcast_message(
    _record(
      events=[
        _event(indicators={"wt1": 1.0}),
        _event("TP1", indicators={"wt1": 99.0}),
      ]
    ),
    include_raw=True,
    include_meta=True,
  )
  assert "wt1: 99.0" in msg
  assert "wt1: 1.0" not in msg


# ── Execution table (public audience) ──────────────────────────────


def _worker(account_id="12345678", gateway="MT5", status=TradeStatusEnum.OPENED):
  return SimpleNamespace(
    account_id=account_id,
    gateway=gateway,
    market=MarketTypeEnum.FOREX,
    latest_status=status,
    worker_id=f"FOREX-{gateway}-{account_id}",
  )


def test_no_execution_table_without_workers():
  assert "Executions" not in format_broadcast_message(_record())


def test_no_execution_table_without_include_meta():
  """Passing workers alone is not enough — the public body never shows them."""
  msg = format_broadcast_message(
    _record(), workers=[_worker(), _worker("87654321", status=TradeStatusEnum.CLOSED)]
  )
  assert "Executions" not in msg


def test_execution_table_lists_worker_and_status():
  msg = format_broadcast_message(
    _record(),
    workers=[_worker(), _worker("87654321", status=TradeStatusEnum.CLOSED)],
    include_meta=True,
  )
  assert "Executions (2)" in msg
  assert "MT5 ****5678" in msg
  assert "MT5 ****4321" in msg
  assert "OPENED" in msg
  assert "CLOSED" in msg
  # No inner <pre> here — the whole broadcast body is boxed once at send
  # time (BroadcastNotifier), and Telegram disallows nesting <pre> tags.
  assert "<pre>" not in msg
  assert "Worker".ljust(len("MT5 ****5678")) + "  Status" in msg


def test_worker_label_masks_the_account_id():
  """A public channel must not carry anyone's full account number."""
  label = worker_label(_worker(account_id="12345678"))
  assert "12345678" not in label
  assert label == "MT5 ****5678"


def test_worker_label_falls_back_to_the_market_without_a_gateway():
  assert worker_label(_worker(gateway=None)) == "FOREX ****5678"


def test_worker_label_keeps_a_short_account_id_as_is():
  assert worker_label(_worker(account_id="42")) == "MT5 42"


# ── Blocked message ────────────────────────────────────────────────


def test_blocked_message_contents():
  msg = format_blocked_message(_payload())
  assert em.BLOCKED in msg
  assert "OANDA:XAUUSD" in msg
  assert "signal_blocked" in msg
