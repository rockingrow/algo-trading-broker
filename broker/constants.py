SIGNAL_BLOCKED = "signal_blocked"
SILENT_SIGNAL = "silent_signal"
NOTIFICATION_INCLUDE_SIGNAL_RAW = "notification_include_signal_raw"
NOTIFICATION_TIMEZONE_KEY = "notification_timezone"
CRYPTO_ALLOWED_SYMBOL_KEY = "crypto_allowed_symbol"
CRYPTO_MAX_LEVERAGE_KEY = "crypto_max_leverage"

# Time window (in seconds) used by the SYSTEM.RETRY_SIGNAL replay sent back to a
# freshly-connected worker: the broker returns every signal persisted in the
# last MAX_RETRY_TIMEOUT seconds whose strategy the worker announced.
MAX_RETRY_TIMEOUT_KEY = "max_retry_timeout"
DEFAULT_MAX_RETRY_TIMEOUT_SECONDS = 60
