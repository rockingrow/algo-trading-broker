"""The 422 handler's JSON-syntax diagnostics.

TradingView sends an alert whose JSON *it* could not parse as ``text/plain``,
so FastAPI hands the raw body to the model and pydantic reports a generic
"Input should be a valid dictionary" — which never mentions the syntax error
that actually caused it. These cover the message that does.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from broker.app import (
  install_exception_handlers,
  json_syntax_error,
  redact_secrets,
)


# A Pine ``str.tostring`` that emitted a thousands separator — the real payload
# behind the 422s: {"bar_index": 16,222} is not valid JSON.
MALFORMED = (
  '{"strategy": "SIDEWAY_M15_V1", "sample": {"action": "HEARTBEAT", '
  '"bar_index": 16,222}, "token": "t"}'
)


def test_json_syntax_error_locates_the_offending_character():
  message = json_syntax_error([{"loc": ["body"], "input": MALFORMED}])

  assert message is not None
  assert "line 1 column" in message
  # The snippet is a window around the break, not the whole alert body.
  assert "16,222" in message
  assert len(message) < 200


def test_json_syntax_error_accepts_a_bytes_body():
  assert json_syntax_error([{"loc": ["body"], "input": MALFORMED.encode()}])


def test_the_webhook_token_never_reaches_the_message():
  raw = (
    '{"strategy": "S", "bar_index": 16,222, '
    '"token": "5a7a92a5-7a26-42c7-a981-fd20c0040acf"}'
  )
  message = json_syntax_error([{"loc": ["body"], "input": raw}])

  # token *is* WEBHOOK_SECRET — a rejected alert must not put it in the log.
  assert "5a7a92a5" not in message
  assert '"token": "***"' in message


def test_redact_secrets_scrubs_parsed_bodies_and_raw_text():
  parsed = redact_secrets({"input": {"strategy": "S", "token": "super-secret"}})
  assert parsed["input"]["token"] == "***"
  assert parsed["input"]["strategy"] == "S"

  # Also inside a raw body, including one the snippet window cut in half.
  assert "super-secret" not in redact_secrets('{"token": "super-secret"}')
  assert "super-secret" not in redact_secrets('{"token": "super-secret')
  assert redact_secrets(['{"token": "s"}'])[0] == '{"token": "***"}'


def test_json_syntax_error_is_none_for_ordinary_field_errors():
  # A well-formed body missing a required field must be reported as-is.
  assert (
    json_syntax_error([{"loc": ["body", "token"], "input": {"strategy": "s"}}]) is None
  )
  assert json_syntax_error([{"loc": ["body"], "input": '{"strategy": "s"}'}]) is None


class _Body(BaseModel):
  strategy: str


def _app() -> TestClient:
  app = FastAPI()
  install_exception_handlers(app)

  @app.post("/echo")
  async def echo(body: _Body):  # pragma: no cover — never reached on bad input
    return {"strategy": body.strategy}

  return TestClient(app)


def test_malformed_body_answers_422_with_the_json_error():
  resp = _app().post("/echo", content=MALFORMED, headers={"Content-Type": "text/plain"})

  assert resp.status_code == 422
  assert "Expecting" in resp.json()["json_error"]


def test_valid_body_is_unaffected():
  resp = _app().post("/echo", json={"strategy": "s"})
  assert resp.status_code == 200


def test_field_level_errors_still_hide_the_token():
  # Well-formed JSON, wrong shape: pydantic echoes the parsed body back as the
  # error's `input`, token and all, into both the log and the response.
  resp = _app().post("/echo", json={"sample": "x", "token": "super-secret"})

  assert resp.status_code == 422
  assert "super-secret" not in resp.text
  assert "json_error" not in resp.json()
