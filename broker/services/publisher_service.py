"""
broker/services/publisher_service.py — ZeroMQ PUB socket that broadcasts trading signals to all subscribers.
"""

from __future__ import annotations

import threading

import zmq

from broker.schemas.webhook_schema import TradingSignal
from broker.settings import settings
from broker.logger import get_logger

log = get_logger(__name__)


class SignalPublisher:
  """
  Thread-safe ZeroMQ PUB broker.

  All subscriber VPS nodes connect to this socket and receive
  topic-filtered messages in the format:

      <TOPIC>|<JSON payload>

  e.g.::

      SIGNAL|{"action":"open","symbol":"XAUUSD",...}
  """

  def __init__(self) -> None:
    self._lock = threading.Lock()
    self._context = zmq.Context()
    self._socket = self._context.socket(zmq.PUB)
    bind_addr = f"tcp://{settings.ZMQ_BROKER_HOST}:{settings.ZMQ_PUB_PORT}"
    self._socket.bind(bind_addr)
    log.info("ZeroMQ PUB socket bound to %s", bind_addr)

  def publish(self, signal: TradingSignal) -> None:
    """Serialise *signal* and broadcast to all connected subscribers."""
    topic = settings.ZMQ_TOPIC
    payload = signal.model_dump_json()
    message = f"{topic}|{payload}"

    with self._lock:
      self._socket.send_string(message)

    log.info(
      "Published [%s] signal_id=%s action=%s symbol=%s",
      topic,
      signal.signal_id,
      signal.action,
      signal.symbol,
    )

  def close(self) -> None:
    """Gracefully close socket and context."""
    self._socket.close()
    self._context.term()
    log.info("ZeroMQ PUB socket closed.")
