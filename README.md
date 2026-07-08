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
- Market-regime filter with limited high-conviction longs in weak regimes, plus confirmed inverse-ETF hedging with exposure caps and earnings blackouts
- Congressional trade auto-discovery from official House and Senate disclosure feeds
- Analyst consensus checks and exclusion of broad-market or sector ETFs from buy candidates
- A put-shadow paper evaluator that scores hypothetical put trades so you can judge a bearish strategy before funding it
- FastAPI dashboard and a holiday-aware daily email report with zero-P&L self-diagnosis
- Optional E*TRADE mirror that copies fills to a second brokerage account with independent risk caps (preview-only by default)

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

### 5. Optional tools

```bash
# Analyze a running bot's trade history and signal edge (defaults to the
# local dashboard; point --url or TRADEBOT_DASHBOARD_URL at a deployment)
python analyze_performance.py

# Interactive lesson on how put options work, with paper examples
python puts_learn.py

# Obtain and verify E*TRADE OAuth tokens for the mirror feature
python etrade_smoke.py
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
- `MAX_CONSECUTIVE_BUY_ERRORS`
- `MIN_BUY_NOTIONAL`

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

Regime and hedging:

- `MARKET_REGIME_FILTER`
- `MARKET_REGIME_ALLOW_LIMITED_LONGS`
- `INVERSE_CONFIRMATION_HOURS`
- `MAX_INVERSE_POSITIONS`
- `MAX_INVERSE_EXPOSURE_PCT`
- `EARNINGS_BLACKOUT_DAYS`
- `EXCLUDE_BROAD_MARKET_ETFS`

E*TRADE mirror (optional, disabled by default):

- `ETRADE_MIRROR_ENABLED`
- `ETRADE_MIRROR_ENV`
- `ETRADE_ACCOUNT_ID_KEY`
- `ETRADE_MIRROR_PREVIEW_ONLY`
- `ETRADE_MIRROR_MAX_ORDER_VALUE`
- `ETRADE_MIRROR_MAX_TOTAL_CAPITAL`
- plus consumer key / access token secrets per environment (see `.env.example`)

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
  analyst_consensus.py
  put_shadow.py
  etrade.py
  mirror.py
  mcp_bridge.py
  templates/index.html
```

## Notes

- This repository is a starter project, not financial advice.
- Demo mode is the safest way to explore the system before connecting a broker.
- Review all live-trading and compliance requirements for your jurisdiction before using real money.
