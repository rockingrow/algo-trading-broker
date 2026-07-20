1. SIGNAL
2. ADMIN (broadcast admin messages — no account_id)
   ADMIN.<market>.<gateway>.<account_id> (private per-account admin subject — only that account's worker subscribes; e.g. ADMIN.FOREX.MT5.12345678)
3. TRADE
4. SYSTEM
5. SIGNALS.> (JetStream stream — durable webhook envelope buffer feeding the broker's own SignalWorker)
