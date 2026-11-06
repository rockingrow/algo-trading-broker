import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import json
import logging
import re
import time
import traceback

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from broker.db.engine import close_db, init_db
from broker.db.repository import (
  SqlAlchemyAccountRepository,
  SqlAlchemySettingRepository,
  SqlAlchemySignalRepository,
  SqlAlchemyTradeBroadcastRepository,
  SqlAlchemyTradeNotificationRepository,
  SqlAlchemyTradeRepository,
)
from broker.helpers import emoji_constants as em
from broker.logger import get_logger
from broker.nats import DEMO_SIGNAL_SUBJECT_FILTER, nats_client
from broker.openapi import fastapi_kwargs
from broker.providers import (
  make_broadcast_dispatcher,
  make_signal_broadcaster,
  make_signals_notifier,
)
from broker.router import get_core_router
from broker.services.demo_signal_service import DemoSignalConsumer
from broker.services.nats_service import (
  NatsPublisher,
  SystemEventConsumer,
  TradeEventConsumer,
)
from broker.services.notification_service import (
  QueuedNotifier,
  TelegramNotification,
)
from broker.services.signal_processing_service import (
  DeferredEnqueuer,
  SignalProcessingService,
  SignalWorker,
)
from broker.services.signal_retry_job import SignalRetryJob
from broker.services.trade_card_service import TradeCardService
from broker.settings import settings

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
  """Start every background service, then stop them in reverse order.

  Each service registers its ``stop`` on an ``AsyncExitStack`` the moment it
  has started, so shutdown is the start sequence played backwards — and a
  service that fails to start still unwinds everything started before it,
  instead of leaving a NATS connection or a sweeper task running behind a
  crashed startup. The start order therefore encodes the shutdown contract:

  * consumers that *produce* work (TRADE, SYSTEM, the drill, ``SignalWorker``,
    the retry job) start last, so they stop first;
  * the broadcast dispatcher starts before them, so its sweeper's last pass
    drains whatever they wrote on their way out;
  * the queues behind them (signal and drill notifiers, deferred enqueue,
    trade cards) start earlier still, so nothing is queued behind their flush;
  * NATS and the database start first and close last, so a pending enqueue
    or write can still land.
  """
  notifier = TelegramNotification()

  async with AsyncExitStack() as stack:
    # Install the Telegram ERROR log handler first so failures during the rest
    # of startup are already forwarded. ``start`` is idempotent when the
    # feature is disabled (no records ever reach the handler).
    if settings.telegram.ENABLED and settings.telegram.LOG_ERRORS_ENABLED:
      from broker.services.notification_service import telegram_log_handler

      telegram_log_handler.start(asyncio.get_running_loop())
      stack.push_async_callback(telegram_log_handler.stop)

    await init_db()
    stack.push_async_callback(close_db)
    # NATS lifecycle alerts go through a queue as well: nats-py awaits the
    # disconnected/reconnected callbacks inside its own reconnect loop, so a
    # Telegram send that hangs for the full HTTP timeout would hold up the very
    # reconnect the webhook is waiting on.
    nats_notifier = QueuedNotifier(notifier)
    await _start(stack, nats_notifier)
    nats_client.set_notifier(nats_notifier)
    await nats_client.connect()
    stack.push_async_callback(nats_client.close)

    publisher = NatsPublisher(connection=nats_client)
    setting_repo = SqlAlchemySettingRepository()
    signal_repo = SqlAlchemySignalRepository()
    # Owner trade cards run off their own queue for the same reason as the
    # signal notifications: the TRADE consumer handles one report at a time, so
    # a DM to a throttled Telegram would stall the trade bookkeeping behind it.
    # QueuedNotifier can't serve here — a card needs the message id its send
    # returns — so the service owns the queue itself.
    trade_card_service = TradeCardService(
      broadcast_repository=SqlAlchemyTradeBroadcastRepository(),
      notification_repository=SqlAlchemyTradeNotificationRepository(),
      setting_repository=setting_repo,
    )
    await _start(stack, trade_card_service)
    # Signal-cycle broadcaster writer + CDC dispatcher: signals and TRADE events
    # only record cycle changes onto ``broadcast_messages``; a Postgres
    # LISTEN/NOTIFY listener wakes the dispatcher which then edits the Telegram
    # message. Keeping them separate takes Telegram off the signal path entirely.
    signal_broadcaster = make_signal_broadcaster()
    broadcast_dispatcher = make_broadcast_dispatcher(setting_repo)
    # Signal notifications go out through a queue: the JetStream worker handles
    # envelopes one at a time, so awaiting a Telegram send that a throttled
    # network will hold for the full HTTP timeout would delay the *next*
    # signal's fan-out to the trading workers.
    signals_notifier = QueuedNotifier(make_signals_notifier(setting_repo))
    await _start(stack, signals_notifier)
    # Stopped after the producers, so nothing is still being queued behind the
    # flush, and before NATS closes, so a pending enqueue can still land.
    deferred_enqueuer = DeferredEnqueuer(publisher)
    await _start(stack, deferred_enqueuer)
    # QTE's end-to-end drill. Given no publisher on purpose: it renders the
    # Telegram card a real signal would get and can reach no worker — see
    # broker/services/demo_signal_service.py. Its operator line goes through a
    # queue for the same reason every other Telegram call on a NATS path does:
    # nats-py awaits a subscription's callback before pulling that
    # subscription's next message, and `make demo-signal` fires one drill per
    # mounted strategy back to back — unqueued, a throttled Telegram would
    # serialize the whole run behind one HTTP timeout each.
    demo_notifier = QueuedNotifier(notifier)
    await _start(stack, demo_notifier)
    # Before the producers, so it stops after them: the sweeper's last pass
    # drains anything they wrote to the broadcast log on their way out.
    await _start(stack, broadcast_dispatcher)

    signal_service = SignalProcessingService(
      signal_repository=signal_repo,
      setting_repository=setting_repo,
      publisher=publisher,
      notifier=signals_notifier,
      broadcaster=signal_broadcaster,
      webhook_secret=settings.webhook.SECRET,
      deferred_enqueuer=deferred_enqueuer,
    )
    producers = [
      TradeEventConsumer(
        trade_repository=SqlAlchemyTradeRepository(),
        connection=nats_client,
        card_service=trade_card_service,
        signal_broadcast_service=signal_broadcaster,
      ),
      SystemEventConsumer(
        setting_repository=setting_repo,
        account_repository=SqlAlchemyAccountRepository(),
        publisher=publisher,
        signal_repository=signal_repo,
        connection=nats_client,
      ),
      DemoSignalConsumer(
        broadcaster=signal_broadcaster,
        notifier=demo_notifier,
        connection=nats_client,
      ),
      SignalWorker(service=signal_service, connection=nats_client),
      SignalRetryJob(
        service=signal_service,
        signal_repository=signal_repo,
        interval_seconds=settings.signal.RETRY_INTERVAL_SECONDS,
      ),
    ]
    for producer in producers:
      await _start(stack, producer)

    app.state.publisher = publisher
    # The webhook route builds its own request-scoped service, but the deferred
    # queue must outlive the request that filled it.
    app.state.deferred_enqueuer = deferred_enqueuer

    api_prefix = (
      f"/{settings.broker_api.API_PREFIX}" if settings.broker_api.API_PREFIX else ""
    )

    # Notification: Startup
    await notifier.send_message(
      f"{em.BROKER_STARTED} <b>Broker Node Started</b>\n"
      f"{em.PLUG} NATS Publishing: <code>{nats_client.subjects_line()}</code> + dynamic (by strategy & per-account ADMIN)\n"
      f"{em.PLUG} NATS Listening: <code>{nats_client.listen_subjects_line()}</code>\n"
      f"{em.DEMO_SIGNAL} NATS Drill: <code>{DEMO_SIGNAL_SUBJECT_FILTER}</code> (Telegram only)\n"
      f"{em.ENDPOINT} Endpoint: <code>{settings.broker_url}{api_prefix}</code>"
    )

    try:
      yield
    finally:
      # Notification: Shutdown — sent before anything stops, while NATS and the
      # notifier queues are still up. The stack then unwinds every service.
      await notifier.send_message(
        f"{em.BROKER_STOPPED} <b>Broker Node Stopped</b>\n"
        f"{em.ENDPOINT} Endpoint: <code>{settings.broker_url}{api_prefix}</code>"
      )


