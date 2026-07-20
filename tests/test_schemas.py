import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from broker.schemas.account_schema import MarketTypeEnum
from broker.schemas.core import SignalActionEnum
from broker.schemas.publisher_schema import (
  AdminActionEnum,
  AdminSignal,
  PublishTopicEnum,
  TradingSignal,
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


def test_position_event_missing_required_rejected():
  d = _event_dict()
  del d["account_id"]
  with pytest.raises(ValidationError):
    PositionEvent(**d)


def test_position_event_invalid_market_rejected():
  with pytest.raises(ValidationError):
    PositionEvent(**_event_dict(market="STOCKS"))
