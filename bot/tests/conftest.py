"""Provide the minimal environment BotSettings requires so importing
``app.config`` during tests never needs a real .env or live services."""

import os

os.environ.setdefault("BOT_TELEGRAM_TOKEN", "123:test-token")
os.environ.setdefault("BROKER_API_KEY", "test-api-key")
os.environ.setdefault("TELEGRAM_ADMIN_IDS", "")
os.environ.setdefault("BOT_LOG_LEVEL", "WARNING")
