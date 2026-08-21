"""
broker/db/models.py
────────────────────
SQLAlchemy ORM models for the audit table.

signals   — every TradingView webhook signal received by the broker
"""

from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import (
  BigInteger,
  Boolean,
  DateTime,
  Enum,
  ForeignKey,
  Numeric,
  String,
  Text,
  func,
  Integer,
  UniqueConstraint,
  text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from broker.schemas.account_schema import MarketTypeEnum
from broker.schemas.core import (
  BotPlatformTypeEnum,
  BroadcastAudienceEnum,
  BroadcastLogKindEnum,
  BroadcastLogStatusEnum,
  BroadcastStatusEnum,
  SignalActionEnum,
  SignalStatusEnum,
)
from broker.schemas.trade_schema import TradeStatusEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
  """Abstract base for all ORM models, providing UUID primary key and auto-managed timestamps."""

  id: Mapped[uuid.UUID] = mapped_column(
    UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
  )
  createdAt: Mapped[datetime] = mapped_column(
    DateTime(timezone=True),
    nullable=False,
    server_default=func.now(),
  )
  updatedAt: Mapped[datetime] = mapped_column(
    DateTime(timezone=True),
    nullable=False,
    server_default=func.now(),
    onupdate=func.now(),
  )


class Signal(Base):
  """
  One row per incoming TradingView webhook signal.
  Logs components of the WebhookPayload.
  """

  __tablename__ = "signals"

  # WebhookPayload columns
  strategy: Mapped[str] = mapped_column(String(50), nullable=False)
  symbol: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
  timeframe: Mapped[str] = mapped_column(String(20), nullable=False)
  timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
  # Cycle id shared by every alert of one trade (see ``BroadcastMessage``).
  # Nullable because rows written before the column existed have none.
  signal_uxid: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)

  # PositionSchema columns
  action: Mapped[SignalActionEnum] = mapped_column(
    Enum(SignalActionEnum), nullable=False
  )
  price: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
  quantity: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
  sl: Mapped[float | None] = mapped_column(Numeric(20, 8), nullable=True)
  tp1: Mapped[float | None] = mapped_column(Numeric(20, 8), nullable=True)
  tp2: Mapped[float | None] = mapped_column(Numeric(20, 8), nullable=True)
  is_running: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
  risk_percent: Mapped[float] = mapped_column(
    Numeric(10, 4), nullable=False, default=0.0
  )
  is_scale_position: Mapped[bool] = mapped_column(
    Boolean, nullable=False, default=False
  )
  scale_strategy: Mapped[str | None] = mapped_column(String(50), nullable=True)

  # Delivery state: QUEUED once persisted, PUBLISHED after the JetStream handler
  # has fanned it out to workers and finished the notification pipeline, or
  # FAILED once every retry attempt has been exhausted. Rows stay QUEUED
  # between attempts so the 15s retry job can re-pick them.
  status: Mapped[SignalStatusEnum] = mapped_column(
    Enum(SignalStatusEnum),
    nullable=False,
    default=SignalStatusEnum.QUEUED,
    server_default=SignalStatusEnum.QUEUED.value,
    index=True,
  )
  # Remaining fan-out attempts. Seeded from ``settings.signal.MAX_ATTEMPTS``
  # on insert and decremented on every failed attempt; when it would drop to
  # ``0`` the row is flipped to ``FAILED`` instead.
  attempts: Mapped[int] = mapped_column(
    Integer,
    nullable=False,
    default=0,
    server_default="0",
  )
  # Timestamp of the last attempt (nullable — a QUEUED row that has never been
  # attempted yet has ``NULL`` here). Used by the retry job to enforce the
  # minimum gap between two attempts on the same row so a poll tick cannot
  # race an in-flight attempt.
  last_attempt: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
  )

  # Complex objects stored as JSONB
  indicators: Mapped[dict] = mapped_column(JSONB, nullable=True)
  inputs: Mapped[dict] = mapped_column(JSONB, nullable=True)
  raw: Mapped[dict] = mapped_column(JSONB, nullable=True)

  def __repr__(self) -> str:
    return (
      f"<Signal id={self.id} symbol={self.symbol} "
      f"action={self.action} timestamp={self.timestamp}>"
    )


