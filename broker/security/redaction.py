"""
broker/security/redaction.py — scrub the webhook secret out of anything logged.

The webhook's ``token`` *is* ``WEBHOOK_SECRET``, and every signal envelope on
JetStream carries it. Error paths that log a payload — a rejected request, a
malformed envelope — would otherwise write it to the log and, through the
ERROR forwarder, into a Telegram chat. One helper, used by the HTTP layer and
the JetStream consumer alike.
"""

from __future__ import annotations

import re

_REDACTED = "***"

# Keys whose value is a shared secret rather than data worth logging. The
# webhook's ``token`` *is* WEBHOOK_SECRET, and a rejected alert's body — the
# whole of it, before this — went straight into the 422 log line.
_SECRET_KEYS = frozenset({"token"})

# Same secret seen inside a raw (unparsed) body. The trailing alternative
# catches a value the snippet window cut in half.
_SECRET_IN_TEXT = re.compile(r'("token"\s*:\s*")(?:[^"]*"|[^"]*$)')


def redact_secrets(value):
  """Replace secret values anywhere in *value* — dict, list, or raw body text.

  A validation error carries the offending input back to the caller, so
  everything the webhook was sent (its ``token`` included) reaches the log and
  the response body unless it is scrubbed here first.
  """
  if isinstance(value, dict):
    return {
      key: (_REDACTED if key in _SECRET_KEYS else redact_secrets(item))
      for key, item in value.items()
    }
  if isinstance(value, list):
    return [redact_secrets(item) for item in value]
  if isinstance(value, str):
    return _SECRET_IN_TEXT.sub(rf'\1{_REDACTED}"', value)
  return value
