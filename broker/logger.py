"""
broker/logger.py — Structured coloured logger.
"""

from __future__ import annotations

import copy
import datetime
import logging
import sys
from pathlib import Path

from broker.settings import settings

LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(parents=True, exist_ok=True)


class _DailyFileHandler(logging.FileHandler):
  """FileHandler that rolls over to a new dated log file at midnight without restarting the process."""

  def __init__(self, directory: Path, mode: str = "a", encoding: str = "utf-8"):
    self.directory = directory
    self.current_date = datetime.datetime.now().strftime("%Y%m%d")
    super().__init__(directory / f"{self.current_date}.log", mode, encoding)

  def emit(self, record: logging.LogRecord) -> None:
    new_date = datetime.datetime.now().strftime("%Y%m%d")
    if new_date != self.current_date:
      self.close()
      self.current_date = new_date
      self.baseFilename = str(self.directory / f"{new_date}.log")
      self.stream = self._open()
    super().emit(record)


def _tz_suffix() -> str:
  """Return the local timezone abbreviation or UTC offset, e.g. 'ICT' or '+0700'."""
  now = datetime.datetime.now(datetime.timezone.utc).astimezone()
  tz_name = now.strftime("%Z")
  return tz_name if tz_name else now.strftime("%z")


def uvicorn_log_config() -> dict:
  """Return a uvicorn LOGGING_CONFIG with timezone in the access-log format."""
  from uvicorn.config import LOGGING_CONFIG

  cfg = copy.deepcopy(LOGGING_CONFIG)
  tz = _tz_suffix()
  access_fmt = f"%(asctime)s {tz} | %(levelprefix)s %(message)s"
  cfg["formatters"]["access"]["fmt"] = access_fmt
  cfg["formatters"]["access"]["datefmt"] = "%Y-%m-%d %H:%M:%S"
  cfg["formatters"]["default"]["fmt"] = f"%(asctime)s {tz} | %(levelprefix)s %(message)s"
  cfg["formatters"]["default"]["datefmt"] = "%Y-%m-%d %H:%M:%S"
  return cfg


def get_logger(name: str) -> logging.Logger:
  logger = logging.getLogger(name)

  if not logger.handlers:
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    logger.setLevel(level)

    fmt = logging.Formatter(
      fmt="%(asctime)s | %(levelname)-8s | %(process)d | %(name)s | %(message)s",
      datefmt=f"%Y-%m-%d %H:%M:%S {_tz_suffix()}",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(fmt)

    file_handler = _DailyFileHandler(LOGS_DIR)
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.propagate = False

  _maybe_attach_telegram_handler(logger)

  return logger


def _maybe_attach_telegram_handler(logger: logging.Logger) -> None:
  """Attach the shared Telegram error handler when enabled in settings.

  Imported lazily to avoid a circular import (``notification_service`` imports
  this module). Skipped while ``notification_service`` is still initializing —
  its own logger is filtered from forwarding anyway."""
  if not (settings.TELEGRAM_ENABLED and settings.TELEGRAM_LOG_ERRORS_ENABLED):
    return

  from broker.services import notification_service

  handler = getattr(notification_service, "telegram_log_handler", None)
  if handler is None:
    return

  if handler not in logger.handlers:
    logger.addHandler(handler)
