"""Unit tests for BrokerClientUser/BrokerClientAdmin using httpx's built-in
MockTransport (no network)."""

from __future__ import annotations

import httpx

from app.services.broker_client import BrokerClientAdmin, BrokerClientUser


def _client(handler, api_prefix=""):
  return BrokerClientUser(
    base_url="http://broker:8080",
    api_key="secret-key",
    api_prefix=api_prefix,
    transport=httpx.MockTransport(handler),
  )


def _admin_client(handler, api_prefix=""):
  return BrokerClientAdmin(
    base_url="http://broker:8080",
    api_key="secret-key",
    api_prefix=api_prefix,
    transport=httpx.MockTransport(handler),
  )


async def test_link_success_returns_account():
  captured = {}

  def handler(request: httpx.Request) -> httpx.Response:
    captured["path"] = request.url.path
    captured["method"] = request.method
    captured["api_key"] = request.headers.get("x-api-key")
    return httpx.Response(200, json={"account_id": "acc-1", "is_active": True})

  client = _client(handler)
  result = await client.link("11111111-1111-1111-1111-111111111111", 7)
  assert result == {"account_id": "acc-1", "is_active": True}
  assert captured["path"] == "/v1/telegram/link"
  assert captured["method"] == "POST"
  assert captured["api_key"] == "secret-key"
  await client.aclose()


async def test_link_invalid_token_returns_none():
  def handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, json={"detail": "Invalid link token"})

  client = _client(handler)
  assert await client.link("bad", 7) is None
  await client.aclose()


async def test_get_account_404_returns_none():
  def handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404)

  client = _client(handler)
  assert await client.get_account(7) is None
  await client.aclose()


async def test_list_trades_passes_pagination_params():
  captured = {}

  def handler(request: httpx.Request) -> httpx.Response:
    captured["path"] = request.url.path
    captured["params"] = dict(request.url.params)
    return httpx.Response(200, json={"data": [], "page": {"total": 0}})

  client = _client(handler)
  result = await client.list_trades(7, limit=5, offset=10)
  assert result["page"]["total"] == 0
  assert captured["path"] == "/v1/telegram/7/trades"
  assert captured["params"] == {"limit": "5", "offset": "10"}
  await client.aclose()


async def test_flat_and_prevent_post_expected_bodies():
  bodies = []

  def handler(request: httpx.Request) -> httpx.Response:
    bodies.append((request.url.path, request.content.decode()))
    return httpx.Response(200, json={"action": "X", "scope": "account=acc-1"})

  client = _client(handler)
  await client.flat(7)
  await client.prevent(7, enabled=True)
  await client.prevent(7, enabled=False)
  assert bodies[0][0] == "/v1/telegram/7/commands/flat"
  assert bodies[1][0] == "/v1/telegram/7/commands/prevent"
  assert '"enabled":true' in bodies[1][1].replace(" ", "")
  assert '"enabled":false' in bodies[2][1].replace(" ", "")
  await client.aclose()


async def test_list_accounts_returns_list():
  captured = {}

  def handler(request: httpx.Request) -> httpx.Response:
    captured["path"] = request.url.path
    return httpx.Response(
      200,
      json=[
        {"id": "a1", "account_id": "acc-1", "is_active": True},
        {"id": "a2", "account_id": "acc-2", "is_active": False},
      ],
    )

  client = _client(handler)
  result = await client.list_accounts(7)
  assert len(result) == 2
  assert captured["path"] == "/v1/telegram/7/accounts"
  await client.aclose()


async def test_switch_account_posts_account_id():
  captured = {}

  def handler(request: httpx.Request) -> httpx.Response:
    captured["path"] = request.url.path
    captured["body"] = request.content.decode()
    return httpx.Response(200, json={"account_id": "acc-2", "is_active": True})

  client = _client(handler)
  result = await client.switch_account(7, "a2")
  assert result["account_id"] == "acc-2"
  assert captured["path"] == "/v1/telegram/7/active-account"
  assert '"account_id":"a2"' in captured["body"].replace(" ", "")
  await client.aclose()


async def test_switch_account_not_owned_returns_none():
  def handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404)

  client = _client(handler)
  assert await client.switch_account(7, "bad-id") is None
  await client.aclose()


async def test_unlink_true_on_success_false_on_404():
  def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"status": "unlinked"})

  def missing(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404)

  c1 = _client(ok)
  assert await c1.unlink(7) is True
  await c1.aclose()

  c2 = _client(missing)
  assert await c2.unlink(7) is False
  await c2.aclose()


async def test_api_prefix_is_prepended():
  captured = {}

  def handler(request: httpx.Request) -> httpx.Response:
    captured["path"] = request.url.path
    return httpx.Response(200, json={"account_id": "acc-1"})

  client = _client(handler, api_prefix="abc123")
  await client.get_account(7)
  assert captured["path"] == "/abc123/v1/telegram/7"
  await client.aclose()


async def test_server_error_returns_none():
  def handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, text="boom")

  client = _client(handler)
  assert await client.get_account(7) is None
  await client.aclose()


# ── admin methods ───────────────────────────────────────────────────


async def test_admin_list_accounts():
  captured = {}

  def handler(request: httpx.Request) -> httpx.Response:
    captured["path"] = request.url.path
    return httpx.Response(200, json=[{"account_id": "acc-1"}])

  client = _admin_client(handler)
  res = await client.admin_list_accounts()
  assert res[0]["account_id"] == "acc-1"
  assert captured["path"] == "/v1/accounts"
  await client.aclose()


