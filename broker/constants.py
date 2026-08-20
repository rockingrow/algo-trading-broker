SIGNAL_BLOCKED = "signal_blocked"
SILENT_SIGNAL = "silent_signal"
NOTIFICATION_INCLUDE_SIGNAL_RAW = "notification_include_signal_raw"
NOTIFICATION_TIMEZONE_KEY = "notification_timezone"
CRYPTO_ALLOWED_SYMBOL_KEY = "crypto_allowed_symbol"
CRYPTO_MAX_LEVERAGE_KEY = "crypto_max_leverage"

# broker_settings key holding the PUBLIC signal-broadcast chat ids as a
# comma-separated list (e.g. ``-1001234567890,@my_public_channel``). Unlike the
# private broadcast chats (``TELEGRAM_PRIVATE_BROADCAST_CHAT_IDS`` env var, a deployment
# concern), the public audience is edited at runtime — from the admin API or the
# Telegram bot's /admin_public_chats — so it lives in the database, not .env.
# Empty = the public broadcast is off.
PUBLIC_BROADCAST_CHAT_IDS_KEY = "public_broadcast_chat_ids"

# broker_settings key holding the strategy → magic-number map as a JSON text
# blob (e.g. ``{"MT5_GOLD_M5_V1": 20260409, ...}``). Sent to every worker in the
# ``strategy_magic_map`` block of its WORKER_CONNECTED_ACK, filtered down to the
# strategies that worker announced. Editable via
# POST /admin/settings/strategy-magic-map.
STRATEGY_MAGIC_MAP_KEY = "strategy_magic_map"

# Time window (in seconds) used by the ``retry_signals`` replay inside the
# WORKER_CONNECTED_ACK: the broker returns every signal persisted in the
# last MAX_RETRY_TIMEOUT seconds whose strategy the worker announced.
MAX_RETRY_TIMEOUT_KEY = "max_retry_timeout"
DEFAULT_MAX_RETRY_TIMEOUT_SECONDS = 60

# ── accounts.settings keys ────────────────────────────────────────────────
# Keys inside the per-account ``accounts.settings`` JSONB blob — a different
# scope from everything above, which are broker-wide ``broker_settings`` rows.
# Each one must match a field name on
# ``broker.schemas.account_schema.AccountSettings`` (pinned by a test), which
# is what shapes the ``settings`` block of the WORKER_CONNECTED_ACK.
#
# Written by POST /v1/telegram/{id}/commands/prevent — /prevent stores True,
# /allow stores False — alongside the BLOCK_SIGNAL/ALLOW_SIGNAL admin push.
ACCOUNT_SETTING_SIGNAL_BLOCKED = "signal_blocked"
