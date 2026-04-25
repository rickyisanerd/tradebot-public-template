# TradeBot

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/new)

TradeBot is a Python trading bot for low-priced equities. It can run in demo mode with no broker keys, or connect to Alpaca paper and live accounts. It scans a configurable universe, combines several analyzers plus external signal inputs, learns from outcomes over time, and exposes a FastAPI dashboard for monitoring and manual control.

## What it includes

- Demo, paper, and live Alpaca broker modes
- Local analyzers for momentum, pullback, risk, and decision support
- External signals for congressional trades, SEC filings, earnings, macro events, and short-volume data
- SQLite persistence for trades, positions, learning weights, cached signals, and audit history
- Dynamic scaling of capital, scan breadth, and risk as equity changes
- Peak-based trailing stops, optional partial profits, and account-level safety rails
- FastAPI dashboard and daily email report support

## Quick start

### 1. Clone and install

```bash
git clone https://github.com/YOUR_GITHUB_USER/tradebot-public-template.git
cd tradebot-public-template
cp .env.example .env
```

#### Linux or macOS

```bash
./create_venv.sh
source .venv/bin/activate
```

#### Windows

```powershell
create_venv.bat
.venv\Scripts\activate
```

### 2. Run in demo mode

```bash
python -m tradebot.cli scan
python -m tradebot.cli trade-once
python -m tradebot.cli dashboard
```

Open the dashboard at [http://127.0.0.1:8008](http://127.0.0.1:8008).

### 3. Refresh external signals

```bash
python -m tradebot.cli refresh-signals
python -m tradebot.cli refresh-congress
python -m tradebot.cli refresh-sec
python -m tradebot.cli refresh-earnings
python -m tradebot.cli refresh-macro
```

### 4. Run tests

```bash
python run_tests.py
```

## Broker modes

| Mode | Env value | Keys required |
| --- | --- | --- |
| Demo | `BROKER_MODE=demo` | No |
| Paper | `BROKER_MODE=paper` | Yes |
| Live | `BROKER_MODE=live` | Yes |

## Important environment variables

Copy `.env.example` to `.env` and update the values for your environment.

Core settings:

- `BROKER_MODE`
- `ALPACA_KEY_ID`
- `ALPACA_SECRET_KEY`
- `DATA_DIR`
- `AUTO_TRADE_ENABLED`
- `AUTO_TRADE_INTERVAL_MINUTES`
- `STARTING_CASH`

Risk and sizing:

- `MAX_TOTAL_CAPITAL`
- `MAX_OPEN_POSITIONS`
- `MAX_NEW_POSITIONS_PER_RUN`
- `RISK_PER_TRADE_PCT`
- `MAX_POSITION_PCT`
- `STOP_LOSS_PCT`
- `TRAILING_STOP_PCT`
- `DRAWDOWN_SOFT_LIMIT_PCT`
- `DRAWDOWN_HARD_LIMIT_PCT`
- `DAILY_LOSS_LIMIT_PCT`

External signals:

- `CONGRESS_REPORT_URLS`
- `SEC_USER_AGENT`
- `ALPHA_VANTAGE_API_KEY`
- `POLYGON_API_KEY`

Email reporting:

- `RESEND_API_KEY`
- `REPORT_EMAIL`
- `REPORT_SENDER_EMAIL`
- `REPORT_DASHBOARD_URL`

## Deploying to Railway

1. Push this repo to GitHub.
2. Create a new Railway project from the repo.
3. Add your environment variables in Railway.
4. Mount a persistent volume and set `DATA_DIR=/data` if you want SQLite state to survive redeploys.
5. Start with `AUTO_TRADE_ENABLED=false` until you confirm your broker and signal configuration.

Railway tips:

- Run one instance only. SQLite and the in-process scheduler are not meant for multiple replicas.
- Keep live risk small until you have confidence in the setup.
- Export your learning weights before major redeploys if you want to preserve them.

## Architecture

```text
tradebot/
  cli.py
  config.py
  dashboard.py
  db.py
  engine.py
  analytics.py
  providers.py
  congress.py
  sec.py
  earnings.py
  macro.py
  polygon.py
  email_report.py
  mcp_bridge.py
  templates/index.html
```

## Notes

- This repository is a starter project, not financial advice.
- Demo mode is the safest way to explore the system before connecting a broker.
- Review all live-trading and compliance requirements for your jurisdiction before using real money.
