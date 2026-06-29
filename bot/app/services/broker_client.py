"""
app/services/broker_client.py — Thin async client over the Broker HTTP API.

The bot never touches the database or NATS directly; everything goes through
the broker's ``/v1/telegram/*`` endpoints, authenticated with ``X-API-KEY``.
A single shared ``httpx.AsyncClient`` is created at startup and closed on
shutdown. Methods return parsed JSON (dict) on success, or ``None`` when the
resource is missing / the call fails — handlers decide what to tell the user.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from app.logger import get_logger

log = get_logger(__name__)


class BrokerClient:
  """Async wrapper around the broker's Telegram API surface."""

  def __init__(
    self,
    base_url: str,
    api_key: str,
    api_prefix: str = "",
    timeout: float = 10.0,
    transport: httpx.AsyncBaseTransport | None = None,
  ) -> None:
    prefix = api_prefix.strip("/")
    self._prefix = f"/{prefix}" if prefix else ""
    self._client = httpx.AsyncClient(
      base_url=base_url.rstrip("/"),
      headers={"X-API-KEY": api_key},
      timeout=timeout,
      transport=transport,  # injected in tests; None = default network transport
    )
    self._closed = False

  async def aclose(self) -> None:
    """Close the underlying HTTP client (idempotent)."""
    if not self._closed:
      self._closed = True
      await self._client.aclose()

  def _path(self, suffix: str) -> str:
    return f"{self._prefix}/v1/telegram{suffix}"

  async def _request(
    self, method: str, suffix: str, **kwargs: Any
  ) -> Optional[httpx.Response]:
    try:
      resp = await self._client.request(method, self._path(suffix), **kwargs)
    except httpx.HTTPError as exc:
      log.error("Broker request %s %s failed: %s", method, suffix, exc)
      return None
    if resp.status_code == 404:
      return resp  # let caller distinguish "not found" from transport errors
    if resp.status_code >= 400:
      log.error(
        "Broker %s %s returned %s: %s",
        method,
        suffix,
        resp.status_code,
        resp.text,
      )
      return None
    return resp

  async def link(self, token: str, telegram_user_id: int) -> Optional[dict[str, Any]]:
    """Bind a Telegram user to an account via its link token.

    Returns the linked account summary, or None if the token is invalid.
    """
    resp = await self._request(
      "POST",
      "/link",
      json={"token": token, "telegram_user_id": telegram_user_id},
    )
    if resp is None or resp.status_code == 404:
      return None
    return resp.json()

  async def get_account(self, telegram_user_id: int) -> Optional[dict[str, Any]]:
    """Return the account bound to a Telegram user, or None if unbound."""
    resp = await self._request("GET", f"/{telegram_user_id}")
    if resp is None or resp.status_code == 404:
      return None
    return resp.json()

  async def list_trades(
    self, telegram_user_id: int, limit: int = 5, offset: int = 0
  ) -> Optional[dict[str, Any]]:
    """Return a page of trades for the user's account."""
    resp = await self._request(
      "GET",
      f"/{telegram_user_id}/trades",
      params={"limit": limit, "offset": offset},
    )
    if resp is None or resp.status_code == 404:
      return None
    return resp.json()

  async def flat(
    self,
    telegram_user_id: int,
    symbol: Optional[str] = None,
    strategy: Optional[str] = None,
  ) -> Optional[dict[str, Any]]:
    """Close positions for the user's account."""
    resp = await self._request(
      "POST",
      f"/{telegram_user_id}/commands/flat",
      json={"symbol": symbol, "strategy": strategy},
    )
    if resp is None or resp.status_code == 404:
      return None
    return resp.json()

  async def prevent(
    self, telegram_user_id: int, enabled: bool = True
  ) -> Optional[dict[str, Any]]:
    """Block (enabled=True) or allow (enabled=False) new entries."""
    resp = await self._request(
      "POST",
      f"/{telegram_user_id}/commands/prevent",
      json={"enabled": enabled},
    )
    if resp is None or resp.status_code == 404:
      return None
    return resp.json()

  async def unlink(self, telegram_user_id: int) -> bool:
    """Clear the user's account binding. Returns True on success."""
    resp = await self._request("POST", f"/{telegram_user_id}/unlink")
    return resp is not None and resp.status_code < 400
