"""
broker/openapi.py — OpenAPI / Swagger documentation metadata.

Centralises everything that shapes the generated docs (tag descriptions,
contact/license, server list) so ``create_app`` stays a thin composition root.
"""

from __future__ import annotations

from typing import Any, Dict, List

from broker.settings import settings

API_TITLE = "Algo Trading Broker"
API_VERSION = "1.0.0"

API_DESCRIPTION = """
Central broker node for the algo-trading system.

* Receives **TradingView** JSON webhook alerts.
* Persists every signal to **PostgreSQL**.
* Fans them out over **NATS** to subscriber VPS nodes.

### Authentication
Protected endpoints require the `X-API-KEY` header. Click **Authorize** 🔓
above and paste the broker API key to try them out from this page.
""".strip()


# Tag order here controls the section order shown in Swagger UI.
OPENAPI_TAGS: List[Dict[str, Any]] = [
  {
    "name": "webhook",
    "description": "Ingest TradingView alerts and fan them out via NATS.",
  },
  {
    "name": "accounts",
    "description": "Read registered trading accounts. **Requires `X-API-KEY`.**",
  },
  {
    "name": "trades",
    "description": "List trade history for a trading account. **Requires `X-API-KEY`.**",
  },
  {
    "name": "settings",
    "description": "Runtime broker toggles (e.g. block signals). **Requires `X-API-KEY`.**",
  },
  {
    "name": "telegram",
    "description": (
      "Endpoints consumed by the Telegram bot service: link/unlink users, list "
      "their trades, and issue FLAT/PREVENT commands. **Requires `X-API-KEY`.**"
    ),
  },
  {
    "name": "system",
    "description": "Health and liveness probes. Public, no auth required.",
  },
]


# Reusable OpenAPI ``responses`` block for endpoints guarded by ``ensure_api_key``.
AUTH_RESPONSES: Dict[int, Dict[str, Any]] = {
  401: {"description": "Missing or invalid `X-API-KEY` header."},
}


def _servers() -> List[Dict[str, str]]:
  """Advertise the reachable base URLs so 'Try it out' targets the right host."""
  servers: List[Dict[str, str]] = []
  if settings.BROKER_PUBLIC_URL.startswith(("http://", "https://")):
    servers.append({"url": settings.BROKER_PUBLIC_URL, "description": "Public"})
  servers.append({"url": "/", "description": "This host"})
  return servers


def fastapi_kwargs() -> Dict[str, Any]:
  """Keyword arguments passed straight into ``FastAPI(...)``.

  When ``DOCS_ENABLED`` is false the docs/openapi routes are disabled
  entirely, hiding the schema in production.
  """
  contact: Dict[str, str] = {"name": "Algo Trading Broker"}
  if settings.broker_url.startswith(("http://", "https://")):
    contact["url"] = settings.broker_url

  kwargs: Dict[str, Any] = {
    "title": API_TITLE,
    "version": API_VERSION,
    "description": API_DESCRIPTION,
    "openapi_tags": OPENAPI_TAGS,
    "contact": contact,
    "license_info": {"name": "Proprietary"},
    "servers": _servers(),
  }

  if not settings.DOCS_ENABLED:
    kwargs.update(docs_url=None, redoc_url=None, openapi_url=None)

  return kwargs
