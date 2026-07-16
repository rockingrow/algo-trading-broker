import json

from broker.services.signal_worker import SignalWorker


class FakeMsg:
  def __init__(self, data: bytes):
    self.data = data
    self.acked = False
    self.naked = False
    self.termed = False

  async def ack(self):
    self.acked = True

  async def nak(self):
    self.naked = True

  async def term(self):
    self.termed = True


class FakeService:
  def __init__(self, raise_exc: Exception | None = None):
    self.calls: list[tuple[str, dict]] = []
    self._raise = raise_exc

  async def handle_enqueued(self, *, signal_id, payload):
    if self._raise is not None:
      raise self._raise
    self.calls.append((signal_id, payload.model_dump()))
    return {"status": "accepted", "signal_id": signal_id}


def _envelope(signal_id: str = "sig-1") -> bytes:
  return json.dumps(
    {
      "signal_id": signal_id,
      "payload": {
        "strategy": "wt_cross_v1",
        "symbol": "OANDA:XAUUSD",
        "timeframe": "60",
        "timestamp": "2026-06-30T00:00:00+00:00",
        "position": {"action": "LONG", "price": 100.0, "quantity": 1.0},
        "token": "secret",
      },
    }
  ).encode()


async def test_valid_envelope_is_handled_and_acked():
  service = FakeService()
  worker = SignalWorker(service=service, connection=object())  # type: ignore[arg-type]
  msg = FakeMsg(_envelope())
  await worker._handle_one(msg)

  assert len(service.calls) == 1
  assert service.calls[0][0] == "sig-1"
  assert msg.acked is True
  assert msg.naked is False
  assert msg.termed is False


async def test_malformed_json_envelope_is_termed():
  service = FakeService()
  worker = SignalWorker(service=service, connection=object())  # type: ignore[arg-type]
  msg = FakeMsg(b"{not-json")
  await worker._handle_one(msg)

  assert service.calls == []
  assert msg.termed is True
  assert msg.acked is False


async def test_envelope_missing_signal_id_is_termed():
  service = FakeService()
  worker = SignalWorker(service=service, connection=object())  # type: ignore[arg-type]
  msg = FakeMsg(json.dumps({"payload": {}}).encode())
  await worker._handle_one(msg)

  assert msg.termed is True


async def test_invalid_webhook_payload_is_termed():
  service = FakeService()
  worker = SignalWorker(service=service, connection=object())  # type: ignore[arg-type]
  msg = FakeMsg(json.dumps({"signal_id": "s", "payload": {"foo": "bar"}}).encode())
  await worker._handle_one(msg)

  assert service.calls == []
  assert msg.termed is True


async def test_handler_failure_is_naked_for_redelivery():
  service = FakeService(raise_exc=RuntimeError("boom"))
  worker = SignalWorker(service=service, connection=object())  # type: ignore[arg-type]
  msg = FakeMsg(_envelope())
  await worker._handle_one(msg)

  assert msg.acked is False
  assert msg.naked is True
  assert msg.termed is False
