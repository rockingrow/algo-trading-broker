from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, status

from broker.security.ensure_api_key import ensure_api_key
from broker.db.repository import (
  get_broker_setting_by_key,
  set_broker_setting_value,
)
from broker.settings import settings
from broker.constants import SIGNAL_BLOCKED
from broker.services.notification_service import TelegramNotification
from broker.logger import get_logger

log = get_logger(__name__)


def get_admin_router() -> APIRouter:
  router = APIRouter(dependencies=[Depends(ensure_api_key)])

  @router.post("/settings/block-signal", tags=["settings"])
  async def toggle_block_signal() -> Dict[str, str]:
    """Toggle SIGNAL_BLOCKED between '1' (enabled) and '0' (disabled)."""
    current = await get_broker_setting_by_key(SIGNAL_BLOCKED)
    new_value = "0" if current == "1" else "1"

    ok = await set_broker_setting_value(SIGNAL_BLOCKED, new_value)
    if not ok:
      raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to update broker setting",
      )

    state_label = "ENABLED" if new_value == "1" else "DISABLED"
    log.info("SIGNAL_BLOCKED toggled: %s -> %s", current, new_value)

    TelegramNotification(chat_id=settings.TELEGRAM_CHAT_ID).send_message(
      f"⚙️ <b>Broker setting changed</b>\n"
      f"Setting: <code>{SIGNAL_BLOCKED}</code>\n"
      f"Signal blocked: <b>{state_label}</b>\n"
    )

    return {"setting": SIGNAL_BLOCKED, "value": new_value, "state": state_label}

  return router
