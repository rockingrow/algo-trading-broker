# 🚀 Algo Trading Broker

A **broker-only** service that:

1. **Receives** TradingView JSON webhook alerts
2. **Logs** every signal to **PostgreSQL** (`signal_log` table)
3. **Publishes** signals via **ZeroMQ PUB** to subscriber VPS nodes

```
TradingView Alert (JSON webhook)
        │  POST :8080/webhook
        ▼
┌───────────────────────────────────────┐
│           BROKER (this repo)          │
│                                       │
│  FastAPI  :8080                       │
│  ├── POST /webhook   ← TV alert       │
│  ├── GET  /status    ← in-memory stat │
│  └── GET  /health                     │
│                                       │
│  PostgreSQL                           │
│  ├── signal_log   ← every TV signal   │
│  └── trade_event  ← subscriber events│
│                                       │
│  ZMQ PUB  :5555  ──────────────────┐  │
│  ZMQ PULL :5556  ◄─────────────────┘  │
└───────────────────────────────────────┘
         :5555 ↓              ↑ :5556
   ┌──────────────┐   ┌──────────────┐
   │  VPS #1 SUB  │   │  VPS #1 PUSH │
   │  (executor)  │   │  (reporter)  │
   └──────────────┘   └──────────────┘
```

## Project Structure

```
algo-trading-broker/
├── broker/
│   ├── main.py              # Entry point
│   ├── webhook.py           # FastAPI — POST /webhook, GET /status
│   ├── publisher.py         # ZeroMQ PUB (signals → subscribers)
│   ├── trade_listener.py    # ZeroMQ PULL (trade events ← subscribers)
│   ├── signal_parser.py     # TradingView JSON → TradingSignal
│   ├── models.py            # Pydantic models
│   └── db/
│       ├── engine.py        # Async SQLAlchemy engine + session
│       ├── models.py        # ORM: signal_log, trade_event tables
│       └── repository.py    # log_signal(), log_trade_event()
├── shared/
│   ├── config.py            # Pydantic settings from .env
│   └── logger.py            # Structured logger
├── docker-compose.yml       # Broker + PostgreSQL
├── Dockerfile.broker
├── requirements.txt
└── .env.example
```

## Quick Start

```bash
# 1. Clone
git clone <this-repo> && cd algo-trading-broker

# 2. Configure
cp .env.example .env        # Edit with your values

# 3a. Docker (recommended)
docker compose up -d

# 3b. Manual
pip install -r requirements.txt
python3 -m broker.main
```

## PostgreSQL Schema

### `signal_log` — TradingView webhook signals

| Column | Type | Description |
|--------|------|-------------|
| `id` | bigserial PK | Auto ID |
| `signal_id` | varchar(64) | UUID generated per signal |
| `received_at` | timestamptz | When webhook arrived |
| `action` | varchar(20) | `open` / `close` / `close_all` / `modify` |
| `symbol` | varchar(20) | e.g. `XAUUSD` |
| `direction` | varchar(10) | `buy` / `sell` / null |
| `volume` | float | Lot size |
| `sl` / `tp` | float | Stop loss / take profit prices |
| `ticket` | bigint | MT5 ticket (if provided) |
| `comment` | varchar | Free-text comment |
| `raw_payload` | jsonb | Full original JSON |
| `published` | bool | Did ZMQ publish succeed? |
| `error` | text | Error message if any |

## TradingView Webhook Payload

```json
{
  "action":    "open",
  "symbol":    "XAUUSD",
  "direction": "buy",
  "volume":    0.1,
  "sl":        1900.00,
  "tp":        1950.00,
  "comment":   "{{strategy.order.id}}"
}
```

Supported `action` values: `open`, `close`, `close_all`, `modify`

## Environment Variables

See `.env.example` for all settings.

| Variable | Default | Description |
|----------|---------|-------------|
| `WEBHOOK_PORT` | `8080` | Webhook HTTP port |
| `WEBHOOK_SECRET` | `""` | HMAC secret (blank = disabled) |
| `ZMQ_PUB_PORT` | `5555` | ZMQ PUB — signals to subscribers |
| `POSTGRES_HOST` | `localhost` | PostgreSQL host |
| `POSTGRES_PORT` | `5432` | PostgreSQL port |
| `POSTGRES_DB` | `algo_broker` | Database name |
| `POSTGRES_USER` | `algo` | Database user |
| `POSTGRES_PASSWORD` | `changeme` | Database password |