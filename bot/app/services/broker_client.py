"""
app/services/broker_client.py — Thin async client over the Broker HTTP API.

The bot never touches the database or NATS directly; everything goes through
broker HTTP endpoints, authenticated with ``X-API-KEY``. A single shared
``httpx.AsyncClient`` is created per client instance at startup and closed on
shutdown. Methods return parsed JSON (dict/list) on success, or ``None`` when
the resource is missing / the call fails — handlers decide what to tell the
user.

``BrokerClient`` holds the shared HTTP plumbing (connection, request/response
handling, and the ``BASE_PATH`` + ``ENDPOINTS`` convention). Each subclass
sets ``BASE_PATH`` to the resource root it lives under and declares its
endpoint templates as a nested ``ENDPOINTS`` enum (subclassing ``Endpoint``);
methods build request paths via ``self._path(self.ENDPOINTS.X, **params)``
instead of hand-rolling f-strings.

``BrokerClientUser`` (``/v1/telegram/*``) and ``BrokerClientAdmin``
(``/v1/accounts``, ``/v1/{id}/trades``, ``/admin/*``) extend it with the
endpoint groups each audience actually needs, so new endpoints for one side
don't have to be reviewed against the other's surface.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Optional

import httpx

from app.logger import get_logger

log = get_logger(__name__)


class Endpoint(StrEnum):
  """Marker base for per-client endpoint enums (values are path templates)."""


class BrokerClient:
  """Shared HTTP plumbing for talking to the broker API."""

  BASE_PATH: str
  ENDPOINTS: type[Endpoint] = Endpoint

  def __init_subclass__(cls, **kwargs: Any) -> None:
    super().__init_subclass__(**kwargs)
    if not cls.__dict__.get("BASE_PATH"):
      raise TypeError(f"{cls.__name__} must declare a non-empty BASE_PATH")

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

  def _path(self, endpoint: Endpoint, **params: Any) -> str:
    """Join ``BASE_PATH`` with an endpoint template, filling in ``{placeholders}``."""
    return f"{self.BASE_PATH}/{endpoint.format(**params)}"

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


class BrokerClientUser(BrokerClient):
  """Enduser-facing endpoints (``/v1/telegram/*``), scoped to the caller's account."""

  BASE_PATH = "/v1/telegram"

  class ENDPOINTS(Endpoint):
    LINK = "link"
    ACCOUNT = "{telegram_id}"
    ACCOUNTS = "{telegram_id}/accounts"
    SWITCH = "{telegram_id}/active-account"
    TRADES = "{telegram_id}/trades"
    FLAT = "{telegram_id}/commands/flat"
    PREVENT = "{telegram_id}/commands/prevent"
    UNLINK = "{telegram_id}/unlink"

  async def link(self, token: str, telegram_user_id: int) -> Optional[dict[str, Any]]:
    """Bind a Telegram user to an account via its link token (None if invalid)."""
    return self._json_or_none(
      await self._request(
        "POST",
        self._path(self.ENDPOINTS.LINK),
        json={"token": token, "telegram_user_id": telegram_user_id},
      )
    )

  async def get_account(self, telegram_user_id: int) -> Optional[dict[str, Any]]:
    """Return the Telegram user's currently active account, or None if unbound."""
    return self._json_or_none(
      await self._request(
        "GET", self._path(self.ENDPOINTS.ACCOUNT, telegram_id=telegram_user_id)
      )
    )

  async def list_accounts(self, telegram_user_id: int) -> Optional[list[dict[str, Any]]]:
    """Return every account linked to a Telegram user (empty list if none)."""
    return self._json_or_none(
      await self._request(
        "GET", self._path(self.ENDPOINTS.ACCOUNTS, telegram_id=telegram_user_id)
      )
    )

  async def switch_account(
    self, telegram_user_id: int, account_id: str
  ) -> Optional[dict[str, Any]]:
    """Set which of the user's linked accounts is active. None if the
    account_id doesn't exist or isn't linked to this Telegram user."""
    return self._json_or_none(
      await self._request(
        "POST",
        self._path(self.ENDPOINTS.SWITCH, telegram_id=telegram_user_id),
        json={"account_id": account_id},
      )
    )

  async def list_trades(
    self, telegram_user_id: int, limit: int = 5, offset: int = 0
  ) -> Optional[dict[str, Any]]:
    """Return a page of trades for the user's account."""
    return self._json_or_none(
      await self._request(
        "GET",
        self._path(self.ENDPOINTS.TRADES, telegram_id=telegram_user_id),
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
        self._path(self.ENDPOINTS.FLAT, telegram_id=telegram_user_id),
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
        self._path(self.ENDPOINTS.PREVENT, telegram_id=telegram_user_id),
        json={"enabled": enabled},
      )
    )

  async def unlink(self, telegram_user_id: int) -> bool:
    """Clear the user's account binding. Returns True on success."""
    resp = await self._request(
      "POST", self._path(self.ENDPOINTS.UNLINK, telegram_id=telegram_user_id)
    )
    return resp is not None and resp.status_code < 400


class BrokerClientAdmin(BrokerClient):
  """Admin endpoints, reusing the broker's existing management API.

  ``BASE_PATH`` covers the ``/admin/*`` endpoints. ``admin_list_accounts`` and
  ``admin_list_trades`` live under ``/v1/*`` instead (they reuse the broker's
  general account/trade endpoints, unscoped to a caller), so they address
  those paths directly rather than through ``BASE_PATH``/``ENDPOINTS``.
  """

  BASE_PATH = "/admin"

  class ENDPOINTS(Endpoint):
    ACCOUNTS = "accounts"
    FLAT = "flat"
    ROTATE_TOKEN = "accounts/{account_id}/link-token/rotate"
    SETTINGS = "settings"
    SETTINGS_TOGGLE = "settings/{slug}"

  async def admin_list_accounts(self) -> Optional[list[dict[str, Any]]]:
    """All trading accounts (includes link_token + linked_user_ids)."""
    return self._json_or_none(await self._request("GET", "/v1/accounts"))

  async def admin_create_account(
    self,
    account_id: str,
    market: str,
    gateway: str,
    account_name: Optional[str] = None,
  ) -> Optional[dict[str, Any]]:
    """Manually register a new account. None on failure (already exists,
    invalid market/gateway pair, or a transport error — the broker's error
    response is logged in ``_request``)."""
    return self._json_or_none(
      await self._request(
        "POST",
        self._path(self.ENDPOINTS.ACCOUNTS),
        json={
          "account_id": account_id,
          "market": market,
          "gateway": gateway,
          "account_name": account_name,
        },
      )
    )

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
    market: Optional[str] = None,
    gateway: Optional[str] = None,
  ) -> Optional[dict[str, Any]]:
    """Publish a FLAT directive (all fields None = flat everything).

    The broker requires market + gateway together with account_id (422
    otherwise) — account_id alone no longer identifies a single account.
    """
    return self._json_or_none(
      await self._request(
        "POST",
        self._path(self.ENDPOINTS.FLAT),
        json={
          "strategy": strategy,
          "symbol": symbol,
          "account_id": account_id,
          "market": market,
          "gateway": gateway,
        },
      )
    )

  async def admin_rotate_token(self, account_id: str) -> Optional[dict[str, Any]]:
    """Rotate an account's Telegram link token; returns the new token."""
    return self._json_or_none(
      await self._request(
        "POST", self._path(self.ENDPOINTS.ROTATE_TOKEN, account_id=account_id)
      )
    )

  async def admin_get_settings(self) -> Optional[list[dict[str, Any]]]:
    """Current state of the broker toggle settings."""
    return self._json_or_none(
      await self._request("GET", self._path(self.ENDPOINTS.SETTINGS))
    )

  async def admin_toggle_setting(self, slug: str) -> Optional[dict[str, Any]]:
    """Toggle a broker setting (slug: block-signal, silent-signal, include-signal-raw)."""
    return self._json_or_none(
      await self._request("POST", self._path(self.ENDPOINTS.SETTINGS_TOGGLE, slug=slug))
    )