class Trade(Base):
  """
  One row per trade opened by the broker.
  """

  __tablename__ = "trades"
  __table_args__ = (
    UniqueConstraint(
      "market",
      "gateway",
      "account_id",
      "ref_id",
      name="uq_trades_market_gateway_account_ref",
    ),
  )

  # Trading Account info
  account_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
  # Denormalized from the owning accounts row at upsert time (see
  # TradeRepository._upsert_account) — account_id alone doesn't identify an
  # account uniquely, so these two are needed to scope trades to the right one.
  market: Mapped[MarketTypeEnum | None] = mapped_column(
    Enum(MarketTypeEnum), nullable=True
  )
  gateway: Mapped[str | None] = mapped_column(String(50), nullable=True)
  account_leverage: Mapped[int | None] = mapped_column(Integer, nullable=True)
  account_balance_init: Mapped[float] = mapped_column(Numeric(20, 8), nullable=True)
  account_balance: Mapped[float] = mapped_column(Numeric(20, 8), nullable=True)

  # Strategy
  strategy: Mapped[str] = mapped_column(String(50), nullable=False)
  strategy_code: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

  # Trade
  ref_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

  # Trade details
  symbol: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
  action: Mapped[SignalActionEnum] = mapped_column(
    Enum(SignalActionEnum), nullable=False
  )
  price: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
  quantity: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
  sl: Mapped[float | None] = mapped_column(Numeric(20, 8), nullable=True)
  tp1: Mapped[float | None] = mapped_column(Numeric(20, 8), nullable=True)
  tp2: Mapped[float | None] = mapped_column(Numeric(20, 8), nullable=True)
  is_running: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
  risk_percent: Mapped[float] = mapped_column(
    Numeric(10, 4), nullable=False, default=0.0
  )
  comment: Mapped[str | None] = mapped_column(String(255), nullable=True)
  gateway_return_code: Mapped[int | None] = mapped_column(Integer, nullable=True)

  # Status
  status: Mapped[TradeStatusEnum] = mapped_column(Enum(TradeStatusEnum), nullable=False)
  # The event that last moved this trade — TP1 / TP2 / SL / R_SL / FLAT /
  # TERMINAL_CLOSED / FORCED_CLOSED (see ``TradeStatusPolicy.to_last_action``).
  # Several of those map onto the same ``status``, and ``action`` keeps the
  # entry direction, so the row alone otherwise never says *how* a trade ended.
  # Persisted rather than passed along with the event because the live trade
  # card is re-rendered later — by the bot, on a Detail tap — with only the row
  # to go on. Nullable: rows written before the column existed have none.
  last_action: Mapped[str | None] = mapped_column(String(20), nullable=True)
  reject_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

  def __repr__(self) -> str:
    return (
      f"<Trade id={self.id} account_id={self.account_id} gateway={self.gateway} "
      f"ref_id={self.ref_id} action={self.action} symbol={self.symbol}>"
    )


