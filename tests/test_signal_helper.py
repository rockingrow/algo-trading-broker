from datetime import datetime, timezone

from broker.helpers.signal_helper import action_to_emoji, parse_signal
from broker.schemas.core import SignalActionEnum
from broker.schemas.webhook_schema import PositionSchema, WebhookPayload


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
