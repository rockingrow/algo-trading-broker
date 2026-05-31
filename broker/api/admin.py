from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, status

from broker.constants import SIGNAL_BLOCKED
from broker.providers import get_admin_notifier, get_setting_repository
from broker.interfaces import Notifier, SettingRepository
from broker.logger import get_logger
from broker.security.ensure_api_key import ensure_api_key

log = get_logger(__name__)


def get_admin_router() -> APIRouter:
  router = APIRouter(dependencies=[Depends(ensure_api_key)])

  @router.post("/settings/block-signal", tags=["settings"])
  async def toggle_block_signal(
    setting_repo: SettingRepository = Depends(get_setting_repository),
    notifier: Notifier = Depends(get_admin_notifier),
  ) -> Dict[str, str]:
    """Toggle SIGNAL_BLOCKED between '1' (enabled) and '0' (disabled)."""
    current = await setting_repo.get(SIGNAL_BLOCKED)
    new_value = "0" if current == "1" else "1"

    ok = await setting_repo.set(SIGNAL_BLOCKED, new_value)
    if not ok:
      raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to update broker setting",
      )

    state_label = "ENABLED" if new_value == "1" else "DISABLED"
    log.info("SIGNAL_BLOCKED toggled: %s -> %s", current, new_value)

    await notifier.send_message(
      f"⚙️ <b>Broker setting changed</b>\n"
      f"Setting: <code>{SIGNAL_BLOCKED}</code>\n"
      f"Signal blocked: <b>{state_label}</b>\n"
    )

    return {"setting": SIGNAL_BLOCKED, "value": new_value, "state": state_label}

  return router