class Account(Base):
  """
  One row per account managed by the broker.
  """

  __tablename__ = "accounts"
  # account_id alone is NOT unique: two different real accounts on different
  # gateways (e.g. an MT5 login and a Binance account) can coincidentally
  # share the same bare id. The composite key is what's actually unique.
  __table_args__ = (
    UniqueConstraint(
      "market", "gateway", "account_id", name="uq_accounts_market_gateway_account_id"
    ),
  )

  # Trading Account info
  account_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
  account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
  account_balance: Mapped[float] = mapped_column(Numeric(20, 8), nullable=True)
  market: Mapped[MarketTypeEnum] = mapped_column(Enum(MarketTypeEnum), nullable=False)
  # Exchange/gateway the account trades through, e.g. MT5 (forex) or BINANCE
  # (crypto). Combined with market + account_id it forms the worker
  # addressing id <market>-<gateway>-<account_id> used on the SYSTEM subject.
  # Nullable so rows predating this column (or workers that don't report it)
  # remain valid.
  gateway: Mapped[str | None] = mapped_column(String(50), nullable=True)

  last_activity_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
  )

  # Per-account settings the owner toggles with a bot command (today only
  # ``signal_blocked``, from /prevent and /allow — see
  # :class:`broker.schemas.account_schema.AccountSettings`). Sent to the worker
  # in the ``settings`` block of its WORKER_CONNECTED_ACK, so a worker that
  # (re)connects picks up what was set while it was offline instead of coming
  # up with defaults.
  #
  # JSONB rather than one boolean column per command: the set of commands grows
  # and each new toggle would otherwise cost a migration, while the whole blob
  # travels to the worker as a single object anyway. It also beats TEXT holding
  # JSON (as ``broker_settings.value`` does) because Postgres can then merge a
  # single key server-side — ``settings || '{"k": v}'`` in
  # ``AccountRepository.update_settings`` — instead of the read-modify-write
  # that loses a concurrent command's update, and the column stays queryable
  # (``WHERE settings @> '{"signal_blocked": true}'``, GIN-indexable) if a
  # future admin view needs it.
  #
  # NOT NULL with a ``{}`` default so readers never have to distinguish "no
  # settings" from NULL; an account that has never run a command has ``{}``.
  settings: Mapped[dict] = mapped_column(
    JSONB,
    nullable=False,
    default=dict,
    server_default=text("'{}'::jsonb"),
  )

  # No bot/chat-platform columns live here on purpose: an account is a trading
  # domain object and must not know about Telegram. Who may drive it from a
  # bot lives in ``AccountBotLink``, the invite secrets in
  # ``AccountLinkToken``, and the per-user active selection in ``BotSession``.

  def __repr__(self) -> str:
    return (
      f"<Account id={self.id} account_id={self.account_id} "
      f"market={self.market} gateway={self.gateway}>"
    )


class AccountBotLink(Base):
  """
  Many-to-many join between trading accounts and chat-platform users.

  One account may be driven by several bot users (e.g. an owner plus an
  assistant), and one bot user may hold several accounts (different
  market/gateway pairs) — neither direction is expressible as a column on
  ``accounts``, which is why this table exists.

  There is deliberately no role/permission column yet: every linked user has
  the same rights. Add one here when a real read-only use case shows up.

  ``platform_user_id`` is a *string*, not an integer, even though Telegram and
  Discord ids are 64-bit numbers — Slack/Matrix ids are opaque strings, so
  storing text avoids a second migration later. The API layer still speaks
  ``telegram_user_id: int``; the repository is the single place that casts.
  """

  __tablename__ = "account_bot_links"
  __table_args__ = (
    UniqueConstraint(
      "platform",
      "platform_user_id",
      "account_id",
      name="uq_account_bot_links_platform_user_account",
    ),
  )

  account_id: Mapped[uuid.UUID] = mapped_column(
    UUID(as_uuid=True),
    ForeignKey("accounts.id", ondelete="CASCADE"),
    nullable=False,
    index=True,
  )
  platform: Mapped[BotPlatformTypeEnum] = mapped_column(
    Enum(BotPlatformTypeEnum), nullable=False
  )
  platform_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

  def __repr__(self) -> str:
    return (
      f"<AccountBotLink account_id={self.account_id} "
      f"platform={self.platform} platform_user_id={self.platform_user_id}>"
    )


