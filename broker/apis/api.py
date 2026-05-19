from typing import Dict

from fastapi import APIRouter, HTTPException, status

from broker.db.repository import get_broker_setting_by_key, set_broker_setting_value
from broker.settings import settings
from broker.constants import PREVENT_SIGNAL
from broker.services.notification_service import TelegramNotification
from broker.logger import get_logger

log = get_logger(__name__)


def get_router() -> APIRouter:
  router = APIRouter()

  @router.get("/health", tags=["system"])
  async def health() -> Dict[str, str]:
    return {"status": "ok"}

  @router.post("/settings/prevent-signal", tags=["settings"])
  async def toggle_prevent_signal() -> Dict[str, str]:
    """Toggle PREVENT_SIGNAL between '1' (enabled) and '0' (disabled)."""
    current = await get_broker_setting_by_key(PREVENT_SIGNAL)
    new_value = "0" if current == "1" else "1"

    ok = await set_broker_setting_value(PREVENT_SIGNAL, new_value)
    if not ok:
      raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to update broker setting",
      )

    state_label = "ENABLED" if new_value == "1" else "DISABLED"
    log.info("PREVENT_SIGNAL toggled: %s -> %s", current, new_value)

    TelegramNotification(chat_id=settings.TELEGRAM_CHAT_ID).send_message(
      f"⚙️ <b>Broker setting changed</b>\n"
      f"Setting: <code>{PREVENT_SIGNAL}</code>\n"
      f"Signal processing: <b>{state_label}</b>\n"
    )

    return {"setting": PREVENT_SIGNAL, "value": new_value, "state": state_label}

  return router
