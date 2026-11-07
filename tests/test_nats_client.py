"""``NatsClient.connect``: the broker's own connection and its streams."""

from __future__ import annotations

import pytest

import broker.nats as nats_module
from broker.nats import BROKER_CLIENT_NAME, NatsClient


class FakeConnection:
  def __init__(self):
    self.closed = False

  async def close(self):
    self.closed = True


@pytest.fixture
def fake_connect(monkeypatch):
  calls: list[dict] = []
  connection = FakeConnection()

  async def connect(**options):
    calls.append(options)
    return connection

  monkeypatch.setattr(nats_module.nats_lib, "connect", connect)
  return calls, connection


async def test_connect_names_the_connection_and_ensures_both_streams(
  fake_connect, monkeypatch
):
  calls, _ = fake_connect
  ensured: list[str] = []
  client = NatsClient()

  async def ensure_signal_stream():
    ensured.append("SIGNALS")

  async def ensure_trade_stream():
    ensured.append("TRADES")

  monkeypatch.setattr(client, "ensure_signal_stream", ensure_signal_stream)
  monkeypatch.setattr(client, "ensure_trade_stream", ensure_trade_stream)
  await client.connect()

  assert calls[0]["name"] == BROKER_CLIENT_NAME
  assert ensured == ["SIGNALS", "TRADES"]


async def test_a_failed_stream_setup_closes_the_connection(fake_connect, monkeypatch):
  _, connection = fake_connect
  client = NatsClient()

  async def jetstream_disabled():
    raise RuntimeError("JetStream not enabled")

  monkeypatch.setattr(client, "ensure_signal_stream", jetstream_disabled)
  with pytest.raises(RuntimeError):
    await client.connect()

  # Not left behind reconnecting forever after a failed startup.
  assert connection.closed is True
  assert client.is_connected is False