class AccountLinkToken(Base):
  """
  Invite secrets that let a bot user claim an account.

  Split out of ``accounts`` so an account can have several outstanding tokens
  (invite two people with two separately revocable secrets) and so revocation
  is a state change rather than an overwrite.

  A token is *valid* when ``revoked_at IS NULL`` and it has not expired.
  ``expires_at`` is NULL by default, meaning "never expires" — nothing issues
  a deadline today; the column exists so time-boxed invites need no migration.
  ``last_used_at`` is audit only and never affects validity: a token stays
  reusable after a successful link, matching the pre-existing behaviour.
  """

  __tablename__ = "account_link_tokens"

  account_id: Mapped[uuid.UUID] = mapped_column(
    UUID(as_uuid=True),
    ForeignKey("accounts.id", ondelete="CASCADE"),
    nullable=False,
    index=True,
  )
  token: Mapped[uuid.UUID] = mapped_column(
    UUID(as_uuid=True), nullable=False, unique=True, index=True, default=uuid.uuid4
  )
  expires_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
  )
  revoked_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
  )
  last_used_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
  )

  def __repr__(self) -> str:
    return (
      f"<AccountLinkToken account_id={self.account_id} "
      f"revoked_at={self.revoked_at} expires_at={self.expires_at}>"
    )


class BotSession(Base):
  """
  One row per (platform, bot user), tracking which of their (possibly several)
  linked accounts is currently "active" — the one single-account commands
  (/status, /trades, /flat, ...) act on. Kept separate from
  ``AccountBotLink`` because "may drive" and "is currently driving" are
  different facts: a user has N links but exactly one active selection.
  """

  __tablename__ = "bot_sessions"
  __table_args__ = (
    UniqueConstraint(
      "platform", "platform_user_id", name="uq_bot_sessions_platform_user"
    ),
  )

  platform: Mapped[BotPlatformTypeEnum] = mapped_column(
    Enum(BotPlatformTypeEnum), nullable=False
  )
  platform_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
  active_account_id: Mapped[uuid.UUID | None] = mapped_column(
    UUID(as_uuid=True),
    ForeignKey("accounts.id", ondelete="SET NULL"),
    nullable=True,
  )

  def __repr__(self) -> str:
    return (
      f"<BotSession platform={self.platform} "
      f"platform_user_id={self.platform_user_id} "
      f"active_account_id={self.active_account_id}>"
    )


class TradeBroadcastSubscription(Base):
  """
  One row per (platform, bot user) who has opted in to receive the live trade
  card — a Telegram DM posted when one of their linked accounts opens a trade
  and edited in place as that trade progresses.

  Kept as its own table — rather than a column on ``bot_sessions`` or
  ``account_bot_links`` — because the opt-in is a per-user preference that
  spans every account the user holds, independent of which one is active or of
  any single link row. A user is "subscribed" when a row exists here for their
  ``(platform, platform_user_id)``; unsubscribing deletes it.
  """

  __tablename__ = "trade_broadcast_subscriptions"
  __table_args__ = (
    UniqueConstraint(
      "platform",
      "platform_user_id",
      name="uq_trade_broadcast_subscriptions_platform_user",
    ),
  )

  platform: Mapped[BotPlatformTypeEnum] = mapped_column(
    Enum(BotPlatformTypeEnum), nullable=False
  )
  platform_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

  def __repr__(self) -> str:
    return (
      f"<TradeBroadcastSubscription platform={self.platform} "
      f"platform_user_id={self.platform_user_id}>"
    )


