import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from broker.constants import ACCOUNT_SETTING_SIGNAL_BLOCKED
from broker.schemas.account_schema import AccountSettings, MarketTypeEnum
from broker.schemas.core import SignalActionEnum
from broker.schemas.publisher_schema import (
  AdminActionEnum,
  AdminSignal,
  PublishTopicEnum,
  SystemWorkerConnectedAck,
  TradingSignal,
  compose_admin_subject,
)
from broker.schemas.trade_event_schema import PositionEvent, PositionEventType
from broker.schemas.webhook_schema import (
  IndicatorsSchema,
  InputsSchema,
  PositionSchema,
  WebhookPayload,
)


# ── WebhookPayload ─────────────────────────────────────────────────


def _payload_dict(**overrides):
  base = {
    "strategy": "s",
    "symbol": "OANDA:XAUUSD",
    "timeframe": "60",
    "timestamp": "2026-01-01T00:00:00Z",
    "position": {"action": "LONG", "price": 1.0, "quantity": 1.0},
    "token": "t",
  }
  base.update(overrides)
  return base


def test_webhook_payload_minimal_valid():
  p = WebhookPayload(**_payload_dict())
  assert p.symbol == "OANDA:XAUUSD"
  assert p.position.action == SignalActionEnum.LONG
  assert p.indicators is None
  assert p.inputs is None


def test_webhook_payload_generates_a_signal_uxid_when_absent():
  """Alerts that don't send one still work — each simply becomes its own
  broadcast cycle, which is the pre-cycle behaviour."""
  p = WebhookPayload(**_payload_dict())
  assert len(p.signal_uxid) == 16
  assert p.signal_uxid != WebhookPayload(**_payload_dict()).signal_uxid


def test_webhook_payload_keeps_the_signal_uxid_it_was_given():
  p = WebhookPayload(**_payload_dict(signal_uxid="9f2c4b7e18a3d605"))
  assert p.signal_uxid == "9f2c4b7e18a3d605"


def test_webhook_payload_blank_signal_uxid_is_replaced():
  """A TradingView template that interpolates an empty placeholder must not
  key every cycle to the same blank id."""
  blank = WebhookPayload(**_payload_dict(signal_uxid="   "))
  null = WebhookPayload(**_payload_dict(signal_uxid=None))
  assert len(blank.signal_uxid) == 16
  assert len(null.signal_uxid) == 16
  assert blank.signal_uxid != null.signal_uxid


def test_webhook_payload_signal_uxid_survives_a_json_roundtrip():
  """The retry job rebuilds the payload from ``signals.raw``; a regenerated id
  there would split one cycle across two messages."""
  original = WebhookPayload(**_payload_dict(signal_uxid="9f2c4b7e18a3d605"))
  assert (
    WebhookPayload(**json.loads(original.model_dump_json())).signal_uxid
    == "9f2c4b7e18a3d605"
  )


def test_webhook_payload_generator_produces_a_valid_uxid():
  """Whatever the generator emits must itself pass the validator — otherwise
  the ``default_factory`` path could produce ids the ``mode=before`` validator
  would reject."""
  # A round-trip through the model exercises both the generator and the
  # validator on that generator's output.
  p1 = WebhookPayload(**_payload_dict())
  p2 = WebhookPayload(**_payload_dict(signal_uxid=p1.signal_uxid))
  assert p2.signal_uxid == p1.signal_uxid
  assert len(p1.signal_uxid) == 16
  assert p1.signal_uxid == p1.signal_uxid.lower()


def test_webhook_payload_uxid_uppercase_hex_is_normalised():
  """Different Pine templates uppercase UUID hex; that must not create a
  second cycle for the same underlying id."""
  p = WebhookPayload(**_payload_dict(signal_uxid="9F2C4B7E18A3D605"))
  assert p.signal_uxid == "9f2c4b7e18a3d605"


