"""Unit tests for BrokerClient using httpx's built-in MockTransport (no network)."""

from __future__ import annotations

import httpx

from app.services.broker_client import BrokerClient


def _client(handler, api_prefix=""):
  return BrokerClient(
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
    return httpx.Response(200, json={"account_id": "acc-1", "telegram_user_id": 7})

  client = _client(handler)
  result = await client.link("11111111-1111-1111-1111-111111111111", 7)
  assert result == {"account_id": "acc-1", "telegram_user_id": 7}
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

  client = _client(handler)
  res = await client.admin_list_accounts()
  assert res[0]["account_id"] == "acc-1"
  assert captured["path"] == "/v1/accounts"
  await client.aclose()


async def test_admin_list_trades_path_and_params():
  captured = {}

  def handler(request: httpx.Request) -> httpx.Response:
    captured["path"] = request.url.path
    captured["params"] = dict(request.url.params)
    return httpx.Response(200, json={"data": [], "page": {}})

  client = _client(handler)
  await client.admin_list_trades("acc-1", limit=7, offset=14)
  assert captured["path"] == "/v1/acc-1/trades"
  assert captured["params"] == {"limit": "7", "offset": "14"}
  await client.aclose()


async def test_admin_flat_default_body():
  captured = {}

  def handler(request: httpx.Request) -> httpx.Response:
    captured["path"] = request.url.path
    captured["body"] = request.content.decode()
    return httpx.Response(200, json={"action": "FLAT", "scope": "ALL"})

  client = _client(handler)
  res = await client.admin_flat()
  assert res["scope"] == "ALL"
  assert captured["path"] == "/admin/flat"
  assert '"account_id":null' in captured["body"].replace(" ", "")
  await client.aclose()


async def test_admin_rotate_settings_toggle_paths():
  seen = []

  def handler(request: httpx.Request) -> httpx.Response:
    seen.append(request.url.path)
    return httpx.Response(200, json={"account_id": "acc-1", "telegram_link_token": "t"})

  client = _client(handler)
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

  client = _client(handler, api_prefix="sec")
  await client.admin_list_accounts()
  assert captured["path"] == "/sec/v1/accounts"
  await client.aclose()