class BroadcastMessage(Base):
  """
  One row per **signal cycle** broadcast to Telegram — not per signal.

  A cycle is everything one trade emits: the LONG/SHORT entry, its TP1/TP2,
  its SL/R_SL, a FLAT. All of them carry the same ``signal_uxid`` in the
  webhook payload, so the pair ``(strategy, signal_uxid)`` identifies the
  cycle and is the unique key. The first signal of a cycle inserts this row and
  posts one Telegram message per broadcast chat; every later signal finds this
  row and *edits* those messages instead of posting new ones, which is the
  whole point — a channel shows one live message per trade rather than five.

  The full history lives in ``events`` (JSONB, append-only) because the
  message body is re-rendered from scratch on every update; ``actions``,
  ``latest_action`` and ``status`` denormalise it for cheap querying and for
  reading a cycle's state at a glance in SQL.
  """

  __tablename__ = "broadcast_messages"
  __table_args__ = (
    UniqueConstraint(
      "strategy",
      "signal_uxid",
      name="uq_broadcast_messages_strategy_signal_uxid",
    ),
  )

  strategy: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
  signal_uxid: Mapped[str] = mapped_column(String(16), nullable=False, index=True)

  symbol: Mapped[str] = mapped_column(String(50), nullable=False)
  timeframe: Mapped[str | None] = mapped_column(String(20), nullable=True)

  # Comma-separated action trail in arrival order, e.g. "LONG,TP1,SL". Kept as
  # text (not an array) so it reads the same in psql, a CSV export and a log
  # line; ``events`` is the structured source of truth.
  actions: Mapped[str] = mapped_column(Text, nullable=False, default="")
  latest_action: Mapped[SignalActionEnum] = mapped_column(
    Enum(SignalActionEnum), nullable=False
  )
  status: Mapped[BroadcastStatusEnum] = mapped_column(
    Enum(BroadcastStatusEnum),
    nullable=False,
    default=BroadcastStatusEnum.RUNNING,
    server_default=BroadcastStatusEnum.RUNNING.value,
    index=True,
  )

  # Every signal of the cycle, oldest first: action, prices, levels, the
  # payload timestamp and (when the fan-out was retried) the attempt number.
  # Re-rendering from this is what lets a later edit show the whole timeline.
  events: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

  # Sequence counter of the cycle, bumped once per appended write-log entry.
  # It is the cycle's version: every ``broadcast_message_logs`` row carries the
  # value it produced, and each chat records the highest one it has rendered
  # (``BroadcastMessageChat.delivered_seq``), which is what stops a slow
  # delivery from overwriting a newer body with an older one.
  last_seq: Mapped[int] = mapped_column(
    BigInteger, nullable=False, default=0, server_default="0"
  )

  # When the cycle was last pushed to Telegram (sent or edited), regardless of
  # whether any individual chat send succeeded.
  last_broadcast_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
  )

  def __repr__(self) -> str:
    return (
      f"<BroadcastMessage id={self.id} strategy={self.strategy} "
      f"signal_uxid={self.signal_uxid} latest_action={self.latest_action} "
      f"status={self.status} last_seq={self.last_seq}>"
    )


class BroadcastMessageChat(Base):
  """
  One row per (cycle, chat): the Telegram message a single chat holds.

  A cycle fans out to several chats — the private channel(s) and the public
  one(s) — and each chat gets its **own** ``message_id``, because editing a
  message is per-chat. ``message`` keeps the exact body last delivered to that
  chat: today private and public render identically, and storing the text per
  chat is what makes it cheap to diverge them later (a trimmed public body,
  say) without a schema change.

  ``message_id`` is NULL when the first send failed; the next signal of the
  cycle retries it as a fresh send rather than an edit.

  ``notified_event_count`` is the other half of the update story: editing a
  message notifies nobody, so every new event also gets a short reply under
  that chat's message, and this counts how many of them have been announced.
  """

  __tablename__ = "broadcast_message_chats"
  __table_args__ = (
    UniqueConstraint(
      "broadcast_message_id",
      "chat_id",
      name="uq_broadcast_message_chats_message_chat",
    ),
  )

  broadcast_message_id: Mapped[uuid.UUID] = mapped_column(
    UUID(as_uuid=True),
    ForeignKey("broadcast_messages.id", ondelete="CASCADE"),
    nullable=False,
    index=True,
  )
  audience: Mapped[BroadcastAudienceEnum] = mapped_column(
    Enum(BroadcastAudienceEnum), nullable=False
  )
  # Telegram chat ids are 64-bit ints, but stored as text so a future platform
  # with opaque ids needs no migration (same reasoning as AccountBotLink).
  chat_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
  message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
  message: Mapped[str | None] = mapped_column(Text, nullable=True)
  # Highest ``BroadcastMessage.last_seq`` this chat has been shown. A delivery
  # carrying an older sequence is dropped rather than sent, so two dispatchers
  # (or a slow retry racing a fresh change) can never replace a newer body with
  # a stale one.
  delivered_seq: Mapped[int] = mapped_column(
    BigInteger, nullable=False, default=0, server_default="0"
  )
  # How many of the cycle's events this chat has already been *told about* with
  # a reply notice under its message. The message itself is edited in place, so
  # a reader who saw it earlier learns nothing from a silent rewrite; every new
  # event therefore also gets a short two-line reply pointing at the same
  # message. NULL means the row predates the notices — it is backfilled to the
  # current event count on the next delivery so an in-flight cycle does not
  # suddenly announce its whole history at once.
  notified_event_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
  # Last delivery error for this chat (e.g. "bot was kicked"), for debugging a
  # channel that silently stopped updating. Cleared on the next success.
  last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)

  def __repr__(self) -> str:
    return (
      f"<BroadcastMessageChat broadcast_message_id={self.broadcast_message_id} "
      f"audience={self.audience} chat_id={self.chat_id} "
      f"message_id={self.message_id} delivered_seq={self.delivered_seq}>"
    )


