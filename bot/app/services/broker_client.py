"""
app/services/broker_client.py — Thin async client over the Broker HTTP API.

The bot never touches the database or NATS directly; everything goes through
broker HTTP endpoints, authenticated with ``X-API-KEY``. A single shared
``httpx.AsyncClient`` is created at startup and closed on shutdown. Methods
return parsed JSON (dict/list) on success, or ``None`` when the resource is
missing / the call fails — handlers decide what to tell the user.

Enduser methods hit ``/v1/telegram/*``; admin methods reuse the broker's
existing management endpoints (``/v1/accounts``, ``/v1/{id}/trades``,
``/admin/*``).
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from app.logger import get_logger

log = get_logger(__name__)


class BrokerClient:
  """Async wrapper around the broker API surface the bot needs."""

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

  def _url(self, path: str) -> str:
    """Prepend the optional broker API prefix to an absolute broker path."""
    return f"{self._prefix}{path}"

  async def _request(
    self, method: str, path: str, **kwargs: Any
  ) -> Optional[httpx.Response]:
    try:
      resp = await self._client.request(method, self._url(path), **kwargs)
    except httpx.HTTPError as exc:
      log.error("Broker request %s %s failed: %s", method, path, exc)
      return None
    if resp.status_code == 404:
      return resp  # let caller distinguish "not found" from transport errors
    if resp.status_code >= 400:
      log.error(
        "Broker %s %s returned %s: %s", method, path, resp.status_code, resp.text
      )
      return None
    return resp

  @staticmethod
  def _json_or_none(resp: Optional[httpx.Response]) -> Optional[Any]:
    if resp is None or resp.status_code == 404:
      return None
    return resp.json()

  # ── Enduser (/v1/telegram/*) ──────────────────────────────────────

  async def link(self, token: str, telegram_user_id: int) -> Optional[dict[str, Any]]:
    """Bind a Telegram user to an account via its link token (None if invalid)."""
    return self._json_or_none(
      await self._request(
        "POST",
        "/v1/telegram/link",
        json={"token": token, "telegram_user_id": telegram_user_id},
      )
    )

  async def get_account(self, telegram_user_id: int) -> Optional[dict[str, Any]]:
    """Return the account bound to a Telegram user, or None if unbound."""
    return self._json_or_none(
      await self._request("GET", f"/v1/telegram/{telegram_user_id}")
    )

  async def list_trades(
    self, telegram_user_id: int, limit: int = 5, offset: int = 0
  ) -> Optional[dict[str, Any]]:
    """Return a page of trades for the user's account."""
    return self._json_or_none(
      await self._request(
        "GET",
        f"/v1/telegram/{telegram_user_id}/trades",
        params={"limit": limit, "offset": offset},
      )
    )

  async def flat(
    self,
    telegram_user_id: int,
    symbol: Optional[str] = None,
    strategy: Optional[str] = None,
  ) -> Optional[dict[str, Any]]:
    """Close positions for the user's account."""
    return self._json_or_none(
      await self._request(
        "POST",
        f"/v1/telegram/{telegram_user_id}/commands/flat",
        json={"symbol": symbol, "strategy": strategy},
      )
    )

  async def prevent(
    self, telegram_user_id: int, enabled: bool = True
  ) -> Optional[dict[str, Any]]:
    """Block (enabled=True) or allow (enabled=False) new entries."""
    return self._json_or_none(
      await self._request(
        "POST",
        f"/v1/telegram/{telegram_user_id}/commands/prevent",
        json={"enabled": enabled},
      )
    )

  async def unlink(self, telegram_user_id: int) -> bool:
    """Clear the user's account binding. Returns True on success."""
    resp = await self._request("POST", f"/v1/telegram/{telegram_user_id}/unlink")
    return resp is not None and resp.status_code < 400

  # ── Admin (reuses broker management endpoints) ────────────────────

  async def admin_list_accounts(self) -> Optional[list[dict[str, Any]]]:
    """All trading accounts (includes telegram_link_token + link status)."""
    return self._json_or_none(await self._request("GET", "/v1/accounts"))

  async def admin_list_trades(
    self, account_id: str, limit: int = 5, offset: int = 0
  ) -> Optional[dict[str, Any]]:
    """Trades for any account (admin — not scoped to the caller)."""
    return self._json_or_none(
      await self._request(
        "GET",
        f"/v1/{account_id}/trades",
        params={"limit": limit, "offset": offset},
      )
    )

  async def admin_flat(
    self,
    strategy: Optional[str] = None,
    symbol: Optional[str] = None,
    account_id: Optional[str] = None,
  ) -> Optional[dict[str, Any]]:
    """Publish a FLAT directive (all fields None = flat everything)."""
    return self._json_or_none(
      await self._request(
        "POST",
        "/admin/flat",
        json={"strategy": strategy, "symbol": symbol, "account_id": account_id},
      )
    )

  async def admin_rotate_token(self, account_id: str) -> Optional[dict[str, Any]]:
    """Rotate an account's Telegram link token; returns the new token."""
    return self._json_or_none(
      await self._request("POST", f"/admin/accounts/{account_id}/link-token/rotate")
    )

  async def admin_get_settings(self) -> Optional[list[dict[str, Any]]]:
    """Current state of the broker toggle settings."""
    return self._json_or_none(await self._request("GET", "/admin/settings"))

  async def admin_toggle_setting(self, slug: str) -> Optional[dict[str, Any]]:
    """Toggle a broker setting (slug: block-signal, silent-signal, include-signal-raw)."""
    return self._json_or_none(await self._request("POST", f"/admin/settings/{slug}"))
