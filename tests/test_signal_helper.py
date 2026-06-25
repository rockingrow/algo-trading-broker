from datetime import datetime, timezone

from broker.helpers.signal_helper import action_to_emoji, parse_signal
from broker.schemas.core import SignalActionEnum
from broker.schemas.webhook_schema import PositionSchema, ScalingSchema, WebhookPayload


def _payload(**overrides) -> WebhookPayload:
  base = dict(
    strategy="strat",
    symbol="OANDA:XAUUSD",
    timeframe="60",
    timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    position=PositionSchema(action=SignalActionEnum.LONG, price=100.0, quantity=1.0),
    token="secret",
  )
  base.update(overrides)
  return WebhookPayload(**base)


def test_action_to_emoji_known_and_default():
  assert action_to_emoji(SignalActionEnum.LONG) == "🟢"
  assert action_to_emoji(SignalActionEnum.SL) == "❌"


def test_parse_signal_normalises_symbol():
  signal = parse_signal(_payload(), "sig-1")
  assert signal.symbol == "XAUUSD"
  assert signal.signal_id == "sig-1"
  assert signal.action == SignalActionEnum.LONG


def test_parse_signal_defaults_missing_numbers():
  payload = _payload(position=PositionSchema(action=SignalActionEnum.SHORT))
  signal = parse_signal(payload, "sig-2")
  assert signal.price == 0.0
  assert signal.quantity == 0.0
  assert signal.is_running is False
  assert signal.risk_percent == 0.0


def test_parse_signal_carries_scaling_when_flagged():
  payload = _payload(
    position=PositionSchema(
      action=SignalActionEnum.LONG,
      price=100.0,
      quantity=1.0,
      is_scale_position=True,
      scaling=ScalingSchema(tp=110.0, sl=95.0, quantity=0.5),
    )
  )
  signal = parse_signal(payload, "sig-3")
  assert signal.is_scale_position is True
  assert signal.scaling is not None
  assert signal.scaling.tp == 110.0
  assert signal.scaling.sl == 95.0
  assert signal.scaling.quantity == 0.5


def test_parse_signal_omits_scaling_when_not_flagged():
  payload = _payload(
    position=PositionSchema(
      action=SignalActionEnum.LONG,
      price=100.0,
      quantity=1.0,
      scaling=ScalingSchema(tp=110.0, sl=95.0, quantity=0.5),
    )
  )
  signal = parse_signal(payload, "sig-4")
  assert signal.is_scale_position is None
  assert signal.scaling is None


def test_parse_signal_carries_scale_strategy_when_flagged():
  payload = _payload(
    position=PositionSchema(
      action=SignalActionEnum.LONG,
      price=100.0,
      quantity=1.0,
      is_scale_position=True,
      scale_strategy="add_on_pullback",
      scaling=ScalingSchema(tp=110.0, sl=95.0, quantity=0.5),
    )
  )
  signal = parse_signal(payload, "sig-5")
  assert signal.is_scale_position is True
  assert signal.scale_strategy == "add_on_pullback"


def test_parse_signal_omits_scale_strategy_when_not_flagged():
  payload = _payload(
    position=PositionSchema(
      action=SignalActionEnum.LONG,
      price=100.0,
      quantity=1.0,
      scale_strategy="add_on_pullback",
    )
  )
  signal = parse_signal(payload, "sig-6")
  assert signal.is_scale_position is None
  assert signal.scale_strategy is None