class BroadcastMessageWorker(Base):
  """
  One row per worker that acted on a cycle, with the status it last reported.

  Fed from the NATS ``TRADE`` events a worker publishes: the event carries the
  ``signal_id`` the broker handed out, which resolves to the signal's
  ``signal_uxid`` and therefore to its cycle. The public broadcast message
  renders these rows as a worker/status table that keeps updating as the trade
  runs, so a reader sees not just the signal but who actually executed it and
  where each of them stands.

  Rows are upserted, never appended: the table shows *current* state per
  worker, and the full history of a trade already lives in ``trades``.
  """

  __tablename__ = "broadcast_message_workers"
  __table_args__ = (
    UniqueConstraint(
      "broadcast_message_id",
      "worker_id",
      name="uq_broadcast_message_workers_message_worker",
    ),
  )

  broadcast_message_id: Mapped[uuid.UUID] = mapped_column(
    UUID(as_uuid=True),
    ForeignKey("broadcast_messages.id", ondelete="CASCADE"),
    nullable=False,
    index=True,
  )
  # ``<market>-<gateway>-<account_id>`` (see ``compose_worker_id``) — the same
  # addressing id the SYSTEM subject uses, so a row is traceable back to one
  # worker. The parts are kept alongside it for display and querying.
  worker_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
  account_id: Mapped[str] = mapped_column(String(50), nullable=False)
  market: Mapped[MarketTypeEnum | None] = mapped_column(
    Enum(MarketTypeEnum), nullable=True
  )
  gateway: Mapped[str | None] = mapped_column(String(50), nullable=True)

  # Broker-side trade status (OPENED / PARTIALLY_CLOSED / CLOSED / REJECTED /
  # FLAT) mapped from the worker's position status, plus the raw worker action
  # for context. ``latest_status`` is what the public table shows.
  latest_status: Mapped[TradeStatusEnum] = mapped_column(
    Enum(TradeStatusEnum), nullable=False
  )
  latest_action: Mapped[str | None] = mapped_column(String(20), nullable=True)
  reject_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
  last_event_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
  )

  def __repr__(self) -> str:
    return (
      f"<BroadcastMessageWorker broadcast_message_id={self.broadcast_message_id} "
      f"worker_id={self.worker_id} latest_status={self.latest_status}>"
    )