def test_webhook_payload_uxid_trims_surrounding_whitespace():
  p = WebhookPayload(**_payload_dict(signal_uxid=" 9f2c4b7e18a3d605  "))
  assert p.signal_uxid == "9f2c4b7e18a3d605"


def test_webhook_payload_uxid_wrong_length_is_rejected():
  """Rejecting at ingress is deliberate: a shortened id could collide with a
  real cycle and quietly merge two unrelated trades."""
  for bad in ("9f2c4b7e18a3d60", "9f2c4b7e18a3d6055", "abc", "a" * 32):
    with pytest.raises(ValidationError):
      WebhookPayload(**_payload_dict(signal_uxid=bad))


def test_webhook_payload_uxid_non_hex_is_rejected():
  """A UUID with dashes, or any other 16-char string that isn't hex."""
  for bad in ("9f2c-4b7e-18a3d6", "not-a-hex-id-abc", "9f2c4b7e18a3d60Z"):
    with pytest.raises(ValidationError):
      WebhookPayload(**_payload_dict(signal_uxid=bad))


def test_webhook_payload_invalid_action_rejected():
  with pytest.raises(ValidationError):
    WebhookPayload(**_payload_dict(position={"action": "BUY"}))


def test_webhook_payload_missing_token_rejected():
  d = _payload_dict()
  del d["token"]
  with pytest.raises(ValidationError):
    WebhookPayload(**d)


def test_indicators_and_inputs_allow_extra_fields():
  ind = IndicatorsSchema(wt1=1.0, custom_metric=42)
  assert ind.model_dump()["custom_metric"] == 42
  inp = InputsSchema(bb_len=20, my_param="x")
  assert inp.model_dump()["my_param"] == "x"


def test_position_optional_numbers_default_none():
  pos = PositionSchema(action=SignalActionEnum.SHORT)
  assert pos.price is None
  assert pos.sl is None
  assert pos.is_running is None


# ── TradingSignal ──────────────────────────────────────────────────


def test_trading_signal_serialises_enum_as_value():
  sig = TradingSignal(
    signal_id="id",
    strategy="s",
    action=SignalActionEnum.TP1,
    symbol="XAUUSD",
    price=1.0,
    quantity=1.0,
  )
  body = json.loads(sig.model_dump_json())
  assert body["action"] == "TP1"


def test_trading_signal_default_timestamp_is_utc():
  sig = TradingSignal(
    signal_id="id",
    strategy="s",
    action=SignalActionEnum.LONG,
    symbol="XAUUSD",
    price=1.0,
    quantity=1.0,
  )
  assert sig.timestamp.tzinfo is not None


# ── AdminSignal (regression: duplicate model_config must keep enum values) ──


def test_admin_signal_uses_enum_values_on_dump():
  sig = AdminSignal(action=AdminActionEnum.FLAT, strategy="s", symbol="XAUUSD")
  dumped = sig.model_dump()
  # use_enum_values=True must survive — the action is the raw string, not the enum.
  assert dumped["action"] == "FLAT"
  assert not isinstance(dumped["action"], AdminActionEnum)


def test_admin_signal_json_roundtrip():
  sig = AdminSignal(
    action=AdminActionEnum.FLAT,
    timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    account_id="acc",
    market=MarketTypeEnum.FOREX,
    gateway="MT5",
  )
  body = json.loads(sig.model_dump_json())
  assert body["action"] == "FLAT"
  assert body["account_id"] == "acc"
  assert body["market"] == "FOREX"
  assert body["gateway"] == "MT5"
  assert body["strategy"] is None


def test_admin_signal_requires_market_and_gateway_with_account_id():
  with pytest.raises(ValidationError):
    AdminSignal(action=AdminActionEnum.FLAT, account_id="acc")


def test_admin_signal_allows_no_account_id_without_market_and_gateway():
  # Global scope (flat/block everything) needs no account identification.
  sig = AdminSignal(action=AdminActionEnum.FLAT)
  assert sig.account_id is None