async def test_admin_create_account_success():
  captured = {}

  def handler(request: httpx.Request) -> httpx.Response:
    captured["path"] = request.url.path
    captured["method"] = request.method
    captured["json"] = request.read()
    return httpx.Response(
      201,
      json={
        "account_id": "7654321",
        "market": "CRYPTO",
        "gateway": "BINANCE",
        "link_token": "b5dc0374-9639-4861-acf4-2d239aa5c1b4",
        "linked_user_ids": [],
      },
    )

  client = _admin_client(handler)
  result = await client.admin_create_account("7654321", "CRYPTO", "BINANCE")
  assert result["account_id"] == "7654321"
  assert captured["path"] == "/admin/accounts"
  assert captured["method"] == "POST"
  await client.aclose()


async def test_admin_create_account_conflict_returns_none():
  def handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(409, json={"detail": "account_id already exists"})

  client = _admin_client(handler)
  result = await client.admin_create_account("7654321", "CRYPTO", "BINANCE")
  assert result is None
  await client.aclose()


async def test_admin_list_trades_path_and_params():
  captured = {}

  def handler(request: httpx.Request) -> httpx.Response:
    captured["path"] = request.url.path
    captured["params"] = dict(request.url.params)
    return httpx.Response(200, json={"data": [], "page": {}})

  client = _admin_client(handler)
  await client.admin_list_trades("acc-1", limit=7, offset=14)
  assert captured["path"] == "/v1/acc-1/trades"
  assert captured["params"] == {"limit": "7", "offset": "14"}
  await client.aclose()


async def test_get_notification_timezone_path():
  captured = {}

  def handler(request: httpx.Request) -> httpx.Response:
    captured["path"] = request.url.path
    return httpx.Response(200, json={"setting": "notification_timezone", "value": "9"})

  client = _admin_client(handler)
  result = await client.get_notification_timezone()
  assert result == {"setting": "notification_timezone", "value": "9"}
  assert captured["path"] == "/admin/settings/notification-timezone"
  await client.aclose()


async def test_admin_flat_default_body():
  captured = {}

  def handler(request: httpx.Request) -> httpx.Response:
    captured["path"] = request.url.path
    captured["body"] = request.content.decode()
    return httpx.Response(200, json={"action": "FLAT", "scope": "ALL"})

  client = _admin_client(handler)
  res = await client.admin_flat()
  assert res["scope"] == "ALL"
  assert captured["path"] == "/admin/flat"
  assert '"account_id":null' in captured["body"].replace(" ", "")
  await client.aclose()


async def test_admin_flat_scoped_body_includes_market_and_gateway():
  captured = {}

  def handler(request: httpx.Request) -> httpx.Response:
    captured["body"] = request.content.decode()
    return httpx.Response(200, json={"action": "FLAT", "scope": "account=acc-1"})

  client = _admin_client(handler)
  await client.admin_flat(account_id="acc-1", market="CRYPTO", gateway="BINANCE")
  body = captured["body"].replace(" ", "")
  assert '"account_id":"acc-1"' in body
  assert '"market":"CRYPTO"' in body
  assert '"gateway":"BINANCE"' in body
  await client.aclose()


async def test_broadcast_subscribe_unsubscribe_status_paths():
  seen = []

  def handler(request: httpx.Request) -> httpx.Response:
    seen.append((request.method, request.url.path))
    return httpx.Response(200, json={"subscribed": True})

  client = _client(handler)
  await client.subscribe_broadcast(555)
  await client.unsubscribe_broadcast(555)
  await client.get_broadcast_subscription(555)
  assert seen == [
    ("POST", "/v1/telegram/555/broadcast/subscribe"),
    ("POST", "/v1/telegram/555/broadcast/unsubscribe"),
    ("GET", "/v1/telegram/555/broadcast"),
  ]
  await client.aclose()


async def test_admin_link_telegram_path_and_body():
  captured = {}

  def handler(request: httpx.Request) -> httpx.Response:
    captured["path"] = request.url.path
    captured["body"] = request.content.decode()
    return httpx.Response(200, json={"account_id": "acc-1", "linked_user_ids": ["999"]})

  client = _admin_client(handler)
  res = await client.admin_link_telegram("3fa85f64-5717-4562-b3fc-2c963f66afa6", 999)
  assert res["linked_user_ids"] == ["999"]
  assert captured["path"] == (
    "/admin/accounts/3fa85f64-5717-4562-b3fc-2c963f66afa6/link-telegram"
  )
  assert '"telegram_user_id":999' in captured["body"].replace(" ", "")
  await client.aclose()


async def test_admin_rotate_settings_toggle_paths():
  seen = []

  def handler(request: httpx.Request) -> httpx.Response:
    seen.append(request.url.path)
    return httpx.Response(200, json={"account_id": "acc-1", "link_token": "t"})

  client = _admin_client(handler)
  await client.admin_rotate_token("acc-1")
  await client.admin_get_settings()
  await client.admin_toggle_setting("block-signal")
  assert seen == [
    "/admin/accounts/acc-1/link-token/rotate",
    "/admin/settings",
    "/admin/settings/block-signal",
  ]
  await client.aclose()


async def test_admin_prefix_applied():
  captured = {}

  def handler(request: httpx.Request) -> httpx.Response:
    captured["path"] = request.url.path
    return httpx.Response(200, json=[])

  client = _admin_client(handler, api_prefix="sec")
  await client.admin_list_accounts()
  assert captured["path"] == "/sec/v1/accounts"
  await client.aclose()
