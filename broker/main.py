"""
broker/main.py — Entry point for the central broker process.

Startup sequence
────────────────
1. Init PostgreSQL connection pool + create tables (init_db)
2. Bind ZeroMQ PUB socket  (signals → subscribers)
3. Start FastAPI / uvicorn webhook server (blocks)
"""

from __future__ import annotations

import uvicorn

from broker.app import create_app
from broker.settings import settings
from broker.logger import get_logger

log = get_logger("broker")


def main() -> None:
  log.info("Starting Algo Trading Broker v1.0")
  log.info("Webhook    → http://%s:%d", settings.WEBHOOK_HOST, settings.WEBHOOK_PORT)
  log.info(
    "ZMQ PUB    → tcp://%s:%d (signals to subscribers)",
    settings.ZMQ_BROKER_HOST,
    settings.ZMQ_PUB_PORT,
  )
  log.info(
    "PostgreSQL → %s:%d/%s",
    settings.POSTGRES_HOST,
    settings.POSTGRES_PORT,
    settings.POSTGRES_DB,
  )

  # ── Start Webhook Server ────────────────────────
  app = create_app()

  uvicorn.run(
    app,
    host=settings.WEBHOOK_HOST,
    port=settings.WEBHOOK_PORT,
    log_level=settings.LOG_LEVEL.lower(),
  )


if __name__ == "__main__":
  main()
