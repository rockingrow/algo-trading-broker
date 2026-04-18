ZeroMQ Message Format: <TOPIC>|<JSON_PAYLOAD>

1. LONG Signal:
SIGNAL|{"signal_id":"d1678122-3837-4340-9a8f-28c03728615c","timestamp":"2026-04-10T22:55:00Z","action":"LONG","symbol":"XAUUSD","price":2334.5,"quantity":6.0,"sl":2329.5,"tp1":2340.0,"tp2":2345.0,"is_running":false,"risk_percent":3.0}

2. SHORT Signal:
SIGNAL|{"signal_id":"7677943d-0d9c-4444-8d48-6a5996055d72","timestamp":"2026-04-11T14:30:00Z","action":"SHORT","symbol":"XAUUSD","price":2350.0,"quantity":6.0,"sl":2355.0,"tp1":2345.0,"tp2":2340.0,"is_running":false,"risk_percent":3.0}

3. TP1 Signal (Partial Close):
SIGNAL|{"signal_id":"90d18227-862d-4f1b-a5b6-735870b55160","timestamp":"2026-04-11T15:05:00Z","action":"TP1","symbol":"XAUUSD","price":2345.0,"quantity":1.8,"sl":null,"tp1":null,"tp2":null,"is_running":true,"risk_percent":3.0}

4. TP2 Signal (Full Close):
SIGNAL|{"signal_id":"c4f4a3e9-79a8-444c-9f8e-8a241284a1e9","timestamp":"2026-04-11T15:45:00Z","action":"TP2","symbol":"XAUUSD","price":2340.0,"quantity":4.2,"sl":null,"tp1":null,"tp2":null,"is_running":false,"risk_percent":3.0}

5. SL Signal (Full Close):
SIGNAL|{"signal_id":"b372074e-7d63-4424-9f8e-4a81284a1e9c","timestamp":"2026-04-11T10:10:00Z","action":"SL","symbol":"XAUUSD","price":2330.0,"quantity":6.0,"sl":null,"tp1":null,"tp2":null,"is_running":false,"risk_percent":3.0}

6. R_SL Signal (Revised Stop Loss):
SIGNAL|{"signal_id":"f1278122-3837-4340-9a8f-28c03728615c","timestamp":"2026-04-11T16:15:00Z","action":"R_SL","symbol":"XAUUSD","price":2350.0,"quantity":4.2,"sl":null,"tp1":null,"tp2":null,"is_running":false,"risk_percent":3.0}