class BroadcastMessageLog(Base):
  """
  Append-only write log of everything that changed a cycle.

  Nothing sends to Telegram at write time any more. A signal or a worker
  execution updates the cycle **and appends a row here in the same
  transaction**; a Postgres trigger then fires ``pg_notify`` and the
  dispatcher (``BroadcastDispatcher``) picks the change up and edits the
  Telegram messages. Three properties come out of that:

  * **Sequential.** ``seq`` is the cycle's monotonically increasing version
    (``BroadcastMessage.last_seq``), assigned under a row lock on the cycle, so
    concurrent writers queue instead of interleaving.
  * **Lossless.** The change is durable before any Telegram call is attempted.
    A crash, a Telegram outage or a missed notification cannot lose it — the
    row stays ``PENDING`` and the dispatcher's sweeper re-picks it.
  * **No overwrite.** Each chat records the sequence it has rendered, so a late
    delivery cannot replace a newer body with an older one.

  ``payload`` keeps what the change was (the signal event, the worker report)
  for audit; the message body itself is always re-rendered from the cycle's
  current state rather than from this row.
  """

  __tablename__ = "broadcast_message_logs"
  __table_args__ = (
    UniqueConstraint(
      "broadcast_message_id", "seq", name="uq_broadcast_message_logs_message_seq"
    ),
  )

  broadcast_message_id: Mapped[uuid.UUID] = mapped_column(
    UUID(as_uuid=True),
    ForeignKey("broadcast_messages.id", ondelete="CASCADE"),
    nullable=False,
    index=True,
  )
  seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
  kind: Mapped[BroadcastLogKindEnum] = mapped_column(
    Enum(BroadcastLogKindEnum), nullable=False
  )
  payload: Mapped[dict] = mapped_column(JSONB, nullable=True)

  status: Mapped[BroadcastLogStatusEnum] = mapped_column(
    Enum(BroadcastLogStatusEnum),
    nullable=False,
    default=BroadcastLogStatusEnum.PENDING,
    server_default=BroadcastLogStatusEnum.PENDING.value,
    index=True,
  )
  attempts: Mapped[int] = mapped_column(
    Integer, nullable=False, default=0, server_default="0"
  )
  last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
  delivered_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
  )

  def __repr__(self) -> str:
    return (
      f"<BroadcastMessageLog broadcast_message_id={self.broadcast_message_id} "
      f"seq={self.seq} kind={self.kind} status={self.status}>"
    )


class TradeNotification(Base):
  """
  One row per live trade card: the Telegram message a subscriber was sent for
  one trade, remembered so later status changes can *edit* that same message
  instead of posting a new one.

  Keyed by ``(trade_id, platform, chat_id)`` — one card per trade per
  recipient. ``message_id`` is what ``editMessageText`` needs; ``status`` is
  the trade status the card currently shows, so an event that changes nothing
  visible (a worker re-emitting the same status after an SL tweak) is skipped
  rather than spending a Bot API call that Telegram would reject as
  "message is not modified".

  ``chat_id`` holds the recipient's platform user id (a Telegram DM chat has
  the same id as the user), as text for the same reason ``AccountBotLink``
  stores ids as text. Rows are deleted with their trade, and dropped
  individually when Telegram reports the message as permanently unreachable
  (user deleted it, or blocked the bot).
  """

  __tablename__ = "trade_notifications"
  __table_args__ = (
    UniqueConstraint(
      "trade_id",
      "platform",
      "chat_id",
      name="uq_trade_notifications_trade_platform_chat",
    ),
  )

  trade_id: Mapped[uuid.UUID] = mapped_column(
    UUID(as_uuid=True),
    ForeignKey("trades.id", ondelete="CASCADE"),
    nullable=False,
    index=True,
  )
  platform: Mapped[BotPlatformTypeEnum] = mapped_column(
    Enum(BotPlatformTypeEnum), nullable=False
  )
  chat_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
  message_id: Mapped[int] = mapped_column(Integer, nullable=False)
  status: Mapped[TradeStatusEnum] = mapped_column(Enum(TradeStatusEnum), nullable=False)

  def __repr__(self) -> str:
    return (
      f"<TradeNotification trade_id={self.trade_id} chat_id={self.chat_id} "
      f"message_id={self.message_id} status={self.status}>"
    )


class BrokerSetting(Base):
  """
  One row per broker setting.
  """

  __tablename__ = "broker_settings"
  __table_args__ = (UniqueConstraint("key", name="uq_broker_settings_key"),)

  key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
  value: Mapped[str] = mapped_column(Text, nullable=False)

  def __repr__(self) -> str:
    return f"<BrokerSetting id={self.id} key={self.key} value={self.value}>"
