"""
broker/logger.py — Structured coloured logger.
"""

from __future__ import annotations

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


def get_logger(name: str) -> logging.Logger:
  logger = logging.getLogger(name)

  if not logger.handlers:
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    logger.setLevel(level)

    fmt = logging.Formatter(
      fmt="%(asctime)s | %(levelname)-8s | %(process)d | %(name)s | %(message)s",
      datefmt="%Y-%m-%d %H:%M:%S",
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

  Imported lazily to avoid a circular import (the handler pulls in
  ``notification_service``, which imports this module)."""
  if not (settings.TELEGRAM_ENABLED and settings.TELEGRAM_LOG_ERRORS_ENABLED):
    return

  from broker.services.telegram_log_handler import telegram_log_handler

  if telegram_log_handler not in logger.handlers:
    logger.addHandler(telegram_log_handler)
