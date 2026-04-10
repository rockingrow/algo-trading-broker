"""
broker/webhook.py — FastAPI application that receives TradingView webhook alerts.

On every successful webhook:
  1. Validate HMAC signature (if WEBHOOK_SECRET is set)
  2. Parse + validate the JSON payload → TradingSignal
  3. Write the signal to PostgreSQL (signal_log table)
  4. Publish the signal via ZeroMQ PUB to all subscriber VPS nodes
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Dict

from fastapi import FastAPI, Header, HTTPException, Request, status

from broker.db.repository import log_signal
from broker.models import BrokerStatus, TradingSignal, WebhookPayload
from broker.publisher import SignalPublisher
from broker.signal_parser import parse_signal
from shared.config import settings
from shared.logger import get_logger

log = get_logger(__name__)

# ── In-memory counters (fast status endpoint) ─────────────────────
_start_time = time.time()
_signals_received: int = 0
_signals_published: int = 0
_last_signal: TradingSignal | None = None


def create_app(publisher: SignalPublisher) -> FastAPI:
    """Build and return the FastAPI application with all routes wired up."""

    app = FastAPI(
        title="Algo Trading Broker",
        description=(
            "Receives TradingView JSON webhook alerts, logs them to PostgreSQL, "
            "and fans them out via ZeroMQ PUB to subscriber VPS nodes."
        ),
        version="2.0.0",
    )

    # ── Helpers ────────────────────────────────────────────────────

    def _verify_hmac(body: bytes, signature: str | None) -> None:
        """Raise 401 if HMAC validation is enabled and the signature is wrong."""
        if not settings.WEBHOOK_SECRET:
            return  # validation disabled
        if signature is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing X-Signature header.",
            )
        expected = hmac.new(
            settings.WEBHOOK_SECRET.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid HMAC signature.",
            )

    # ── Routes ─────────────────────────────────────────────────────

    @app.get("/health", tags=["system"])
    async def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/status", response_model=BrokerStatus, tags=["system"])
    async def broker_status() -> BrokerStatus:
        return BrokerStatus(
            uptime_seconds=round(time.time() - _start_time, 1),
            signals_received=_signals_received,
            signals_published=_signals_published,
            last_signal=_last_signal,
        )

    @app.post("/webhook", status_code=status.HTTP_202_ACCEPTED, tags=["webhook"])
    async def receive_webhook(
        request: Request,
        x_signature: str | None = Header(default=None, alias="X-Signature"),
    ) -> Dict[str, Any]:
        global _signals_received, _signals_published, _last_signal

        # 1. Read raw body + HMAC check
        body = await request.body()
        _verify_hmac(body, x_signature)

        raw: Dict[str, Any] = await request.json()
        log.info("Webhook received: %s", raw)

        # 2. Parse & validate
        parse_error: str | None = None
        signal: TradingSignal | None = None
        try:
            payload = WebhookPayload(**raw)
            signal = parse_signal(payload)
        except Exception as exc:
            parse_error = str(exc)
            log.error("Signal parse error: %s", exc)

        if signal is None:
            # Log failed parse attempt to DB (best-effort) then return 422
            _signals_received += 1
            await log_signal(
                signal=TradingSignal(
                    action="open",          # placeholder — parse failed
                    symbol=raw.get("symbol", "UNKNOWN"),
                ),
                raw_payload=raw,
                published=False,
                error=parse_error,
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=parse_error,
            )

        _signals_received += 1

        # 3. Write to PostgreSQL (signal_log) — before publishing so we
        #    always have a record even if ZMQ publish fails.
        publish_error: str | None = None
        published = False
        try:
            publisher.publish(signal)
            published = True
            _signals_published += 1
            _last_signal = signal
        except Exception as exc:
            publish_error = str(exc)
            log.error("ZMQ publish error: %s", exc)

        await log_signal(
            signal=signal,
            raw_payload=raw,
            published=published,
            error=publish_error,
        )

        # 4. Return — raise error AFTER logging so the DB record exists
        if not published:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Signal logged but publish failed: {publish_error}",
            )

        return {
            "status": "accepted",
            "signal_id": signal.signal_id,
            "timestamp": signal.timestamp.isoformat(),
        }

    return app
