"""
broker/db/repository — async persistence layer, one module per aggregate.

Each repository class is a thin data-access wrapper around SQLAlchemy sessions
and implements a Protocol declared in ``broker/interfaces/`` so that callers
depend on the abstraction, not on these concrete classes. Business rules about
the trade lifecycle live in ``broker/domain/trade_status.py`` — these modules
only read and write rows.

Modules:

* ``account`` — accounts, link tokens, bot links, bot sessions
* ``trade`` — trades reported by workers
* ``trade_notification`` / ``trade_broadcast`` — owner trade cards
* ``broadcast_message`` — broadcast cycles and their write log
* ``setting`` — ``broker_settings``
* ``signal`` — persisted inbound signals

Import the concrete classes from this package; the module split is an
implementation detail. Sessions are opened through ``_session.get_session``
so tests can swap the factory for every repository at once.
"""

from broker.db.repository import _session

from broker.db.repository.account import SqlAlchemyAccountRepository
from broker.db.repository.trade_broadcast import SqlAlchemyTradeBroadcastRepository
from broker.db.repository.broadcast_message import SqlAlchemyBroadcastMessageRepository
from broker.db.repository.trade_notification import (
  SqlAlchemyTradeNotificationRepository,
)
from broker.db.repository.setting import SqlAlchemySettingRepository
from broker.db.repository.signal import SqlAlchemySignalRepository
from broker.db.repository.trade import SqlAlchemyTradeRepository

__all__ = [
  "_session",
  "SqlAlchemyAccountRepository",
  "SqlAlchemyTradeBroadcastRepository",
  "SqlAlchemyBroadcastMessageRepository",
  "SqlAlchemyTradeNotificationRepository",
  "SqlAlchemySettingRepository",
  "SqlAlchemySignalRepository",
  "SqlAlchemyTradeRepository",
]