async def _start(stack: AsyncExitStack, service) -> None:
  """Start *service* and register its ``stop`` for the reverse-order shutdown."""
  await service.start()
  stack.push_async_callback(service.stop)


def install_webhook_connection_close(app: FastAPI) -> None:
  """Send ``Connection: close`` on every response to the TradingView webhook,
  and time how long each delivery took to answer.

  TradingView keeps a per-endpoint TCP pool, but uvicorn drops idle keep-alive
  sockets after ``WEBHOOK_KEEPALIVE_TIMEOUT`` seconds. On any strategy whose
  alert cadence exceeds that timeout (e.g. a 15-minute timeframe against the
  120s default), TradingView reuses a socket the server has already closed
  and the delivery fails with "server closed the connection unexpectedly".
  Signalling close per response makes TradingView open a fresh TCP for every
  alert, removing the race entirely at the cost of one extra handshake.

  The timing is what turns the *other* TradingView failure — "request took too
  long and timed out", reported by TradingView with no server-side trace at
  all — into something readable in the broker's own log: one line per alert
  with the elapsed milliseconds, raised to ``warning`` when the handler
  overruns the enqueue deadline it is supposed to fit inside.
  """

  @app.middleware("http")
  async def _force_close_webhook(request: Request, call_next):
    if not request.url.path.endswith("/secret/webhook"):
      return await call_next(request)

    started = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - started
    response.headers["Connection"] = "close"
    level = (
      logging.WARNING if elapsed > settings.webhook.ENQUEUE_TIMEOUT else logging.INFO
    )
    log.log(
      level,
      "Webhook answered %s in %.0fms",
      response.status_code,
      elapsed * 1000,
    )
    return response


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


