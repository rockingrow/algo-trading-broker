from datetime import datetime, timezone

from broker.helpers import emoji_constants as em
from broker.helpers.signal_helper import (
  action_to_emoji,
  parse_signal,
  payload_from_stored,
  payload_idempotency_key,
)
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
  assert action_to_emoji(SignalActionEnum.LONG) == em.LONG
  assert action_to_emoji(SignalActionEnum.SL) == em.SL


def test_parse_signal_normalises_symbol():
  signal = parse_signal(_payload(), "sig-1")
  assert signal.symbol == "XAUUSD"
  assert signal.signal_id == "sig-1"
  assert signal.action == SignalActionEnum.LONG


def test_parse_signal_defaults_missing_numbers():
  payload = _payload(position=PositionSchema(action=SignalActionEnum.SHORT))
  signal = parse_signal(payload, "sig-1")
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
  signal = parse_signal(payload, "sig-1")
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
  signal = parse_signal(payload, "sig-1")
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
  signal = parse_signal(payload, "sig-1")
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
  signal = parse_signal(payload, "sig-1")
  assert signal.is_scale_position is None
  assert signal.scale_strategy is None


def test_parse_signal_ships_both_ids():
  """``signal_id`` identifies the signal (the worker's de-duplication key);
  ``signal_uxid`` identifies the trade cycle it belongs to."""
  signal = parse_signal(_payload(signal_uxid="9f2c4b7e18a3d605"), "sig-7")
  assert signal.signal_id == "sig-7"
  assert signal.signal_uxid == "9F2C4B7E18A3D605"


def test_signal_id_stays_unique_per_action_while_the_cycle_id_is_shared():
  """Dedup by signal_id keeps working: two actions of one trade are two
  signals, and only the cycle id is common to them."""
  uxid = "9F2C4B7E18A3D605"
  entry = parse_signal(_payload(signal_uxid=uxid), "sig-entry")
  close = parse_signal(
    _payload(
      signal_uxid=uxid,
      position=PositionSchema(action=SignalActionEnum.TP1, price=110.0, quantity=0.5),
    ),
    "sig-close",
  )
  assert entry.signal_id != close.signal_id
  assert entry.signal_uxid == close.signal_uxid == uxid


# ── The alert's identity ────────────────────────────────────────────────────


def test_idempotency_key_is_stable_for_the_same_alert():
  uxid = "9F2C4B7E18A3D605"
  assert payload_idempotency_key(_payload(signal_uxid=uxid)) == (
    payload_idempotency_key(_payload(signal_uxid=uxid))
  )


def test_idempotency_key_ignores_the_token():
  """The token is the shared secret, not a fact about which alert this is —
  and it is excluded from the digest as well as from the persisted row."""
  uxid = "9F2C4B7E18A3D605"
  assert payload_idempotency_key(_payload(signal_uxid=uxid, token="a")) == (
    payload_idempotency_key(_payload(signal_uxid=uxid, token="b"))
  )


def test_idempotency_key_survives_a_json_round_trip():
  """The HTTP path keys the ``Nats-Msg-Id`` off the payload as received, while
  the worker keys the row off the payload rebuilt from the envelope. Both must
  land on the same digest, or the two layers would dedup on different keys."""
  original = _payload(signal_uxid="9F2C4B7E18A3D605")
  rebuilt = WebhookPayload(**original.model_dump(mode="json"))
  assert payload_idempotency_key(rebuilt) == payload_idempotency_key(original)


def test_a_scale_in_at_the_same_timestamp_keys_differently():
  """The digest covers every field, so a second entry on the same bar at a
  different price is its own alert — a business tuple of
  (uxid, action, timestamp) would have swallowed it."""
  uxid = "9F2C4B7E18A3D605"
  first = _payload(
    signal_uxid=uxid,
    position=PositionSchema(action=SignalActionEnum.LONG, price=2500.5, quantity=1.0),
  )
  second = _payload(
    signal_uxid=uxid,
    position=PositionSchema(action=SignalActionEnum.LONG, price=2501.0, quantity=1.0),
  )
  assert first.timestamp == second.timestamp
  assert payload_idempotency_key(first) != payload_idempotency_key(second)


def test_payload_from_stored_accepts_a_row_without_a_token():
  stored = _payload(signal_uxid="9F2C4B7E18A3D605").model_dump(
    mode="json", exclude={"token"}
  )
  assert "token" not in stored
  assert payload_from_stored(stored).signal_uxid == "9F2C4B7E18A3D605"
