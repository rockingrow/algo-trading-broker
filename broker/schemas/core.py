from enum import Enum


class SignalActionEnum(str, Enum):
  """All possible trade actions carried by a signal: entries, partial-close targets, stop-loss, and full flatten."""

  LONG = "LONG"
  SHORT = "SHORT"
  TP1 = "TP1"
  TP2 = "TP2"
  R_SL = "R_SL"
  SL = "SL"
  FLAT = "FLAT"


class MarketEnum(str, Enum):
  CRYPTO = "CRYPTO"
  FOREX = "FOREX"


class ForexGatewayEnum(str, Enum):
  MT5 = "MT5"


class CryptoGatewayEnum(str, Enum):
  BINANCE = "BINANCE"


class BotPlatformTypeEnum(str, Enum):
  """Chat platform a bot user identity belongs to.

  Only Telegram exists today, but the account<->bot-user tables are keyed by
  ``(platform, platform_user_id)`` so adding Discord/Slack later is a new enum
  member rather than a schema migration.
  """

  TELEGRAM = "TELEGRAM"


class BroadcastAudienceEnum(str, Enum):
  """Who a signal-cycle broadcast message is aimed at.

  ``PRIVATE`` — operator-facing channel, sourced from the
  ``TELEGRAM_PRIVATE_BROADCAST_CHAT_IDS`` env var (a deployment concern). Gets
  the strategy name, signal id and worker execution table on top of the base
  body.
  ``PUBLIC`` — subscriber-facing channel, sourced from the
  ``public_broadcast_chat_ids`` broker setting (edited at runtime from the
  admin API or the bot's ``/admin_public_chats`` command). Gets only the bare
  price/level/timeline body.

  Stored per chat row rather than per message so the two audiences can be
  rendered differently later without touching the schema.
  """

  PRIVATE = "PRIVATE"
  PUBLIC = "PUBLIC"


class BroadcastStatusEnum(str, Enum):
  """Lifecycle of one broadcast signal cycle.

  ``RUNNING`` — an entry (LONG/SHORT) opened the cycle and it is still live;
  a TP1 keeps it running because part of the position remains open.

  ``CLOSED`` — a terminal action (TP2, SL, R_SL, FLAT) ended it. The message
  keeps its history but stops advertising an open position.
  """

  RUNNING = "RUNNING"
  CLOSED = "CLOSED"


class BroadcastLogKindEnum(str, Enum):
  """What changed on a cycle, as recorded in the broadcast write log.

  ``SIGNAL`` — a new TradingView action joined the cycle (entry, TP, SL, FLAT).
  ``EXECUTION`` — a worker reported a trade for the cycle (a TRADE event), so
  the worker/status table inside the message needs refreshing.
  """

  SIGNAL = "SIGNAL"
  EXECUTION = "EXECUTION"


class BroadcastLogStatusEnum(str, Enum):
  """Delivery state of one write-log entry.

  ``PENDING`` — appended, not yet pushed to Telegram.
  ``SENDING`` — claimed by a dispatcher; another dispatcher must leave it be.
  ``DELIVERED`` — the chats now show a body that includes this change.
  ``FAILED`` — every delivery attempt was exhausted; the sweeper stops
  re-picking it, and the next change to the cycle carries its content anyway
  because the body is always re-rendered in full.
  """

  PENDING = "PENDING"
  SENDING = "SENDING"
  DELIVERED = "DELIVERED"
  FAILED = "FAILED"


class SignalStatusEnum(str, Enum):
  """Delivery state of a persisted webhook signal.

  ``QUEUED`` — the signal was written to the DB but the background handler
  has not yet successfully fanned it out to workers. May still be retried by
  the retry job as long as ``attempts > 0``.

  ``PUBLISHED`` — the handler successfully published the signal to the
  strategy subject (and ran the notification pipeline).

  ``FAILED`` — every attempt exhausted (``attempts`` decremented to ``0``)
  without a successful publish. Terminal — the retry job stops re-picking it.
  """

  QUEUED = "QUEUED"
  PUBLISHED = "PUBLISHED"
  FAILED = "FAILED"