def json_syntax_error(errors: list) -> str | None:
  """Describe *where* a rejected body stopped being valid JSON, if that is why.

  TradingView parses an alert message itself and, when the parse fails, sends
  the text as ``text/plain`` instead of ``application/json``. FastAPI then
  hands the raw body straight to the model, and pydantic reports the generic
  ``Input should be a valid dictionary or object to extract fields from`` — a
  message that says nothing about the actual problem, which is a syntax error
  somewhere in the alert's JSON (a Pine ``str.tostring`` that emitted a
  thousands separator, an unquoted value, a trailing comma).

  Returns ``None`` when the body is not that case, so ordinary field-level
  validation errors are reported unchanged.
  """
  for error in errors:
    if tuple(error.get("loc") or ()) != ("body",):
      continue
    raw = error.get("input")
    if isinstance(raw, bytes):
      raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str):
      continue
    try:
      json.loads(raw)
    except json.JSONDecodeError as exc:
      # A window around the offending character — the whole alert body is
      # hundreds of fields long and unreadable in a log line. Sliced before
      # redacting so the reported position still matches the body as sent.
      snippet = redact_secrets(raw[max(0, exc.pos - 40) : exc.pos + 40])
      return f"{exc.msg} at line {exc.lineno} column {exc.colno} — near: …{snippet}…"
    except ValueError:
      continue
  return None


def install_exception_handlers(app: FastAPI) -> None:
  """Translate validation failures and crashes into logged JSON responses."""

  @app.exception_handler(RequestValidationError)
  async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # Position first (needs the body as sent), secrets scrubbed second.
    raw_errors = jsonable_encoder(exc.errors())
    json_error = json_syntax_error(raw_errors)
    errors = redact_secrets(raw_errors)
    if json_error:
      log.warning(
        "422 Unprocessable Content | %s %s | body is not valid JSON: %s",
        request.method,
        request.url.path,
        json_error,
      )
      return JSONResponse(
        status_code=422, content={"detail": errors, "json_error": json_error}
      )

    log.warning(
      "422 Unprocessable Content | %s %s | %s",
      request.method,
      request.url.path,
      errors,
    )
    return JSONResponse(status_code=422, content={"detail": errors})

  @app.exception_handler(Exception)
  async def global_exception_handler(request: Request, exc: Exception):
    log.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc)
    log.error(traceback.format_exc())
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


def create_app() -> FastAPI:
  """Build and return the FastAPI application with all routes wired up."""
  app = FastAPI(lifespan=lifespan, **fastapi_kwargs())

  install_webhook_connection_close(app)
  install_exception_handlers(app)

  # Include Core Router — mount under secret prefix if configured
  api_prefix = (
    f"/{settings.broker_api.API_PREFIX}" if settings.broker_api.API_PREFIX else ""
  )
  app.include_router(get_core_router(), prefix=api_prefix)

  return app
