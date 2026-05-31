"""Test configuration: provide the minimal environment the settings module
requires so importing ``broker.*`` never touches a real .env or live services."""

import os

os.environ.setdefault("BROKER_API_KEY", "test-api-key")
os.environ.setdefault("WEBHOOK_SECRET", "secret")
os.environ.setdefault("TELEGRAM_ENABLED", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")