def test_publish_topic_enum_values():
  assert PublishTopicEnum.SIGNAL.value == "SIGNAL"
  assert PublishTopicEnum.ADMIN.value == "ADMIN"
  assert PublishTopicEnum.TRADE.value == "TRADE"


def test_compose_admin_subject_from_enum_market():
  from broker.schemas.account_schema import MarketTypeEnum

  assert (
    compose_admin_subject(MarketTypeEnum.FOREX, "MT5", "12345678")
    == "ADMIN.FOREX.MT5.12345678"
  )


def test_compose_admin_subject_from_string_market():
  assert (
    compose_admin_subject("CRYPTO", "BINANCE", "7654321")
    == "ADMIN.CRYPTO.BINANCE.7654321"
  )


# ── AccountSettings (accounts.settings ⇄ the ACK's settings block) ──


def test_account_settings_default_to_no_command_ever_run():
  # An account with `{}` in the column must still produce a complete block.
  assert AccountSettings().model_dump() == {"signal_blocked": False}


def test_account_settings_keys_match_the_constants():
  # The constant is what the command endpoint writes into the JSONB blob and
  # the field is what the worker reads out of the ACK — a rename that touches
  # only one of them would silently stop persisting.
  assert ACCOUNT_SETTING_SIGNAL_BLOCKED in AccountSettings.model_fields


def test_account_settings_drops_unknown_keys():
  # A key written by a newer broker is not forwarded to the worker (it stays in
  # the row — writes merge rather than replace).
  settings = AccountSettings(**{"signal_blocked": True, "from_the_future": "x"})
  assert settings.model_dump() == {"signal_blocked": True}


def test_account_settings_rejects_a_wrong_typed_value():
  # Callers catch this and fall back to the defaults rather than failing a
  # handshake; see _parse_account_settings.
  with pytest.raises(ValidationError):
    AccountSettings(signal_blocked="maybe")


def test_worker_connected_ack_always_carries_a_settings_block():
  ack = SystemWorkerConnectedAck(account_id="FOREX-MT5-1")
  body = json.loads(ack.model_dump_json())
  assert body["settings"] == {"signal_blocked": False}


# ── PositionEvent ──────────────────────────────────────────────────


def _event_dict(**overrides):
  base = {
    "event": "CREATED",
    "market": "FOREX",
    "strategy": "strat",
    "id": 1,
    "ref_source_id": "rs-1",
    "ref_id": "r-1",
    "symbol": "XAUUSD",
    "action": "LONG",
    "volume": 0.1,
    "opened_price": 100.0,
    "status": "OPENED",
    "account_id": "acc-1",
  }
  base.update(overrides)
  return base


def test_position_event_valid():
  ev = PositionEvent(**_event_dict())
  assert ev.event == PositionEventType.CREATED.value
  assert ev.risk_percent == 0.0  # default
  assert ev.closed_price is None
  assert ev.reject_reason is None  # default


def test_position_event_carries_reject_reason():
  ev = PositionEvent(
    **_event_dict(status="REJECTED", reject_reason="MAX ORDER limit reached")
  )
  assert ev.status == "REJECTED"
  assert ev.reject_reason == "MAX ORDER limit reached"


def test_position_event_rejected_for_open_position():
  # A worker that already holds an open position rejects the broker's new
  # SIGNAL and fires the same REJECTED TRADE, only the reason differs.
  ev = PositionEvent(
    **_event_dict(
      status="REJECTED", reject_reason="Open position already exists for XAUUSD"
    )
  )
  assert ev.status == "REJECTED"
  assert ev.reject_reason == "Open position already exists for XAUUSD"


def test_position_event_missing_required_rejected():
  d = _event_dict()
  del d["account_id"]
  with pytest.raises(ValidationError):
    PositionEvent(**d)


def test_position_event_invalid_market_rejected():
  with pytest.raises(ValidationError):
    PositionEvent(**_event_dict(market="STOCKS"))
