"""
broker/services/publisher_service.py — ZeroMQ PUB socket that broadcasts trading signals to all subscribers.

Security
--------
When ``settings.ZMQ_CURVE_ENABLED`` is ``True`` the socket is hardened with
ZeroMQ CURVE (Elliptic-Curve Diffie-Hellman) so that:

* Only clients that possess the server's *public* key can subscribe.
* All traffic is encrypted end-to-end.
* The server authenticates clients that present a valid *client* public key
  (allow-any-client mode is used by default; switch to an allowlist via
  :class:`zmq.auth.thread.ThreadAuthenticator` if you need per-client ACLs).

Key generation::

    python scripts/generate_curve_keypair.py
"""

from __future__ import annotations

import threading

import zmq
import zmq.auth
import zmq.utils.monitor
from zmq.auth.thread import ThreadAuthenticator

from broker.schemas.publisher_schema import PublishTopicEnum, TradingSignal
from broker.services.notification_service import TelegramNotification
from broker.settings import settings
from broker.logger import get_logger

log = get_logger(__name__)


class SignalPublisher:
  """
  Thread-safe ZeroMQ PUB broker.

  All subscriber VPS nodes connect to this socket and receive
  topic-filtered messages in the format::

      <TOPIC>|<JSON payload>

  e.g.::

      SIGNAL|{"action":"open","symbol":"XAUUSD",...}

  When CURVE is enabled the socket acts as the *server* side of the
  CURVE handshake.  Clients must supply the server's public key when
  connecting.
  """

  def __init__(self) -> None:
    self._lock = threading.Lock()
    self._context = zmq.Context()
    self._auth: ThreadAuthenticator | None = None
    self._monitor_stop = threading.Event()

    self._socket = self._context.socket(zmq.PUB)

    if settings.ZMQ_CURVE_ENABLED:
      self._setup_curve()

    bind_addr = f"tcp://{settings.ZMQ_BROKER_HOST}:{settings.ZMQ_PUB_PORT}"
    self._socket.bind(bind_addr)
    log.info(
      "ZeroMQ PUB socket bound to %s (CURVE=%s)",
      bind_addr,
      settings.ZMQ_CURVE_ENABLED,
    )

    # Start background thread to detect new subscriber connections
    self._start_monitor()

  # ── Private helpers ───────────────────────────────────────────────

  def _start_monitor(self) -> None:
    """Spin up a daemon thread that watches for new worker connections."""
    self._socket.monitor(
      "inproc://publisher-monitor",
      zmq.EVENT_ACCEPTED | zmq.EVENT_HANDSHAKE_SUCCEEDED | zmq.EVENT_HANDSHAKE_FAILED_NO_DETAIL,
    )
    monitor_sock = self._context.socket(zmq.PAIR)
    monitor_sock.connect("inproc://publisher-monitor")

    def _watch() -> None:
      notification = TelegramNotification()
      while not self._monitor_stop.is_set():
        if monitor_sock.poll(timeout=500):  # 500 ms tick
          try:
            event_data = zmq.utils.monitor.recv_monitor_message(monitor_sock)
            event = event_data["event"]
            endpoint = event_data.get("endpoint", b"").decode(errors="replace")

            if event == zmq.EVENT_ACCEPTED:
              log.info("ZMQ worker connected: %s", endpoint)
              # Only notify on accept if CURVE is disabled (to avoid double notification)
              if not settings.ZMQ_CURVE_ENABLED:
                notification.send_message(
                  f"🔌 <b>ZMQ Worker Connected</b>\nEndpoint: <code>{endpoint}</code>"
                )

            elif event == zmq.EVENT_HANDSHAKE_SUCCEEDED:
              log.info("ZMQ worker CURVE handshake succeeded: %s", endpoint)
              notification.send_message(
                f"🔐 <b>ZMQ Worker Authenticated (CURVE)</b>\nEndpoint: <code>{endpoint}</code>"
              )

            elif event == zmq.EVENT_HANDSHAKE_FAILED_NO_DETAIL:
              log.warning("ZMQ worker CURVE handshake failed: %s", endpoint)
              notification.send_message(
                f"⚠️ <b>ZMQ Worker Auth Failed (CURVE)</b>\nEndpoint: <code>{endpoint}</code>"
              )
          except Exception as exc:
            log.warning("ZMQ monitor error: %s", exc)
      monitor_sock.close()

    t = threading.Thread(target=_watch, name="zmq-monitor", daemon=True)
    t.start()
    self._monitor_thread = t

  def _setup_curve(self) -> None:
    """Configure CURVE server-side encryption & authentication."""
    if not settings.ZMQ_CURVE_SERVER_PUBLIC_KEY:
      raise ValueError(
        "ZMQ_CURVE_ENABLED=true but ZMQ_CURVE_SERVER_PUBLIC_KEY is not set. "
        "Run `python scripts/generate_curve_keypair.py` to create a keypair."
      )
    if not settings.ZMQ_CURVE_SERVER_SECRET_KEY:
      raise ValueError(
        "ZMQ_CURVE_ENABLED=true but ZMQ_CURVE_SERVER_SECRET_KEY is not set. "
        "Run `python scripts/generate_curve_keypair.py` to create a keypair."
      )

    # Start background authenticator thread that handles ZAP protocol
    # "allow any" means any client that knows the server public key can connect.
    # To restrict to specific client public keys, configure an allowlist here.
    self._auth = ThreadAuthenticator(self._context)
    self._auth.start()
    self._auth.allow_any = True  # allow any client that owns the server pubkey
    self._auth.configure_curve(domain="*", location=zmq.auth.CURVE_ALLOW_ANY)
    log.info("ZeroMQ CURVE authenticator started (allow-any client mode).")

    # Apply server keypair to the socket
    self._socket.curve_server = True
    self._socket.curve_publickey = settings.ZMQ_CURVE_SERVER_PUBLIC_KEY.encode()
    self._socket.curve_secretkey = settings.ZMQ_CURVE_SERVER_SECRET_KEY.encode()

  # ── Public API ────────────────────────────────────────────────────

  def publish(self, topic: PublishTopicEnum = PublishTopicEnum.SIGNAL, signal: TradingSignal = None) -> None:
    """Serialise *signal* and broadcast to all connected subscribers."""
    if signal is None:
      return
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
    """Gracefully close socket, authenticator, and context."""
    # Signal the monitor thread to stop and wait briefly for it
    self._monitor_stop.set()
    if hasattr(self, "_monitor_thread"):
      self._monitor_thread.join(timeout=2)
    self._socket.disable_monitor()
    self._socket.close()
    if self._auth is not None:
      self._auth.stop()
      log.info("ZeroMQ CURVE authenticator stopped.")
    self._context.term()
    log.info("ZeroMQ PUB socket closed.")
