from datetime import datetime, timezone

from broker.helpers import emoji_constants as em
from broker.helpers.message_formatter import (
  format_blocked_message,
  format_flat_message,
  format_signal_message,
)
from broker.schemas.core import SignalActionEnum
from broker.schemas.webhook_schema import (
  IndicatorsSchema,
  InputsSchema,
  PositionSchema,
  WebhookPayload,
)


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


# ── Header / shared ────────────────────────────────────────────────


def test_header_includes_emoji_symbol_and_formatted_timeframe():
  msg = format_signal_message(_payload())
  assert em.LONG in msg
  assert "<b>OANDA:XAUUSD</b>" in msg
  assert "(H1)" in msg  # timeframe "60" -> H1


# ── FLAT message ───────────────────────────────────────────────────


def test_flat_message_contents():
  msg = format_flat_message(
    _payload(position=PositionSchema(action=SignalActionEnum.FLAT))
  )
  assert "Action: <b>FLAT</b>" in msg
  assert "Strategy: <b>strat</b>" in msg
  assert "Time: 2026-01-01 19:30:00 (UTC+7)" in msg


# ── Time formatting ─────────────────────────────────────────────────


def test_time_defaults_to_utc_plus_7():
  msg = format_signal_message(_payload())
  assert "Time: 2026-01-01 19:30:00 (UTC+7)" in msg


def test_time_honors_custom_timezone_offset():
  msg = format_signal_message(_payload(), timezone_offset="-5")
  assert "Time: 2026-01-01 07:30:00 (UTC-5)" in msg


def test_time_normalises_naive_timestamp_as_utc():
  payload = _payload(timestamp=datetime(2026, 1, 1, 12, 30, 0))
  msg = format_signal_message(payload)
  assert "Time: 2026-01-01 19:30:00 (UTC+7)" in msg


def test_flat_message_time_honors_custom_timezone_offset():
  msg = format_flat_message(
    _payload(position=PositionSchema(action=SignalActionEnum.FLAT)),
    timezone_offset="0",
  )
  assert "Time: 2026-01-01 12:30:00 (UTC+0)" in msg


# ── Signal message ─────────────────────────────────────────────────


def test_signal_message_core_fields():
  msg = format_signal_message(_payload())
  assert "Action: <b>LONG</b>" in msg
  assert "Price: <code>100.0</code>" in msg
  assert "Quantity: <code>1.0</code>" in msg
  assert "SL: <code>95.0</code>" in msg
  assert "TP1: <code>110.0</code>" in msg
  assert "TP2: <code>120.0</code>" in msg


def test_risk_percent_prefers_position_level():
  payload = _payload(
    position=PositionSchema(
      action=SignalActionEnum.LONG, price=1, quantity=1, risk_percent=2.5
    ),
    inputs=InputsSchema(risk_percent=9.9),
  )
  msg = format_signal_message(payload)
  assert "Risk: <code>2.5%</code>" in msg


def test_risk_percent_falls_back_to_inputs():
  payload = _payload(
    position=PositionSchema(action=SignalActionEnum.LONG, price=1, quantity=1),
    inputs=InputsSchema(risk_percent=3.3),
  )
  msg = format_signal_message(payload)
  assert "Risk: <code>3.3%</code>" in msg


def test_risk_percent_absent_omits_section():
  msg = format_signal_message(_payload())
  assert "Risk:" not in msg


# ── Position flag section ──────────────────────────────────────────


def test_no_flag_section_when_all_flags_none():
  msg = format_signal_message(_payload())
  assert "-----------" not in msg


def test_flag_section_rendered_for_set_flags():
  payload = _payload(
    position=PositionSchema(
      action=SignalActionEnum.LONG,
      price=1,
      quantity=1,
      tp1_percent=50.0,
      move_sl_to_be=True,
      is_running=True,
      is_scale_position=True,
      scale_strategy="pullback",
    )
  )
  msg = format_signal_message(payload)
  assert "-----------" in msg
  assert "TP1%:" in msg and "50.0%" in msg
  assert "Move SL to BE:" in msg
  assert "Is Running:" in msg
  assert "Scale Position:" in msg
  assert "pullback" in msg
  # move_sl_to_be False renders FLAG_OFF
  assert em.FLAG_ON in msg


def test_move_sl_to_be_false_renders_off_flag():
  payload = _payload(
    position=PositionSchema(
      action=SignalActionEnum.LONG, price=1, quantity=1, move_sl_to_be=False
    )
  )
  msg = format_signal_message(payload)
  assert "Move SL to BE:" in msg
  assert em.FLAG_OFF in msg


# ── Raw section (indicators / inputs) ──────────────────────────────


def test_raw_section_excluded_by_default():
  payload = _payload(
    indicators=IndicatorsSchema(wt1=1.0),
    inputs=InputsSchema(bb_len=20),
  )
  msg = format_signal_message(payload)
  assert "Indicators:" not in msg
  assert "Inputs:" not in msg


def test_raw_section_included_when_flag_on():
  payload = _payload(
    indicators=IndicatorsSchema(wt1=1.23),
    inputs=InputsSchema(bb_len=20),
  )
  msg = format_signal_message(payload, include_raw=True)
  assert "Indicators:" in msg
  assert "wt1: 1.23" in msg
  assert "Inputs:" in msg
  assert "bb_len: 20" in msg


def test_raw_section_skips_none_values():
  payload = _payload(indicators=IndicatorsSchema(wt1=1.0, wt2=None))
  msg = format_signal_message(payload, include_raw=True)
  assert "wt1: 1.0" in msg
  assert "wt2" not in msg


def test_raw_section_empty_when_all_none():
  payload = _payload(indicators=IndicatorsSchema(), inputs=InputsSchema())
  msg = format_signal_message(payload, include_raw=True)
  assert "Indicators:" not in msg
  assert "Inputs:" not in msg


# ── Blocked message ────────────────────────────────────────────────


def test_blocked_message_contents():
  msg = format_blocked_message(_payload())
  assert em.BLOCKED in msg
  assert "OANDA:XAUUSD" in msg
  assert "signal_blocked" in msg
