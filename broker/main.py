"""
broker/main.py — Entry point for the central broker process.

Startup sequence
────────────────
1. Init PostgreSQL connection pool + create tables (init_db)
2. Bind ZeroMQ PUB socket  (signals → subscribers)
3. Bind ZeroMQ PULL socket (trade events ← subscribers) in a background thread
4. Start FastAPI / uvicorn webhook server (blocks)
"""
from __future__ import annotations

import asyncio
import signal
import sys
import threading

import uvicorn

from broker.db.engine import close_db, init_db
from broker.publisher import SignalPublisher
from broker.trade_listener import TradeEventListener
from broker.webhook import create_app
from shared.config import settings
from shared.logger import get_logger

log = get_logger("broker")


def main() -> None:
    log.info("Starting Algo Trading Broker v2.0")
    log.info("Webhook    → http://%s:%d", settings.WEBHOOK_HOST, settings.WEBHOOK_PORT)
    log.info("ZMQ PUB    → tcp://%s:%d (signals to subscribers)", settings.ZMQ_BROKER_HOST, settings.ZMQ_PUB_PORT)
    log.info("ZMQ PULL   → tcp://%s:%d (trade events from subscribers)", settings.ZMQ_PULL_HOST, settings.ZMQ_PULL_PORT)
    log.info("PostgreSQL → %s:%d/%s", settings.POSTGRES_HOST, settings.POSTGRES_PORT, settings.POSTGRES_DB)

    # ── 1. Initialise DB (synchronously via asyncio.run) ──────────
    asyncio.run(init_db())

    # ── 2. ZeroMQ PUB publisher ───────────────────────────────────
    publisher = SignalPublisher()

    # ── 3. ZeroMQ PULL trade-event listener (background thread) ───
    # We need the event loop that uvicorn will use so we can bridge
    # the sync ZMQ thread back into async DB writes.
    # We create the loop first, pass it to the listener, then hand
    # it to uvicorn via the `loop` parameter.
    loop = asyncio.new_event_loop()

    trade_listener = TradeEventListener(loop=loop)
    trade_listener.start()

    # ── 4. Graceful shutdown handler ──────────────────────────────
    def _shutdown(sig, frame):  # noqa: ANN001
        log.info("Shutdown signal received — cleaning up...")
        trade_listener.stop()
        publisher.close()
        asyncio.run_coroutine_threadsafe(close_db(), loop)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # ── 5. Build FastAPI app + run uvicorn ────────────────────────
    app = create_app(publisher)

    uvicorn.run(
        app,
        host=settings.WEBHOOK_HOST,
        port=settings.WEBHOOK_PORT,
        log_level=settings.LOG_LEVEL.lower(),
        loop="none",          # we manage our own loop
    )


if __name__ == "__main__":
    main()
