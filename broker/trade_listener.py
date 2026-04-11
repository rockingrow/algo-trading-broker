"""
broker/trade_listener.py
─────────────────────────
ZeroMQ PULL socket that listens for trade event reports pushed by subscriber VPS nodes.

Each subscriber pushes a JSON message to tcp://broker:<ZMQ_PULL_PORT> after:
  - successfully opening a position   → event_type = "entry_confirmed"
  - successfully closing a position   → event_type = "close_confirmed"
  - a failed order attempt            → event_type = "error"
  - any custom status update          → event_type = "status"

The listener runs in a dedicated daemon thread (started from main.py) and uses
asyncio.run_coroutine_threadsafe to write each event to PostgreSQL without
blocking the ZMQ receive loop.

Subscriber message format (JSON string):
{
    "event_type":    "entry_confirmed",
    "signal_id":     "uuid-of-original-signal",
    "subscriber_id": "vps-01",
    "ticket":        12345,
    "symbol":        "XAUUSD",
    "direction":     "buy",
    "volume":        0.1,
    "open_price":    2350.50,
    "sl":            2330.00,
    "tp":            2400.00,
    "timestamp":     "2024-01-01T12:00:00+00:00",
    "message":       "Order opened successfully"
}
"""

from __future__ import annotations

import asyncio
import json
import threading

import zmq

from broker.settings import settings
from broker.logger import get_logger

log = get_logger(__name__)


class TradeEventListener:
  """
  Runs a ZMQ PULL socket in a background thread.
  Each received message is persisted to PostgreSQL via the async event loop.

  Usage::

      listener = TradeEventListener(loop=asyncio.get_event_loop())
      listener.start()   # spawns daemon thread
      ...
      listener.stop()
  """

  def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
    self._loop = loop
    self._running = False
    self._thread: threading.Thread | None = None

    self._context = zmq.Context()
    self._socket = self._context.socket(zmq.PULL)
    bind_addr = f"tcp://{settings.ZMQ_PULL_HOST}:{settings.ZMQ_PULL_PORT}"
    self._socket.bind(bind_addr)
    self._socket.setsockopt(zmq.RCVTIMEO, 1000)  # 1 s timeout → allows clean stop
    log.info("ZeroMQ PULL socket bound to %s (trade event listener)", bind_addr)

  # ── Lifecycle ──────────────────────────────────────────────────

  def start(self) -> None:
    """Start the background receive thread."""
    self._running = True
    self._thread = threading.Thread(
      target=self._receive_loop,
      name="trade-listener",
      daemon=True,
    )
    self._thread.start()
    log.info("Trade event listener thread started.")

  def stop(self) -> None:
    """Signal the thread to stop and wait for it to finish."""
    self._running = False
    if self._thread:
      self._thread.join(timeout=3)
    self._socket.close()
    self._context.term()
    log.info("Trade event listener stopped.")

  # ── Internal ───────────────────────────────────────────────────

  def _receive_loop(self) -> None:
    """Blocking ZMQ receive loop — runs in daemon thread."""
    while self._running:
      try:
        raw: str = self._socket.recv_string()
      except zmq.Again:
        # Timeout → loop again to check _running flag
        continue
      except zmq.ZMQError as exc:
        if self._running:
          log.error("ZMQ PULL receive error: %s", exc)
        break

      self._handle(raw)

  def _handle(self, raw: str) -> None:
    """Parse and persist a single trade event message."""
    try:
      data: dict = json.loads(raw)
    except json.JSONDecodeError as exc:
      log.warning("Invalid JSON from subscriber: %s | raw=%r", exc, raw[:200])
      return

    event_type = data.get("event_type", "unknown")
    subscriber_id = data.get("subscriber_id", "?")
    log.info(
      "Trade event received: type=%s subscriber=%s signal_id=%s ticket=%s",
      event_type,
      subscriber_id,
      data.get("signal_id"),
      data.get("ticket"),
    )

    # Schedule the async DB write on the main event loop
    from broker.db.repository import log_trade_event  # late import avoids circular

    asyncio.run_coroutine_threadsafe(log_trade_event(data), self._loop)
