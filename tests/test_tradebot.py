import os
import subprocess
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from fastapi.testclient import TestClient
import etrade_smoke

from tradebot.congress import CongressTracker
from tradebot.config import Settings
from tradebot.dashboard import TradingScheduler, create_app, mirror_retry_needed
from tradebot.db import Database
from tradebot.engine import TradingEngine
from tradebot.analyst_consensus import AnalystConsensusTracker
from tradebot.email_report import _daily_and_total_summary, _extract_etrade_position_rows, build_report_html, get_etrade_report_summary
from tradebot.etrade import ETradeError, extract_preview_id
from tradebot.mirror import ETradeMirrorExecutor
from tradebot.earnings import EarningsTracker
from tradebot.macro import MacroTracker
from tradebot.mcp_bridge import analyze as analyze_with_mcp
from tradebot.models import AccountSnapshot, Candidate, CongressTrade, PositionSnapshot
from tradebot.providers import AlpacaBroker, BaseBroker, ProviderError, build_broker
from tradebot.sec import SecTracker


def make_settings(tmp_path: Path) -> Settings:
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "demo"
    # Pin values that changed in the small-account enhancement so existing
    # deterministic tests stay stable against the demo simulator.
    settings.max_total_capital = 0
    settings.max_open_positions = 0
    settings.risk_per_trade_pct = 0.01
    settings.max_position_pct = 0.10
    settings.min_reward_risk = 1.8
    settings.starting_cash = 100_000
    # Pin the price cap so results don't depend on a local .env value
    # (0 means uncapped; the default $10 cap excludes test bars near $10).
    settings.max_stock_price = 0.0
    settings.__post_init__()
    settings.congress_report_urls = []
    # Keep tests offline: auto-discovery of official disclosures hits the network.
    settings.congress_auto_fetch = False
    settings.sec_user_agent = ""
    settings.alpha_vantage_api_key = ""
    settings.polygon_api_key = ""
    settings.analyst_consensus_enabled = False
    settings.market_regime_filter = False
    settings.shadow_mode_strategies = False
    settings.profit_lock_dollars = 0
    settings.congress_override_mode = "auto"
    return settings


def _iso_date(days_offset: int) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=days_offset)).isoformat()


def _slash_date(days_offset: int) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=days_offset)).strftime("%m/%d/%Y")


def test_scan_and_trade_once(tmp_path: Path):
    settings = make_settings(tmp_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=Database(settings.db_path))
    candidates = engine.scan_market()
    assert candidates
    result = engine.trade_once()
    assert "candidates" in result
    snapshot = engine.dashboard_snapshot()
    assert "account" in snapshot
    assert isinstance(snapshot["positions"], list)


def test_dashboard_renders(tmp_path: Path):
    settings = make_settings(tmp_path)
    app = create_app(settings)
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "TradeBot Dashboard" in response.text


def test_extract_etrade_position_rows_parses_gain_fields() -> None:
    payload = {
        "PortfolioResponse": {
            "AccountPortfolio": [
                {
                    "Position": [
                        {
                            "Product": {"symbol": "VTI"},
                            "quantity": 6.7359,
                            "marketValue": 2342.6460,
                            "todayGainLoss": -16.3684,
                            "totalGain": 1580.3060,
                        },
                        {
                            "Product": {"symbol": "MSFT"},
                            "quantity": 5.3819,
                            "marketValue": 2281.8495,
                            "todayGainLoss": 31.7968,
                            "totalGain": 2029.4495,
                        },
                    ]
                }
            ]
        }
    }

    rows = _extract_etrade_position_rows(payload)

    assert rows == [
        {"symbol": "VTI", "quantity": 6.7359, "market_value": 2342.646, "day_gain": -16.3684, "total_gain": 1580.306},
        {"symbol": "MSFT", "quantity": 5.3819, "market_value": 2281.8495, "day_gain": 31.7968, "total_gain": 2029.4495},
    ]


def test_get_etrade_report_summary_uses_balance_and_positions(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, env_name: str) -> None:
            assert env_name == "live"

        def balance(self, account_id_key: str):
            assert account_id_key == "ACCOUNTKEY"
            return {
                "BalanceResponse": {
                    "Computed": {
                        "totalAccountValue": 13308.41687,
                        "cashAvailableForInvestment": 376.28,
                    }
                }
            }

        def positions(self, account_id_key: str):
            assert account_id_key == "ACCOUNTKEY"
            return {
                "PortfolioResponse": {
                    "AccountPortfolio": [
                        {
                            "Position": [
                                {
                                    "Product": {"symbol": "VTI"},
                                    "quantity": 6.7359,
                                    "marketValue": 2342.6460,
                                    "todayGainLoss": -16.3684,
                                    "totalGain": 1580.3060,
                                },
                                {
                                    "Product": {"symbol": "MSFT"},
                                    "quantity": 5.3819,
                                    "marketValue": 2281.8495,
                                    "todayGainLoss": 31.7968,
                                    "totalGain": 2029.4495,
                                },
                            ]
                        }
                    ]
                }
            }

    monkeypatch.setenv("ETRADE_ACCOUNT_ID_KEY", "ACCOUNTKEY")
    monkeypatch.setenv("ETRADE_MIRROR_ENV", "live")
    monkeypatch.setattr("tradebot.email_report.ETradeClient", FakeClient)

    summary = get_etrade_report_summary()

    assert summary is not None
    assert summary["label"] == "E*TRADE"
    assert summary["equity"] == 13308.42
    assert summary["cash"] == 376.28
    assert summary["daily_pnl"] == 15.43
    assert summary["total_pnl"] == 3609.76


def test_build_report_html_includes_etrade_comparison_block() -> None:
    snapshot = {
        "account": {"equity": 1100.0},
        "performance": {"total_pnl": 100.0, "total_return_pct": 10.0},
        "safety_status": {"daily_equity_anchor": 1050.0},
    }
    etrade_summary = {
        "label": "E*TRADE",
        "equity": 13308.42,
        "cash": 376.28,
        "daily_pnl": 15.43,
        "daily_pct": 0.12,
        "total_pnl": 3609.76,
        "total_pct": 37.23,
    }

    html = build_report_html(snapshot, etrade_summary=etrade_summary)

    assert "Account comparison" in html
    assert "TradeBot / Alpaca" in html
    assert "E*TRADE" in html
    assert "$+15.43" in html


def test_daily_report_prefers_broker_previous_close_equity() -> None:
    snapshot = {
        "account": {"equity": 1125.0, "last_equity": 1100.0},
        "performance": {"total_pnl": 125.0, "total_return_pct": 12.5},
        "safety_status": {"daily_equity_anchor": 1125.0},
    }

    summary = _daily_and_total_summary(snapshot)

    assert summary["daily_anchor"] == 1100.0
    assert summary["daily_anchor_source"] == "broker_previous_close"
    assert summary["daily_pnl"] == 25.0
    assert summary["daily_pct"] == 2.27


def test_analyst_consensus_tracker_parses_stockanalysis_forecast_html(tmp_path: Path) -> None:
    tracker = AnalystConsensusTracker(make_settings(tmp_path), Database(tmp_path / "tradebot.db"))
    html = """
    <html>
      <body>
        <p>Price Target: $24.47 (+0.53%)</p>
        <p>Analyst Consensus: Hold</p>
      </body>
    </html>
    """

    parsed = tracker._parse(html)

    assert parsed == {"consensus": "Hold", "target_upside_pct": 0.53}


def test_analyst_consensus_tracker_parses_wrapped_stockanalysis_markup(tmp_path: Path) -> None:
    tracker = AnalystConsensusTracker(make_settings(tmp_path), Database(tmp_path / "tradebot.db"))
    html = """
    <div>Price Target: <span>$24.47 (+0.33%)</span></div>
    <div>Analyst Consensus: <span class="font-bold">Hold</span></div>
    """

    parsed = tracker._parse(html)

    assert parsed == {"consensus": "Hold", "target_upside_pct": 0.33}


def test_analyst_consensus_tracker_ignores_blank_cached_consensus(tmp_path: Path) -> None:
    db = Database(tmp_path / "tradebot.db")
    tracker = AnalystConsensusTracker(make_settings(tmp_path), db)
    db.set_bot_state(
        "analyst_consensus:CWAN",
        json.dumps(
            {
                "symbol": "CWAN",
                "source": "stockanalysis",
                "consensus": "",
                "target_upside_pct": 0.0,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
    )

    assert tracker._load_cached("CWAN") is None


def test_analyst_consensus_tracker_parses_yahoo_quote_recommendation(tmp_path: Path) -> None:
    tracker = AnalystConsensusTracker(make_settings(tmp_path), Database(tmp_path / "tradebot.db"))
    html = """
    <script>
      {"currentPrice":{"raw":23.31,"fmt":"23.31"},
       "targetMeanPrice":{"raw":24.47,"fmt":"24.47"},
       "recommendationMean":{"raw":3.0,"fmt":"3.00"},
       "recommendationKey":"hold"}
    </script>
    """

    parsed = tracker._parse_yahoo_quote(html)

    assert parsed is not None
    assert parsed["consensus"] == "Hold"
    assert round(parsed["target_upside_pct"], 2) == 4.98


def test_analyst_consensus_tracker_prefers_yahoo_quote_before_stockanalysis(tmp_path: Path) -> None:
    tracker = AnalystConsensusTracker(make_settings(tmp_path), Database(tmp_path / "tradebot.db"))

    class FakeResponse:
        status_code = 200
        text = '{"currentPrice":{"raw":10.0},"targetMeanPrice":{"raw":12.0},"recommendationKey":"buy"}'

    class FakeSession:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def get(self, url: str, timeout: int):
            self.urls.append(url)
            return FakeResponse()

    fake_session = FakeSession()
    tracker.session = fake_session

    snapshot = tracker.get("CWAN")

    assert snapshot is not None
    assert snapshot["source"] == "yahoo_finance"
    assert snapshot["source_url"] == "https://finance.yahoo.com/quote/CWAN"
    assert snapshot["consensus"] == "Buy"
    assert fake_session.urls == ["https://finance.yahoo.com/quote/CWAN"]


def test_analyst_consensus_tracker_returns_none_when_fetches_fail(tmp_path: Path) -> None:
    tracker = AnalystConsensusTracker(make_settings(tmp_path), Database(tmp_path / "tradebot.db"))

    class FailingSession:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def get(self, url: str, timeout: int):
            self.urls.append(url)
            raise TimeoutError("slow upstream")

    fake_session = FailingSession()
    tracker.session = fake_session

    snapshot = tracker.get("CWAN")

    assert snapshot is None
    assert fake_session.urls == [
        "https://finance.yahoo.com/quote/CWAN",
        "https://stockanalysis.com/stocks/cwan/forecast/",
    ]


def test_healthcheck_endpoint(tmp_path: Path):
    settings = make_settings(tmp_path)
    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_market_session_status_uses_exchange_calendar_for_holidays_and_early_closes(tmp_path: Path):
    settings = make_settings(tmp_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=Database(settings.db_path))

    holiday = engine._market_session_status(datetime(2026, 12, 25, 15, 0, tzinfo=timezone.utc))
    early_close = engine._market_session_status(datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc))

    assert holiday["is_open"] is False
    assert holiday["next_open"] is not None
    assert early_close["current_session_date"] == "2026-11-27"
    assert early_close["is_early_close"] is True


def test_api_status_reports_auto_trade_settings(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.auto_trade_enabled = True
    settings.auto_trade_interval_minutes = 60
    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/api/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["auto_trade_enabled"] is True
    assert payload["auto_trade_interval_minutes"] == 60
    assert "signal_health" in payload
    assert "signal_refresh_history" in payload
    assert "degraded_mode" in payload
    assert "buying_paused_reason" in payload


def test_settings_prefers_port_env_for_deploys(tmp_path: Path):
    previous_port = os.environ.get("PORT")
    previous_dashboard_port = os.environ.get("DASHBOARD_PORT")
    os.environ["PORT"] = "9000"
    os.environ["DASHBOARD_PORT"] = "8008"
    try:
        settings = Settings(data_dir=tmp_path)
    finally:
        if previous_port is None:
            os.environ.pop("PORT", None)
        else:
            os.environ["PORT"] = previous_port
        if previous_dashboard_port is None:
            os.environ.pop("DASHBOARD_PORT", None)
        else:
            os.environ["DASHBOARD_PORT"] = previous_dashboard_port

    assert settings.dashboard_port == 9000


def test_settings_leaves_scan_universe_empty_when_not_configured(tmp_path: Path):
    previous_scan_universe = os.environ.get("SCAN_UNIVERSE")
    os.environ.pop("SCAN_UNIVERSE", None)
    try:
        settings = Settings(data_dir=tmp_path)
    finally:
        if previous_scan_universe is None:
            os.environ.pop("SCAN_UNIVERSE", None)
        else:
            os.environ["SCAN_UNIVERSE"] = previous_scan_universe

    assert settings.scan_universe == []


def test_settings_accepts_alpaca_api_key_alias(tmp_path: Path):
    previous_key_id = os.environ.get("ALPACA_KEY_ID")
    previous_api_key = os.environ.get("ALPACA_API_KEY")
    os.environ.pop("ALPACA_KEY_ID", None)
    os.environ["ALPACA_API_KEY"] = "alias-key"
    try:
        settings = Settings(data_dir=tmp_path)
    finally:
        if previous_key_id is None:
            os.environ.pop("ALPACA_KEY_ID", None)
        else:
            os.environ["ALPACA_KEY_ID"] = previous_key_id
        if previous_api_key is None:
            os.environ.pop("ALPACA_API_KEY", None)
        else:
            os.environ["ALPACA_API_KEY"] = previous_api_key

    assert settings.alpaca_key_id == "alias-key"


def test_trading_scheduler_runs_callback_once() -> None:
    calls = []
    scheduler = TradingScheduler(3600, lambda: calls.append("tick"))

    result = scheduler.run_cycle()

    assert result is True
    assert calls == ["tick"]


def test_trading_scheduler_reports_callback_failure() -> None:
    errors = []

    def callback() -> None:
        raise RuntimeError("boom")

    scheduler = TradingScheduler(3600, callback, on_error=lambda exc: errors.append(exc))

    result = scheduler.run_cycle()

    assert result is False
    assert len(errors) == 1
    assert str(errors[0]) == "boom"


def test_congress_tracker_parses_house_ptr_text(tmp_path: Path):
    settings = make_settings(tmp_path)
    tracker = CongressTracker(settings, lambda symbols: {symbol: 12.5 for symbol in symbols})
    text = """
    Name: Hon. Example Member
    SoundHound AI, Inc. Class A Common Stock (SOUN) [ST] P 02/10/2026 02/25/2026 $1,001 - $15,000
    Apple Inc. - Common Stock (AAPL) [ST] S 02/10/2026 02/25/2026 $1,001 - $15,000
    """

    trades = tracker.parse_ptr_text(text, "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/example.pdf", "House")

    assert [trade.symbol for trade in trades] == ["SOUN", "AAPL"]
    assert trades[0].member == "Hon. Example Member"
    assert trades[0].side == "buy"
    assert trades[1].side == "sell"


def test_congress_tracker_parses_wrapped_ptr_rows(tmp_path: Path):
    settings = make_settings(tmp_path)
    tracker = CongressTracker(settings, lambda symbols: {symbol: 5.0 for symbol in symbols})
    # Mirrors real pypdf extraction: asset names wrap onto the lines before the
    # [ST] marker and dollar ranges wrap onto the line after the dates.
    text = """
    Name: Hon. Example Member
    F      S     : New
    Adobe Inc. - Common Stock (ADBE)
    [ST]
    S (partial) 05/15/202606/05/2026$1,001 - $15,000
    F      S     : New
    SP Farmers & Merchants Bancorp, Inc.
    (FMAO) [ST]
    P 06/04/202606/04/2026$15,001 -
    $50,000
    F      S     : New
    Coterra Energy Inc. Common Stock
    (CTRA) [ST]
    E 05/08/202606/05/2026$1,001 - $15,000
    """

    trades = tracker.parse_ptr_text(text, "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/example.pdf", "House")

    assert [trade.symbol for trade in trades] == ["ADBE", "FMAO"]
    assert trades[0].side == "sell"
    assert trades[0].asset == "Adobe Inc. - Common Stock"
    assert trades[1].side == "buy"
    assert trades[1].amount_range == "$15,001 - $50,000"
    assert trades[1].trade_date == "06/04/2026"


def test_inverse_hedge_headroom_blocks_same_index_stacking(tmp_path: Path):
    import math

    settings = make_settings(tmp_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=Database(settings.db_path))

    # SPXU and SH both short the S&P — holding one blocks the other outright.
    assert engine._inverse_hedge_headroom("SPXU", [("SH", 200.0)], 1000.0) == 0.0
    # A second distinct index is allowed up to the aggregate exposure cap.
    assert engine._inverse_hedge_headroom("SDOW", [("SQQQ", 250.0)], 1000.0) == 50.0
    # The distinct-position limit (default 2) blocks a third inverse fund.
    assert engine._inverse_hedge_headroom("SH", [("SQQQ", 100.0), ("SDOW", 100.0)], 1000.0) == 0.0
    # Regular stocks are never constrained by hedge limits.
    assert math.isinf(engine._inverse_hedge_headroom("SOFI", [("SQQQ", 900.0)], 1000.0))
    # Same-bucket stacking stays blocked even with the caps disabled.
    settings.max_inverse_positions = 0
    settings.max_inverse_exposure_pct = 0
    assert engine._inverse_hedge_headroom("SPXS", [("SPXU", 50.0)], 1000.0) == 0.0
    assert math.isinf(engine._inverse_hedge_headroom("SDOW", [("SPXU", 50.0)], 1000.0))


def test_regime_persistence_gates_inverse_confirmation(tmp_path: Path):
    settings = make_settings(tmp_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=Database(settings.db_path))
    now = datetime.now(timezone.utc)

    regime = {"enabled": False}
    engine._update_regime_persistence(regime, now=now)
    assert regime["inverse_buys_confirmed"] is True

    # First weak reading starts the clock but does not confirm hedges.
    regime = {"enabled": True, "allow_long_buys": False, "state": "weak"}
    engine._update_regime_persistence(regime, now=now)
    assert regime["inverse_buys_confirmed"] is False

    # Weakness persisting past the window confirms hedge entries.
    regime = {"enabled": True, "allow_long_buys": False, "state": "weak"}
    engine._update_regime_persistence(regime, now=now + timedelta(hours=19))
    assert regime["inverse_buys_confirmed"] is True

    # Missing data never confirms hedges.
    regime = {"enabled": True, "allow_long_buys": False, "state": "missing"}
    engine._update_regime_persistence(regime, now=now + timedelta(hours=20))
    assert regime["inverse_buys_confirmed"] is False

    # An uptrend reading resets the clock for the next weak stretch.
    regime = {"enabled": True, "allow_long_buys": True, "state": "uptrend"}
    engine._update_regime_persistence(regime, now=now + timedelta(hours=21))
    assert regime["inverse_buys_confirmed"] is False
    regime = {"enabled": True, "allow_long_buys": False, "state": "weak"}
    engine._update_regime_persistence(regime, now=now + timedelta(hours=22))
    assert regime["inverse_buys_confirmed"] is False


def test_earnings_blackout_blocks_imminent_reports(tmp_path: Path):
    settings = make_settings(tmp_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=Database(settings.db_path))

    assert engine._in_earnings_blackout({"has_upcoming_earnings": 1.0, "days_until_earnings": 1.0}) is True
    assert engine._in_earnings_blackout({"has_upcoming_earnings": 1.0, "days_until_earnings": 5.0}) is False
    assert engine._in_earnings_blackout({"has_upcoming_earnings": 0.0, "days_until_earnings": 22.0}) is False
    settings.earnings_blackout_days = 0
    assert engine._in_earnings_blackout({"has_upcoming_earnings": 1.0, "days_until_earnings": 0.0}) is False


def test_congress_refresh_keeps_all_trades_when_price_cap_disabled(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.congress_max_price = 0
    settings.congress_report_urls = ["https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/example.pdf"]

    def fake_prices(symbols: list[str]) -> dict[str, float]:
        return {"AAA": 50.0}  # BBB has no quote at all

    tracker = CongressTracker(settings, fake_prices)

    def fake_fetch(url: str, fallback_member: str | None = None) -> list[CongressTrade]:
        return [
            CongressTrade(
                member="Hon. Example Member",
                chamber="House",
                symbol=symbol,
                asset=f"{symbol} Common Stock",
                side="buy",
                trade_date="06/01/2026",
                filed_date="06/05/2026",
                amount_range="$1,001 - $15,000",
                source_url=url,
            )
            for symbol in ("AAA", "BBB")
        ]

    tracker._fetch_report = fake_fetch  # type: ignore[method-assign]

    trades = tracker.refresh()

    assert [trade.symbol for trade in trades] == ["AAA", "BBB"]
    assert all(trade.under_price_cap for trade in trades)
    assert trades[0].current_price == 50.0
    assert trades[1].current_price is None


def test_parse_house_index_filters_to_recent_ptrs():
    from datetime import date

    from tradebot.congress import parse_house_index

    index_text = (
        "Prefix\tLast\tFirst\tSuffix\tFilingType\tStateDst\tYear\tFilingDate\tDocID\n"
        "Hon.\tSuozzi\tThomas\t\tP\tNY03\t2026\t6/9/2026\t20034747\n"
        "Hon.\tOld\tMember\t\tP\tNY01\t2026\t1/2/2026\t20030001\n"
        "Hon.\tBiggs\tSheri\t\tP\tSC03\t2026\t6/9/2026\t20034496\n"
        "\tAaron\tRichard\t\tW\tMI04\t2026\t4/15/2026\t8068\n"
    )

    entries = parse_house_index(index_text, cutoff=date(2026, 5, 1))

    assert [entry[1] for entry in entries] == ["20034747", "20034496"]
    assert entries[0][0] == "Hon. Thomas Suozzi"
    assert entries[0][2] == date(2026, 6, 9)


def test_congress_tracker_parses_senate_ptr_html(tmp_path: Path):
    settings = make_settings(tmp_path)
    tracker = CongressTracker(settings, lambda symbols: {symbol: 5.0 for symbol in symbols})
    html = """
    <table class="table">
      <thead>
        <tr><th>#</th><th>Transaction Date</th><th>Owner</th><th>Ticker</th><th>Asset Name</th>
        <th>Asset Type</th><th>Type</th><th>Amount</th><th>Comment</th></tr>
      </thead>
      <tbody>
        <tr><td>1</td><td>06/05/2026</td><td>Self</td><td><a href="#">PTON</a></td>
        <td>Peloton Interactive, Inc. - Common Stock</td><td>Stock</td><td>Sale (Full)</td>
        <td>$1,001 - $15,000</td><td>--</td></tr>
        <tr><td>2</td><td>06/04/2026</td><td>Spouse</td><td>--</td>
        <td>City Muni Bond</td><td>Municipal Security</td><td>Purchase</td>
        <td>$1,001 - $15,000</td><td>--</td></tr>
        <tr><td>3</td><td>06/03/2026</td><td>Self</td><td><a href="#">AAPL</a></td>
        <td>Apple Calls</td><td>Stock Option</td><td>Purchase</td>
        <td>$1,001 - $15,000</td><td>--</td></tr>
        <tr><td>4</td><td>06/02/2026</td><td>Self</td><td><a href="#">QQQ</a></td>
        <td>Invesco QQQ Trust</td><td>Stock</td><td>Exchange</td>
        <td>$1,001 - $15,000</td><td>--</td></tr>
        <tr><td>5</td><td>06/01/2026</td><td>Self</td><td><a href="#">SOUN</a></td>
        <td>SoundHound AI, Inc.</td><td>Stock</td><td>Purchase</td>
        <td>$1,001 - $15,000</td><td>--</td></tr>
      </tbody>
    </table>
    """

    trades = tracker.parse_senate_ptr_html(
        html,
        "https://efdsearch.senate.gov/search/view/ptr/example/",
        "James Banks",
        "06/07/2026",
    )

    assert [(trade.symbol, trade.side) for trade in trades] == [("PTON", "sell"), ("SOUN", "buy")]
    assert trades[0].chamber == "Senate"
    assert trades[0].member == "James Banks"
    assert trades[0].filed_date == "06/07/2026"
    assert trades[0].trade_date == "06/05/2026"


def test_refresh_congress_trades_filters_to_under_price_cap(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.congress_max_price = 20
    settings.congress_report_urls = ["https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/example.pdf"]
    broker = build_broker(settings)
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=broker, db=db)

    class FakeTracker:
        def __init__(self, _settings, _price_lookup) -> None:
            pass

        def refresh(self) -> list[CongressTrade]:
            return [
                CongressTrade(
                    member="Hon. Example Member",
                    chamber="House",
                    symbol="SOUN",
                    asset="SoundHound AI, Inc. Class A Common Stock",
                    side="buy",
                    trade_date="02/10/2026",
                    filed_date="02/25/2026",
                    amount_range="$1,001 - $15,000",
                    source_url="https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/example.pdf",
                    current_price=13.25,
                    under_price_cap=True,
                )
            ]

    import tradebot.engine as engine_module

    original_tracker = engine_module.CongressTracker
    engine_module.CongressTracker = FakeTracker
    try:
        result = engine.refresh_congress_trades()
    finally:
        engine_module.CongressTracker = original_tracker

    snapshot = engine.dashboard_snapshot()

    assert result
    assert snapshot["congress_trades"][0]["symbol"] == "SOUN"
    assert snapshot["congress_trades"][0]["under_price_cap"] == 1


def test_scan_market_includes_congress_external_inputs_in_candidate_metrics(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.congress_report_urls = ["https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/example.pdf"]
    db = Database(settings.db_path)
    db.replace_congress_trades(
        [
            {
                "member": "Hon. Example Member",
                "chamber": "House",
                "symbol": "HOOD",
                "asset": "Robinhood Markets, Inc.",
                "side": "buy",
                "trade_date": _slash_date(-7),
                "filed_date": _slash_date(-1),
                "amount_range": "$1,001 - $15,000",
                "source_url": "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/example.pdf",
                "current_price": 18.0,
                "under_price_cap": True,
            }
        ]
    )
    now = datetime.now(timezone.utc).isoformat()
    db.update_signal_status(
        "congress",
        "ok",
        last_attempt_at=now,
        last_success_at=now,
        records_count=1,
    )
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=db)

    candidates = engine.scan_market()
    hood = next((candidate for candidate in candidates if candidate.symbol == "HOOD"), None)

    assert hood is not None
    assert hood.metrics["congress_buy_count"] == 1.0
    assert hood.metrics["congress_sell_count"] == 0.0
    assert "decision_support" in hood.analyst_scores
    assert hood.signal_usage["congress"] == "active"


def test_trade_once_with_congress_refresh_runs_refresh_first(tmp_path: Path):
    settings = make_settings(tmp_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=Database(settings.db_path))
    calls: list[str] = []

    def fake_refresh():
        calls.append("refresh")
        return []

    def fake_refresh_sec():
        calls.append("refresh-sec")
        return []

    def fake_refresh_earnings():
        calls.append("refresh-earnings")
        return []

    def fake_refresh_macro():
        calls.append("refresh-macro")
        return []

    def fake_trade_once():
        calls.append("trade")
        return {"sold": [], "bought": [], "candidates": []}

    def fake_refresh_all():
        calls.append("refresh")
        calls.append("refresh-sec")
        calls.append("refresh-earnings")
        calls.append("refresh-macro")
        return {"congress": [], "sec": [], "earnings": [], "macro": []}

    engine.refresh_all_signals = fake_refresh_all  # type: ignore[method-assign]
    engine.trade_once = fake_trade_once  # type: ignore[method-assign]

    result = engine.trade_once_with_congress_refresh()

    assert calls == ["refresh", "refresh-sec", "refresh-earnings", "refresh-macro", "trade"]
    assert result == {"sold": [], "bought": [], "candidates": []}


def test_trade_once_with_signal_refresh_skips_refresh_when_market_closed(tmp_path: Path):
    settings = make_settings(tmp_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=Database(settings.db_path))
    settings.broker_mode = "paper"  # non-demo so the market-hours gate applies
    calls: list[str] = []

    engine.refresh_all_signals = lambda: calls.append("refresh") or {}  # type: ignore[method-assign]
    engine.trade_once = lambda: calls.append("trade") or {"sold": [], "bought": [], "candidates": []}  # type: ignore[method-assign]
    engine._market_is_closed = lambda: True  # type: ignore[method-assign]

    engine.trade_once_with_signal_refresh()
    engine.trade_once_with_signal_refresh()

    assert calls == ["trade", "trade"]
    pause_events = [
        event
        for event in engine.db.recent_audit_events(20)
        if "signal refresh paused" in str(event.get("message", ""))
    ]
    assert len(pause_events) == 1

    engine._market_is_closed = lambda: False  # type: ignore[method-assign]
    engine.trade_once_with_signal_refresh()

    assert calls == ["trade", "trade", "refresh", "trade"]


def test_dashboard_trade_once_refreshes_signals_before_trading(tmp_path: Path):
    settings = make_settings(tmp_path)
    app = create_app(settings)
    client = TestClient(app)
    calls: list[str] = []

    def fake_trade_once_with_signal_refresh():
        calls.append("trade")
        return {"sold": [], "bought": [], "candidates": []}

    app.state.engine.trade_once_with_signal_refresh = fake_trade_once_with_signal_refresh  # type: ignore[method-assign]

    response = client.post("/trade-once", follow_redirects=False)

    assert response.status_code == 303
    assert calls == ["trade"]


def test_dashboard_mirror_retry_picks_up_pending_trade_after_reauth(tmp_path: Path):
    settings = make_settings(tmp_path)
    db = Database(settings.db_path)

    class FakeMirror:
        def __init__(self) -> None:
            self._status = {
                "enabled": True,
                "ready": False,
                "env": "live",
                "preview_only": False,
                "account_id_key": "ABC123456",
                "last_trade_id": 101,
                "last_result": "retry pending UAMY",
                "last_error": "500 service unavailable",
                "auth_expired": False,
                "recovery_hint": "",
            }

        def enabled(self) -> bool:
            return True

        def status(self):
            return dict(self._status)

    fake_mirror = FakeMirror()
    db.record_trade("UAMY", "sell", 2, 9.91, "filled", "stop hit")

    assert mirror_retry_needed(db, fake_mirror) is True

    fake_mirror._status["last_trade_id"] = 102
    fake_mirror._status["last_result"] = "placed UAMY SELL x2"
    fake_mirror._status["last_error"] = ""
    fake_mirror._status["ready"] = True

    assert mirror_retry_needed(db, fake_mirror) is False


def test_refresh_all_signals_runs_each_source(tmp_path: Path):
    settings = make_settings(tmp_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=Database(settings.db_path))
    calls: list[str] = []

    engine.refresh_congress_trades = lambda: calls.append("congress") or []  # type: ignore[method-assign]
    engine.refresh_sec_filings = lambda: calls.append("sec") or []  # type: ignore[method-assign]
    engine.refresh_earnings_events = lambda: calls.append("earnings") or []  # type: ignore[method-assign]
    engine.refresh_macro_events = lambda: calls.append("macro") or []  # type: ignore[method-assign]

    result = engine.refresh_all_signals()

    assert calls == ["congress", "sec", "earnings", "macro"]
    assert result == {"congress": [], "sec": [], "earnings": [], "macro": []}


def test_mcp_bridge_includes_decision_support_score() -> None:
    analysis = analyze_with_mcp(
        {
            "latest": 12.0,
            "sma10": 11.7,
            "sma20": 11.4,
            "sma50": 10.9,
            "rsi14": 58.0,
            "momentum5": 4.0,
            "momentum20": 12.0,
            "volatility20": 24.0,
            "atr": 0.45,
            "atr_pct": 3.8,
            "avg_dollar_volume": 4_500_000,
            "swing_high20": 12.4,
            "swing_low20": 9.8,
            "reward_risk": 2.4,
            "min_reward_risk": 1.8,
        }
    )

    assert "decision_support" in analysis
    assert analysis["decision_support"][0] > 50
    assert analysis["decision_support"][1]


def test_decision_support_rewards_recent_congress_buy_signal() -> None:
    bullish = analyze_with_mcp(
        {
            "latest": 12.0,
            "sma10": 11.7,
            "sma20": 11.4,
            "sma50": 10.9,
            "rsi14": 58.0,
            "momentum5": 4.0,
            "momentum20": 12.0,
            "volatility20": 24.0,
            "atr": 0.45,
            "atr_pct": 3.8,
            "avg_dollar_volume": 4_500_000,
            "swing_high20": 12.4,
            "swing_low20": 9.8,
            "reward_risk": 2.4,
            "min_reward_risk": 1.8,
            "congress_buy_count": 2.0,
            "congress_sell_count": 0.0,
            "congress_net_count": 2.0,
            "days_since_congress_trade": 5.0,
        }
    )
    bearish = analyze_with_mcp(
        {
            "latest": 12.0,
            "sma10": 11.7,
            "sma20": 11.4,
            "sma50": 10.9,
            "rsi14": 58.0,
            "momentum5": 4.0,
            "momentum20": 12.0,
            "volatility20": 24.0,
            "atr": 0.45,
            "atr_pct": 3.8,
            "avg_dollar_volume": 4_500_000,
            "swing_high20": 12.4,
            "swing_low20": 9.8,
            "reward_risk": 2.4,
            "min_reward_risk": 1.8,
            "congress_buy_count": 0.0,
            "congress_sell_count": 2.0,
            "congress_net_count": -2.0,
            "days_since_congress_trade": 5.0,
        }
    )

    assert bullish["decision_support"][0] > bearish["decision_support"][0]
    assert any("congress" in reason for reason in bullish["decision_support"][1])


def test_decision_support_respects_zero_external_weight() -> None:
    weighted = analyze_with_mcp(
        {
            "latest": 12.0,
            "sma10": 11.7,
            "sma20": 11.4,
            "sma50": 10.9,
            "rsi14": 58.0,
            "momentum5": 4.0,
            "momentum20": 12.0,
            "volatility20": 24.0,
            "atr": 0.45,
            "atr_pct": 3.8,
            "avg_dollar_volume": 4_500_000,
            "swing_high20": 12.4,
            "swing_low20": 9.8,
            "reward_risk": 2.4,
            "min_reward_risk": 1.8,
            "sec_form4_count": 1.0,
            "sec_disclosure_count": 1.0,
            "sec_offering_filing_count": 1.0,
            "days_since_sec_filing": 3.0,
            "sec_weight": 1.0,
        }
    )
    unweighted = analyze_with_mcp(
        {
            "latest": 12.0,
            "sma10": 11.7,
            "sma20": 11.4,
            "sma50": 10.9,
            "rsi14": 58.0,
            "momentum5": 4.0,
            "momentum20": 12.0,
            "volatility20": 24.0,
            "atr": 0.45,
            "atr_pct": 3.8,
            "avg_dollar_volume": 4_500_000,
            "swing_high20": 12.4,
            "swing_low20": 9.8,
            "reward_risk": 2.4,
            "min_reward_risk": 1.8,
            "sec_form4_count": 1.0,
            "sec_disclosure_count": 1.0,
            "sec_offering_filing_count": 1.0,
            "days_since_sec_filing": 3.0,
            "sec_weight": 0.0,
        }
    )

    assert weighted["decision_support"][0] < unweighted["decision_support"][0]
    assert not any("SEC" in reason for reason in unweighted["decision_support"][1])


def test_decision_support_penalizes_recent_sec_offering_signal() -> None:
    clean = analyze_with_mcp(
        {
            "latest": 12.0,
            "sma10": 11.7,
            "sma20": 11.4,
            "sma50": 10.9,
            "rsi14": 58.0,
            "momentum5": 4.0,
            "momentum20": 12.0,
            "volatility20": 24.0,
            "atr": 0.45,
            "atr_pct": 3.8,
            "avg_dollar_volume": 4_500_000,
            "swing_high20": 12.4,
            "swing_low20": 9.8,
            "reward_risk": 2.4,
            "min_reward_risk": 1.8,
            "sec_form4_count": 1.0,
            "sec_disclosure_count": 1.0,
            "sec_offering_filing_count": 0.0,
            "days_since_sec_filing": 3.0,
        }
    )
    diluted = analyze_with_mcp(
        {
            "latest": 12.0,
            "sma10": 11.7,
            "sma20": 11.4,
            "sma50": 10.9,
            "rsi14": 58.0,
            "momentum5": 4.0,
            "momentum20": 12.0,
            "volatility20": 24.0,
            "atr": 0.45,
            "atr_pct": 3.8,
            "avg_dollar_volume": 4_500_000,
            "swing_high20": 12.4,
            "swing_low20": 9.8,
            "reward_risk": 2.4,
            "min_reward_risk": 1.8,
            "sec_form4_count": 1.0,
            "sec_disclosure_count": 1.0,
            "sec_offering_filing_count": 1.0,
            "days_since_sec_filing": 3.0,
        }
    )

    assert clean["decision_support"][0] > diluted["decision_support"][0]
    assert any("offering" in reason for reason in diluted["decision_support"][1])


def test_decision_support_penalizes_near_term_earnings() -> None:
    calm = analyze_with_mcp(
        {
            "latest": 12.0,
            "sma10": 11.7,
            "sma20": 11.4,
            "sma50": 10.9,
            "rsi14": 58.0,
            "momentum5": 4.0,
            "momentum20": 12.0,
            "volatility20": 24.0,
            "atr": 0.45,
            "atr_pct": 3.8,
            "avg_dollar_volume": 4_500_000,
            "swing_high20": 12.4,
            "swing_low20": 9.8,
            "reward_risk": 2.4,
            "min_reward_risk": 1.8,
            "has_upcoming_earnings": 1.0,
            "days_until_earnings": 10.0,
        }
    )
    imminent = analyze_with_mcp(
        {
            "latest": 12.0,
            "sma10": 11.7,
            "sma20": 11.4,
            "sma50": 10.9,
            "rsi14": 58.0,
            "momentum5": 4.0,
            "momentum20": 12.0,
            "volatility20": 24.0,
            "atr": 0.45,
            "atr_pct": 3.8,
            "avg_dollar_volume": 4_500_000,
            "swing_high20": 12.4,
            "swing_low20": 9.8,
            "reward_risk": 2.4,
            "min_reward_risk": 1.8,
            "has_upcoming_earnings": 1.0,
            "days_until_earnings": 1.0,
        }
    )

    assert calm["decision_support"][0] > imminent["decision_support"][0]
    assert any("earnings" in reason for reason in imminent["decision_support"][1])


def test_decision_support_penalizes_near_term_macro_event() -> None:
    quiet = analyze_with_mcp(
        {
            "latest": 12.0,
            "sma10": 11.7,
            "sma20": 11.4,
            "sma50": 10.9,
            "rsi14": 58.0,
            "momentum5": 4.0,
            "momentum20": 12.0,
            "volatility20": 24.0,
            "atr": 0.45,
            "atr_pct": 3.8,
            "avg_dollar_volume": 4_500_000,
            "swing_high20": 12.4,
            "swing_low20": 9.8,
            "reward_risk": 2.4,
            "min_reward_risk": 1.8,
            "has_near_macro_event": 1.0,
            "days_until_macro_event": 6.0,
            "near_fomc_count": 0.0,
        }
    )
    event_risk = analyze_with_mcp(
        {
            "latest": 12.0,
            "sma10": 11.7,
            "sma20": 11.4,
            "sma50": 10.9,
            "rsi14": 58.0,
            "momentum5": 4.0,
            "momentum20": 12.0,
            "volatility20": 24.0,
            "atr": 0.45,
            "atr_pct": 3.8,
            "avg_dollar_volume": 4_500_000,
            "swing_high20": 12.4,
            "swing_low20": 9.8,
            "reward_risk": 2.4,
            "min_reward_risk": 1.8,
            "has_near_macro_event": 1.0,
            "days_until_macro_event": 1.0,
            "near_fomc_count": 1.0,
        }
    )

    assert quiet["decision_support"][0] > event_risk["decision_support"][0]
    assert any("macro" in reason.lower() or "fomc" in reason.lower() for reason in event_risk["decision_support"][1])


def test_sec_tracker_extracts_recent_interesting_forms(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.sec_user_agent = "TradeBot test@example.com"
    tracker = SecTracker(settings)

    def fake_get_json(url: str) -> dict:
        if url.endswith("company_tickers.json"):
            return {"0": {"ticker": "HOOD", "cik_str": 1783879}}
        return {
            "filings": {
                "recent": {
                    "form": ["4", "8-K", "424B5", "SC 13G"],
                    "filingDate": [_iso_date(-5), _iso_date(-4), _iso_date(-3), _iso_date(-2)],
                    "accessionNumber": [
                        "0001783879-26-000001",
                        "0001783879-26-000002",
                        "0001783879-26-000003",
                        "0001783879-26-000004",
                    ],
                    "primaryDocument": ["x1.xml", "x2.htm", "x3.htm", "x4.htm"],
                }
            }
        }

    tracker._get_json = fake_get_json  # type: ignore[method-assign]

    filings = tracker.refresh(["HOOD"])

    assert [filing.form for filing in filings] == ["4", "8-K", "424B5"]
    assert all(filing.symbol == "HOOD" for filing in filings)


def test_earnings_tracker_filters_to_requested_symbols_and_window(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.alpha_vantage_api_key = "demo"
    settings.earnings_signal_window_days = 21
    tracker = EarningsTracker(settings)
    csv_text = f"""symbol,name,reportDate,fiscalDateEnding,estimate,currency,reportTime
HOOD,Robinhood,{_iso_date(5)},2025-12-31,0.12,USD,post-market
AAPL,Apple,{_iso_date(40)},2025-12-31,1.23,USD,post-market
"""

    events = tracker._parse_csv(csv_text, ["HOOD"])

    assert len(events) == 1
    assert events[0].symbol == "HOOD"


def test_macro_tracker_parses_fomc_dates(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    tracker = MacroTracker(settings)
    fomc_html = (
        '<html><body>\n'
        '<div>2030 FOMC Meetings</div>\n'
        '<div class="fomc-meeting__month">June</div>\n'
        '<div class="fomc-meeting__date">15-16</div>\n'
        '<div class="fomc-meeting__month">July</div>\n'
        '<div class="fomc-meeting__date">28-29</div>\n'
        '</body></html>'
    )

    # Patch _get_text so we can inject HTML without hitting the network.
    tracker._get_text = lambda url: fomc_html  # type: ignore[method-assign]

    events = tracker.refresh()

    assert len(events) == 2
    assert all(event.event_type == "fomc" for event in events)
    assert events[0].event_date == "2030-06-16"


def test_refresh_sec_filings_stores_symbol_signal_inputs(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.sec_user_agent = "TradeBot test@example.com"
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=db)

    class FakeSecTracker:
        def __init__(self, _settings) -> None:
            pass

        def refresh(self, symbols: list[str]):
            return [
                type(
                    "SecFilingStub",
                    (),
                    {
                        "__dict__": {
                            "symbol": "HOOD",
                            "cik": "0001783879",
                            "form": "4",
                                "filing_date": _iso_date(-5),
                            "accession_number": "0001783879-26-000001",
                            "primary_document": "x1.xml",
                            "sec_url": "https://www.sec.gov/Archives/edgar/data/1783879/000178387926000001/x1.xml",
                        }
                    },
                )()
            ]

    import tradebot.engine as engine_module

    original_tracker = engine_module.SecTracker
    engine_module.SecTracker = FakeSecTracker
    try:
        result = engine.refresh_sec_filings()
    finally:
        engine_module.SecTracker = original_tracker

    signal = db.sec_signal_for_symbol("HOOD", settings.sec_signal_window_days)

    assert result
    assert signal["sec_form4_count"] == 1.0
    assert signal["sec_offering_filing_count"] == 0.0


def test_refresh_earnings_events_stores_symbol_signal_inputs(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.alpha_vantage_api_key = "demo"
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=db)

    class FakeEarningsTracker:
        def __init__(self, _settings) -> None:
            pass

        def refresh(self, symbols: list[str]):
            return [
                type(
                    "EarningsEventStub",
                    (),
                    {
                        "__dict__": {
                            "symbol": "HOOD",
                                "earnings_date": _iso_date(5),
                            "report_time": "post-market",
                            "fiscal_date_ending": "2025-12-31",
                            "estimate": "0.12",
                            "currency": "USD",
                        }
                    },
                )()
            ]

    import tradebot.engine as engine_module

    original_tracker = engine_module.EarningsTracker
    engine_module.EarningsTracker = FakeEarningsTracker
    try:
        result = engine.refresh_earnings_events()
    finally:
        engine_module.EarningsTracker = original_tracker

    signal = db.earnings_signal_for_symbol("HOOD", settings.earnings_signal_window_days)

    assert result
    assert signal["has_upcoming_earnings"] == 1.0


def test_refresh_macro_events_stores_global_signal_inputs(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=db)

    class FakeMacroTracker:
        def __init__(self, _settings, **kwargs) -> None:
            pass

        def refresh(self):
            return [
                type(
                    "MacroEventStub",
                    (),
                    {"__dict__": {"event_type": "fomc", "event_date": (datetime.now(timezone.utc).date() + timedelta(days=2)).isoformat(), "source": "https://www.federalreserve.gov/"}},
                )()
            ]

    import tradebot.engine as engine_module

    original_tracker = engine_module.MacroTracker
    engine_module.MacroTracker = FakeMacroTracker
    try:
        result = engine.refresh_macro_events()
    finally:
        engine_module.MacroTracker = original_tracker

    signal = db.macro_signal(settings.macro_signal_window_days)

    assert result
    assert signal["has_near_macro_event"] == 1.0
    assert signal["near_fomc_count"] == 1.0


def test_refresh_source_failure_marks_degraded_mode_without_raising(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.sec_user_agent = "TradeBot test@example.com"
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=Database(settings.db_path))

    class FailingSecTracker:
        def __init__(self, _settings) -> None:
            pass

        def refresh(self, symbols: list[str]):
            raise RuntimeError("sec feed offline")

    import tradebot.engine as engine_module

    original_tracker = engine_module.SecTracker
    engine_module.SecTracker = FailingSecTracker
    try:
        result = engine.refresh_sec_filings()
    finally:
        engine_module.SecTracker = original_tracker

    status = engine.dashboard_snapshot()["signal_health"]["sec"]

    assert result == []
    assert status["status"] == "error"
    assert engine.degraded_mode() is True


def test_refresh_source_respects_backoff_and_skips_retry(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.sec_user_agent = "TradeBot test@example.com"
    settings.sec_retry_minutes = 15
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=Database(settings.db_path))
    calls = {"count": 0}

    class FailingSecTracker:
        def __init__(self, _settings) -> None:
            pass

        def refresh(self, symbols: list[str]):
            calls["count"] += 1
            raise RuntimeError("sec feed offline")

    import tradebot.engine as engine_module

    original_tracker = engine_module.SecTracker
    engine_module.SecTracker = FailingSecTracker
    try:
        first = engine.refresh_sec_filings()
        second = engine.refresh_sec_filings()
    finally:
        engine_module.SecTracker = original_tracker

    status = engine.dashboard_snapshot()["signal_health"]["sec"]

    assert first == []
    assert second == []
    assert calls["count"] == 1
    assert status["status"] == "backoff"
    assert status["in_backoff"] is True


def test_override_ignore_backoff_retries_source(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.sec_user_agent = "TradeBot test@example.com"
    settings.sec_retry_minutes = 15
    settings.sec_override_mode = "ignore-backoff"
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=Database(settings.db_path))
    calls = {"count": 0}

    class FailingSecTracker:
        def __init__(self, _settings) -> None:
            pass

        def refresh(self, symbols: list[str]):
            calls["count"] += 1
            raise RuntimeError("sec feed offline")

    import tradebot.engine as engine_module

    original_tracker = engine_module.SecTracker
    engine_module.SecTracker = FailingSecTracker
    try:
        engine.refresh_sec_filings()
        engine.refresh_sec_filings()
    finally:
        engine_module.SecTracker = original_tracker

    status = engine.dashboard_snapshot()["signal_health"]["sec"]

    assert calls["count"] == 2
    assert status["override_mode"] == "ignore-backoff"


def test_signal_refresh_history_records_success_and_failure(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.sec_user_agent = "TradeBot test@example.com"
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=Database(settings.db_path))

    class FailingSecTracker:
        def __init__(self, _settings) -> None:
            pass

        def refresh(self, symbols: list[str]):
            raise RuntimeError("sec feed offline")

    import tradebot.engine as engine_module

    original_tracker = engine_module.SecTracker
    engine_module.SecTracker = FailingSecTracker
    try:
        engine.refresh_sec_filings()
    finally:
        engine_module.SecTracker = original_tracker

    history = engine.dashboard_snapshot()["signal_refresh_history"]

    assert history
    assert history[0]["source"] == "sec"
    assert history[0]["status"] == "error"
    assert history[0]["failure_count"] == 1


def test_signal_refresh_history_records_disabled_source(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=Database(settings.db_path))

    engine.refresh_congress_trades()
    history = engine.dashboard_snapshot()["signal_refresh_history"]

    assert history
    assert history[0]["source"] == "congress"
    assert history[0]["status"] == "disabled"


def test_signal_health_respects_custom_freshness_threshold(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.sec_user_agent = "TradeBot test@example.com"
    settings.sec_freshness_hours = 1
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=Database(settings.db_path))
    stale_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    engine.db.update_signal_status(
        "sec",
        "ok",
        last_attempt_at=stale_time,
        last_success_at=stale_time,
        records_count=1,
    )

    status = engine.dashboard_snapshot()["signal_health"]["sec"]

    assert status["stale"] is True
    assert engine.degraded_mode() is True


def test_stale_signal_is_ignored_in_candidate_scoring(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.congress_report_urls = ["https://example.com/report.pdf"]
    settings.congress_freshness_hours = 1
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=db)
    db.replace_congress_trades(
        [
            {
                "member": "Hon. Example Member",
                "chamber": "House",
                "symbol": "HOOD",
                "asset": "Robinhood Markets, Inc.",
                "side": "buy",
                "trade_date": _slash_date(-7),
                "filed_date": _slash_date(-1),
                "amount_range": "$1,001 - $15,000",
                "source_url": "https://example.com/report.pdf",
                "current_price": 18.0,
                "under_price_cap": True,
            }
        ]
    )
    stale_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    db.update_signal_status(
        "congress",
        "ok",
        last_attempt_at=stale_time,
        last_success_at=stale_time,
        records_count=1,
    )

    candidates = engine.scan_market()
    hood = next((candidate for candidate in candidates if candidate.symbol == "HOOD"), None)

    assert hood is not None
    assert hood.signal_usage["congress"] == "stale"
    assert hood.metrics["congress_weight"] == 0.0
    assert hood.metrics["congress_buy_count"] == 0.0


def test_zero_weight_signal_is_reported_and_ignored(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.congress_report_urls = ["https://example.com/report.pdf"]
    settings.decision_support_congress_weight = 0.0
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=db)
    db.replace_congress_trades(
        [
            {
                "member": "Hon. Example Member",
                "chamber": "House",
                "symbol": "HOOD",
                "asset": "Robinhood Markets, Inc.",
                "side": "buy",
                "trade_date": _slash_date(-7),
                "filed_date": _slash_date(-1),
                "amount_range": "$1,001 - $15,000",
                "source_url": "https://example.com/report.pdf",
                "current_price": 18.0,
                "under_price_cap": True,
            }
        ]
    )
    now = datetime.now(timezone.utc).isoformat()
    db.update_signal_status(
        "congress",
        "ok",
        last_attempt_at=now,
        last_success_at=now,
        records_count=1,
    )

    candidates = engine.scan_market()
    hood = next((candidate for candidate in candidates if candidate.symbol == "HOOD"), None)

    assert hood is not None
    assert hood.signal_usage["congress"] == "weight=0"
    assert hood.metrics["congress_weight"] == 0.0
    assert hood.metrics["congress_buy_count"] == 0.0


def test_low_confidence_signal_is_ignored_and_marks_degraded_mode(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.congress_report_urls = ["https://example.com/report.pdf"]
    settings.congress_min_records = 2
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=db)
    db.replace_congress_trades(
        [
            {
                "member": "Hon. Example Member",
                "chamber": "House",
                "symbol": "HOOD",
                "asset": "Robinhood Markets, Inc.",
                "side": "buy",
                "trade_date": _slash_date(-7),
                "filed_date": _slash_date(-1),
                "amount_range": "$1,001 - $15,000",
                "source_url": "https://example.com/report.pdf",
                "current_price": 18.0,
                "under_price_cap": True,
            }
        ]
    )
    now = datetime.now(timezone.utc).isoformat()
    db.update_signal_status(
        "congress",
        "ok",
        last_attempt_at=now,
        last_success_at=now,
        records_count=1,
    )

    hood = next(candidate for candidate in engine.scan_market() if candidate.symbol == "HOOD")
    health = engine.dashboard_snapshot()["signal_health"]["congress"]

    assert hood.signal_usage["congress"] == "low-confidence"
    assert hood.metrics["congress_buy_count"] == 0.0
    assert health["low_confidence"] is True
    assert engine.degraded_mode() is True


def test_override_disabled_forces_source_off(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.congress_report_urls = ["https://example.com/report.pdf"]
    settings.congress_override_mode = "disabled"
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=db)
    db.replace_congress_trades(
        [
            {
                "member": "Hon. Example Member",
                "chamber": "House",
                "symbol": "HOOD",
                "asset": "Robinhood Markets, Inc.",
                "side": "buy",
                "trade_date": _slash_date(-7),
                "filed_date": _slash_date(-1),
                "amount_range": "$1,001 - $15,000",
                "source_url": "https://example.com/report.pdf",
                "current_price": 18.0,
                "under_price_cap": True,
            }
        ]
    )
    now = datetime.now(timezone.utc).isoformat()
    db.update_signal_status("congress", "ok", last_attempt_at=now, last_success_at=now, records_count=1)

    hood = next(candidate for candidate in engine.scan_market() if candidate.symbol == "HOOD")
    health = engine.dashboard_snapshot()["signal_health"]["congress"]

    assert hood.signal_usage["congress"] == "disabled"
    assert health["enabled"] is False
    assert health["override_mode"] == "disabled"


def test_override_trusted_allows_stale_source_usage(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.congress_report_urls = ["https://example.com/report.pdf"]
    settings.congress_freshness_hours = 1
    settings.congress_override_mode = "trusted"
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=db)
    db.replace_congress_trades(
        [
            {
                "member": "Hon. Example Member",
                "chamber": "House",
                "symbol": "HOOD",
                "asset": "Robinhood Markets, Inc.",
                "side": "buy",
                "trade_date": _slash_date(-7),
                "filed_date": _slash_date(-1),
                "amount_range": "$1,001 - $15,000",
                "source_url": "https://example.com/report.pdf",
                "current_price": 18.0,
                "under_price_cap": True,
            }
        ]
    )
    stale_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    db.update_signal_status("congress", "ok", last_attempt_at=stale_time, last_success_at=stale_time, records_count=1)

    hood = next(candidate for candidate in engine.scan_market() if candidate.symbol == "HOOD")
    health = engine.dashboard_snapshot()["signal_health"]["congress"]

    assert hood.signal_usage["congress"] == "trusted"
    assert hood.metrics["congress_buy_count"] == 1.0
    assert health["stale"] is False
    assert health["override_mode"] == "trusted"


def test_no_data_signal_is_reported_without_degraded_mode(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.congress_report_urls = ["https://example.com/report.pdf"]
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=db)
    now = datetime.now(timezone.utc).isoformat()
    db.update_signal_status(
        "congress",
        "ok",
        last_attempt_at=now,
        last_success_at=now,
        records_count=0,
    )

    hood = next(candidate for candidate in engine.scan_market() if candidate.symbol == "HOOD")
    health = engine.dashboard_snapshot()["signal_health"]["congress"]

    assert hood.signal_usage["congress"] == "no-data"
    assert health["no_data"] is True
    assert engine.degraded_mode() is False


def test_manage_positions_sells_when_stop_hit(tmp_path: Path):
    settings = make_settings(tmp_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=Database(settings.db_path))

    first = engine.trade_once()
    assert first["bought"]

    symbol = first["bought"][0]["symbol"]
    meta = engine.db.get_position_meta(symbol)
    assert meta is not None

    # Raise the stored stop above the market so the next management pass must exit.
    engine.db.open_position_meta(
        symbol,
        float(meta["qty"]),
        float(meta["entry_price"]),
        float(meta["entry_price"]) * 10,
        float(meta["target_price"]),
        meta["analysis"],
    )

    sold = engine.manage_positions()
    assert sold
    assert sold[0]["symbol"] == symbol
    assert sold[0]["note"] == "stop hit"


def test_dashboard_snapshot_includes_position_stop_details(tmp_path: Path):
    settings = make_settings(tmp_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=Database(settings.db_path))

    first = engine.trade_once()
    assert first["bought"]

    snapshot = engine.dashboard_snapshot()
    position = snapshot["positions"][0]

    assert "peak_price" in position
    assert "active_stop_price" in position
    assert "trailing_stop_price" in position
    assert "distance_to_stop_pct" in position
    assert "dynamic_controls" in snapshot
    assert "safety_status" in snapshot
    assert "market_session" in snapshot
    assert "signal_diagnostics" in snapshot
    assert "audit_events" in snapshot
    assert "performance" in snapshot
    assert "unrealized_pnl" in snapshot["performance"]


def test_performance_return_uses_tracked_pnl_not_restart_baseline(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.broker_mode = "live"
    settings.__post_init__()
    db = Database(settings.db_path)

    class RestartedLiveBroker(BaseBroker):
        def account(self) -> AccountSnapshot:
            return AccountSnapshot(cash=900.0, equity=1200.0, buying_power=900.0, mode="live", last_equity=1175.0)

        def positions(self) -> list[PositionSnapshot]:
            return [
                PositionSnapshot(
                    symbol="AAPL",
                    qty=2,
                    avg_entry_price=100.0,
                    current_price=115.0,
                    market_value=230.0,
                    unrealized_pl_pct=15.0,
                )
            ]

        def bars(self, symbols: list[str], days: int) -> dict[str, list[dict]]:
            return {}

        def latest_prices(self, symbols: list[str]) -> dict[str, float]:
            return {"AAPL": 115.0}

        def buy(self, symbol: str, qty: int, stop_price=None, target_price=None) -> dict:
            raise NotImplementedError

        def sell(self, symbol: str, qty=None) -> dict:
            raise NotImplementedError

    db.record_trade("MSFT", "sell", 1, 120.0, "filled", "target hit", pnl_amount=20.0)
    engine = TradingEngine(settings=settings, broker=RestartedLiveBroker(settings), db=db)

    performance = engine.dashboard_snapshot()["performance"]

    assert performance["total_pnl"] == 50.0
    assert performance["tracked_basis"] == 1150.0
    assert performance["total_return_pct"] == 4.35


def test_buy_kill_switch_pauses_new_buys_but_not_scans(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.buy_kill_switch = True
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=Database(settings.db_path))
    candidate = Candidate(
        symbol="AAPL",
        price=10.0,
        final_score=90.0,
        action="buy",
        stop_price=9.5,
        target_price=11.5,
        reward_risk=2.0,
        qty=2,
    )

    bought = engine.buy_candidates([candidate])
    scanned = engine.scan_market()

    assert bought == []
    assert scanned
    snapshot = engine.dashboard_snapshot()
    assert snapshot["safety_status"]["pause_new_buys"] is True
    assert snapshot["audit_events"]
    assert engine.broker.positions() == []


def test_dashboard_snapshot_reconciles_live_positions_for_observer_mode(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.broker_mode = "live"
    settings.__post_init__()
    db = Database(settings.db_path)

    class ObserverBroker(BaseBroker):
        def account(self) -> AccountSnapshot:
            return AccountSnapshot(cash=1250.0, equity=1500.0, buying_power=1250.0, mode="live")

        def positions(self) -> list[PositionSnapshot]:
            return [
                PositionSnapshot(
                    symbol="AAPL",
                    qty=2,
                    avg_entry_price=100.0,
                    current_price=110.0,
                    market_value=220.0,
                    unrealized_pl_pct=10.0,
                )
            ]

        def bars(self, symbols: list[str], days: int) -> dict[str, list[dict]]:
            return {}

        def latest_prices(self, symbols: list[str]) -> dict[str, float]:
            return {"AAPL": 110.0}

        def buy(self, symbol: str, qty: int, stop_price=None, target_price=None) -> dict:
            raise NotImplementedError

        def sell(self, symbol: str, qty=None) -> dict:
            raise NotImplementedError

    engine = TradingEngine(settings=settings, broker=ObserverBroker(settings), db=db)

    snapshot = engine.dashboard_snapshot()

    assert snapshot["mode"] == "live"
    assert db.get_position_meta("AAPL") is not None
    assert any(trade["symbol"] == "AAPL" and trade["status"] == "reconciled" for trade in db.recent_trades(10))


def test_refresh_congress_records_signal_audit_event(tmp_path: Path):
    settings = make_settings(tmp_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=Database(settings.db_path))

    engine.refresh_congress_trades()
    diagnostics = engine._signal_diagnostics()
    audit_events = engine.db.recent_audit_events(10)

    assert any(item["category"] == "signal" for item in audit_events)
    assert "congress" in diagnostics


def test_reconcile_broker_state_records_broker_audit_event(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.alpaca_key_id = "key"
    settings.alpaca_secret_key = "secret"
    settings.__post_init__()
    position = PositionSnapshot(
        symbol="AAPL",
        qty=3,
        avg_entry_price=10.0,
        current_price=10.5,
        market_value=31.5,
        unrealized_pl_pct=5.0,
    )

    class ReconcileBroker(BaseBroker):
        name = "reconcile"

        def __init__(self, settings: Settings) -> None:
            super().__init__(settings)

        def account(self) -> AccountSnapshot:
            return AccountSnapshot(cash=1_000, equity=1_000, buying_power=1_000, mode=self.settings.broker_mode)

        def positions(self):
            return [position]

        def bars(self, symbols, days):
            return {}

        def latest_prices(self, symbols):
            return {"AAPL": 10.5}

        def buy(self, symbol: str, qty: int, stop_price=None, target_price=None) -> dict:
            return {"symbol": symbol, "qty": qty, "filled_avg_price": 10.0, "status": "filled"}

        def sell(self, symbol: str, qty=None) -> dict:
            return {"symbol": symbol, "qty": qty or 0, "filled_avg_price": 10.0, "status": "filled"}

    engine = TradingEngine(settings=settings, broker=ReconcileBroker(settings), db=Database(settings.db_path))

    notes = engine.reconcile_broker_state()
    snapshot = engine.dashboard_snapshot()

    assert notes
    assert any(item["category"] == "broker" for item in snapshot["audit_events"])


def test_sell_records_include_realized_pnl_amount(tmp_path: Path):
    settings = make_settings(tmp_path)
    db = Database(settings.db_path)

    class SellingBroker(BaseBroker):
        def __init__(self, settings: Settings) -> None:
            super().__init__(settings)
            self._positions = [
                PositionSnapshot(
                    symbol="SOFI",
                    qty=10,
                    avg_entry_price=10.0,
                    current_price=8.0,
                    market_value=80.0,
                    unrealized_pl_pct=-20.0,
                )
            ]

        def account(self) -> AccountSnapshot:
            return AccountSnapshot(cash=500.0, equity=620.0, buying_power=500.0, mode="demo")

        def positions(self):
            return list(self._positions)

        def bars(self, symbols, days):
            return {}

        def latest_prices(self, symbols):
            return {"SOFI": 8.0}

        def buy(self, symbol: str, qty: int, stop_price=None, target_price=None) -> dict:
            raise NotImplementedError

        def sell(self, symbol: str, qty=None) -> dict:
            self._positions = []
            return {"symbol": symbol, "qty": qty or 0, "filled_avg_price": 8.0, "status": "filled"}

    engine = TradingEngine(settings=settings, broker=SellingBroker(settings), db=db)
    db.open_position_meta("SOFI", 10, 10.0, 9.0, 12.0, {"risk": 60.0})

    sold = engine.manage_positions()
    trades = db.recent_trades(5)

    assert sold
    assert trades[0]["side"] == "sell"
    assert trades[0]["pnl_amount"] == -20.0


def test_etrade_mirror_previews_new_trade_and_skips_reconciled_entries(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.etrade_mirror_enabled = True
    settings.etrade_account_id_key = "ABC123456"
    settings.etrade_mirror_env = "sandbox"
    settings.etrade_mirror_preview_only = True
    settings.etrade_mirror_max_order_value = 500
    settings.etrade_mirror_max_total_capital = 1_000
    db = Database(settings.db_path)

    class FakeETradeClient:
        def __init__(self) -> None:
            self.preview_calls = []

        def estimated_position_market_value(self, account_id_key: str) -> float:
            return 100.0

        def preview_equity_order(self, account_id_key: str, symbol: str, side: str, qty: int):
            self.preview_calls.append((account_id_key, symbol, side, qty))
            return {"PreviewIds": [{"previewId": "abc"}]}

        def place_equity_order(self, account_id_key: str, symbol: str, side: str, qty: int, preview_payload=None):
            raise AssertionError("place should not be called in preview-only mode")

    client = FakeETradeClient()
    mirror = ETradeMirrorExecutor(settings=settings, db=db, client=client)

    db.record_trade("OLD", "buy", 1, 10.0, "filled", "entry")
    seeded = mirror.sync_new_trades()
    seeded_status = mirror.status()

    assert seeded == []
    assert seeded_status["last_trade_id"] == 1
    assert "seeded cursor" in seeded_status["last_result"]

    db.record_trade("AAPL", "buy", 2, 100.0, "filled", "entry")
    db.record_trade("MSFT", "buy", 1, 50.0, "reconciled", "reconciled external position")

    results = mirror.sync_new_trades()
    status = mirror.status()

    assert len(results) == 1
    assert results[0]["status"] == "preview"
    assert client.preview_calls == [("ABC123456", "AAPL", "BUY", 2)]
    assert status["last_trade_id"] == 3
    assert "skipped" in status["last_result"] or "preview" in status["last_result"]


def test_etrade_mirror_places_order_when_preview_only_disabled(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.etrade_mirror_enabled = True
    settings.etrade_account_id_key = "ABC123456"
    settings.etrade_mirror_env = "live"
    settings.etrade_mirror_preview_only = False
    settings.etrade_mirror_max_order_value = 1_000
    settings.etrade_mirror_max_total_capital = 2_000
    db = Database(settings.db_path)

    class FakeETradeClient:
        def __init__(self) -> None:
            self.preview_calls = []
            self.place_calls = []

        def estimated_position_market_value(self, account_id_key: str) -> float:
            return 0.0

        def symbol_quantity(self, account_id_key: str, symbol: str) -> int:
            return 10

        def preview_equity_order(self, account_id_key: str, symbol: str, side: str, qty: int):
            self.preview_calls.append((account_id_key, symbol, side, qty))
            return {"PreviewIds": [{"previewId": "abc"}]}

        def place_equity_order(self, account_id_key: str, symbol: str, side: str, qty: int, preview_payload=None):
            self.place_calls.append((account_id_key, symbol, side, qty, extract_preview_id(preview_payload or {})))
            return {"PlaceOrderResponse": {"orderId": "123"}}

    client = FakeETradeClient()
    mirror = ETradeMirrorExecutor(settings=settings, db=db, client=client)

    db.record_trade("OLD", "buy", 1, 10.0, "filled", "entry")
    mirror.sync_new_trades()
    db.record_trade("AAPL", "buy", 2, 95.0, "filled", "entry")
    mirror.sync_new_trades()
    db.record_trade("AAPL", "sell", 2, 100.0, "filled", "trailing stop")

    results = mirror.sync_new_trades()

    assert len(results) == 1
    assert results[0]["status"] == "placed"
    assert client.preview_calls[-1] == ("ABC123456", "AAPL", "SELL", 10)
    assert client.place_calls[-1] == ("ABC123456", "AAPL", "SELL", 10, "abc")


def test_etrade_mirror_keeps_partial_sell_quantity_when_tradebot_still_holds_symbol(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.etrade_mirror_enabled = True
    settings.etrade_account_id_key = "ABC123456"
    settings.etrade_mirror_env = "live"
    settings.etrade_mirror_preview_only = False
    settings.etrade_mirror_max_order_value = 1_000
    settings.etrade_mirror_max_total_capital = 2_000
    db = Database(settings.db_path)

    class FakeETradeClient:
        def __init__(self) -> None:
            self.preview_calls = []
            self.place_calls = []

        def estimated_position_market_value(self, account_id_key: str) -> float:
            return 0.0

        def symbol_quantity(self, account_id_key: str, symbol: str) -> int:
            return 10

        def preview_equity_order(self, account_id_key: str, symbol: str, side: str, qty: int):
            self.preview_calls.append((account_id_key, symbol, side, qty))
            return {"PreviewIds": [{"previewId": "abc"}]}

        def place_equity_order(self, account_id_key: str, symbol: str, side: str, qty: int, preview_payload=None):
            self.place_calls.append((account_id_key, symbol, side, qty, extract_preview_id(preview_payload or {})))
            return {"PlaceOrderResponse": {"orderId": "123"}}

    client = FakeETradeClient()
    mirror = ETradeMirrorExecutor(settings=settings, db=db, client=client)

    db.record_trade("OLD", "buy", 1, 10.0, "filled", "entry")
    mirror.sync_new_trades()
    db.record_trade("PLUG", "buy", 5, 4.0, "filled", "entry")
    db.record_trade("PLUG", "sell", 2, 4.5, "filled", "trim")

    results = mirror.sync_new_trades()

    assert len(results) == 2
    assert results[-1]["status"] == "placed"
    assert client.preview_calls[-1] == ("ABC123456", "PLUG", "SELL", 2)
    assert client.place_calls[-1] == ("ABC123456", "PLUG", "SELL", 2, "abc")


def test_etrade_mirror_init_failure_does_not_raise(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.etrade_mirror_enabled = True
    settings.etrade_account_id_key = "ABC123456"
    db = Database(settings.db_path)

    mirror = ETradeMirrorExecutor(settings=settings, db=db)
    mirror._client = lambda: (_ for _ in ()).throw(RuntimeError("missing tokens"))  # type: ignore[attr-defined]

    results = mirror.sync_new_trades()
    status = mirror.status()

    assert results == []
    assert "missing tokens" in status["last_error"]


def test_etrade_mirror_auth_failure_does_not_advance_trade_cursor(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.etrade_mirror_enabled = True
    settings.etrade_account_id_key = "ABC123456"
    settings.etrade_mirror_env = "live"
    settings.etrade_mirror_preview_only = False
    db = Database(settings.db_path)

    class FakeETradeClient:
        def estimated_position_market_value(self, account_id_key: str) -> float:
            return 0.0

        def symbol_quantity(self, account_id_key: str, symbol: str) -> int:
            return 3

        def preview_equity_order(self, account_id_key: str, symbol: str, side: str, qty: int):
            raise ETradeError('401 Unauthorized: {"Error":{"message":"oauth_problem=token_expired"}}')

    client = FakeETradeClient()
    mirror = ETradeMirrorExecutor(settings=settings, db=db, client=client)

    db.record_trade("OLD", "buy", 1, 10.0, "filled", "entry")
    mirror.sync_new_trades()
    db.record_trade("PLUG", "sell", 1, 2.99, "filled", "stop hit")

    results = mirror.sync_new_trades()
    status = mirror.status()

    assert results == [
        {
            "trade_id": 2,
            "symbol": "PLUG",
            "side": "SELL",
            "status": "reauth-required",
            "error": '401 Unauthorized: {"Error":{"message":"oauth_problem=token_expired"}}',
        }
    ]
    assert status["last_trade_id"] == 1
    assert status["auth_expired"] is True
    assert status["last_result"] == "reauth required"
    assert "sync the fresh live token to Railway" in status["recovery_hint"]


def test_etrade_mirror_auth_failure_during_symbol_lookup_keeps_trade_replayable(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.etrade_mirror_enabled = True
    settings.etrade_account_id_key = "ABC123456"
    settings.etrade_mirror_env = "live"
    settings.etrade_mirror_preview_only = False
    db = Database(settings.db_path)

    class FakeETradeClient:
        def estimated_position_market_value(self, account_id_key: str) -> float:
            return 0.0

        def symbol_quantity(self, account_id_key: str, symbol: str) -> int:
            raise ETradeError('401 Unauthorized: {"Error":{"message":"oauth_problem=token_expired"}}')

        def preview_equity_order(self, account_id_key: str, symbol: str, side: str, qty: int):
            raise AssertionError("preview should not run when symbol lookup fails")

    client = FakeETradeClient()
    mirror = ETradeMirrorExecutor(settings=settings, db=db, client=client)

    db.record_trade("OLD", "buy", 1, 10.0, "filled", "entry")
    mirror.sync_new_trades()
    db.record_trade("PLUG", "sell", 1, 2.99, "filled", "stop hit")

    results = mirror.sync_new_trades()
    status = mirror.status()

    assert results == [
        {
            "trade_id": 2,
            "symbol": "PLUG",
            "side": "SELL",
            "status": "reauth-required",
            "error": '401 Unauthorized: {"Error":{"message":"oauth_problem=token_expired"}}',
        }
    ]
    assert status["last_trade_id"] == 1
    assert status["auth_expired"] is True


def test_etrade_mirror_transient_failure_keeps_trade_replayable(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.etrade_mirror_enabled = True
    settings.etrade_account_id_key = "ABC123456"
    settings.etrade_mirror_env = "live"
    settings.etrade_mirror_preview_only = False
    db = Database(settings.db_path)

    class FakeETradeClient:
        def estimated_position_market_value(self, account_id_key: str) -> float:
            return 0.0

        def preview_equity_order(self, account_id_key: str, symbol: str, side: str, qty: int):
            raise ETradeError('500 Internal Server Error: {"Error":{"code":100,"message":"The requested service is not currently available, please try after sometime."}}')

    client = FakeETradeClient()
    mirror = ETradeMirrorExecutor(settings=settings, db=db, client=client)

    db.record_trade("OLD", "buy", 1, 10.0, "filled", "entry")
    mirror.sync_new_trades()
    db.record_trade("UAMY", "buy", 2, 9.74, "pending_new", "entry")

    results = mirror.sync_new_trades()
    status = mirror.status()

    assert results == [
        {
            "trade_id": 2,
            "symbol": "UAMY",
            "side": "BUY",
            "status": "retry-pending",
            "error": '500 Internal Server Error: {"Error":{"code":100,"message":"The requested service is not currently available, please try after sometime."}}',
        }
    ]
    assert status["last_trade_id"] == 1
    assert status["last_result"] == "retry pending UAMY"
    assert "service is not currently available" in status["last_error"]


def test_etrade_mirror_init_auth_failure_marks_reauth_required(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.etrade_mirror_enabled = True
    settings.etrade_account_id_key = "ABC123456"
    db = Database(settings.db_path)

    mirror = ETradeMirrorExecutor(settings=settings, db=db)
    mirror._client = lambda: (_ for _ in ()).throw(RuntimeError('401 Unauthorized: {"Error":{"message":"oauth_problem=token_expired"}}'))  # type: ignore[attr-defined]

    results = mirror.sync_new_trades()
    status = mirror.status()

    assert results == []
    assert status["last_trade_id"] == 0
    assert status["auth_expired"] is True
    assert status["last_result"] == "reauth required"


def test_etrade_client_loads_tokens_from_env_before_file(tmp_path: Path):
    previous_access = os.environ.get("ETRADE_LIVE_ACCESS_TOKEN")
    previous_secret = os.environ.get("ETRADE_LIVE_ACCESS_TOKEN_SECRET")
    os.environ["ETRADE_LIVE_ACCESS_TOKEN"] = "env-access"
    os.environ["ETRADE_LIVE_ACCESS_TOKEN_SECRET"] = "env-secret"
    try:
        from tradebot.etrade import load_etrade_tokens

        tokens = load_etrade_tokens("live")
    finally:
        if previous_access is None:
            os.environ.pop("ETRADE_LIVE_ACCESS_TOKEN", None)
        else:
            os.environ["ETRADE_LIVE_ACCESS_TOKEN"] = previous_access
        if previous_secret is None:
            os.environ.pop("ETRADE_LIVE_ACCESS_TOKEN_SECRET", None)
        else:
            os.environ["ETRADE_LIVE_ACCESS_TOKEN_SECRET"] = previous_secret

    assert tokens["access_token"] == "env-access"
    assert tokens["access_token_secret"] == "env-secret"


def test_etrade_smoke_syncs_saved_tokens_to_railway(tmp_path: Path, monkeypatch):
    token_dir = tmp_path / ".etrade"
    token_dir.mkdir(parents=True, exist_ok=True)
    (token_dir / "live.tokens.json").write_text(
        '{"access_token": "token-123", "access_token_secret": "secret-456"}'
    )
    monkeypatch.setenv("ETRADE_TOKEN_DIR", str(token_dir))
    captured = {}

    def fake_run(command, check, capture_output, text):
        captured["command"] = command
        captured["check"] = check
        captured["capture_output"] = capture_output
        captured["text"] = text
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(etrade_smoke.subprocess, "run", fake_run)
    monkeypatch.setattr(etrade_smoke, "_railway_cli", lambda: "railway")

    etrade_smoke._sync_tokens_to_railway("live", service="tradebot", environment="production")

    assert captured["command"] == [
        "railway",
        "variable",
        "set",
        "ETRADE_LIVE_ACCESS_TOKEN=token-123",
        "ETRADE_LIVE_ACCESS_TOKEN_SECRET=secret-456",
        "--service",
        "tradebot",
        "--environment",
        "production",
    ]
    assert captured["check"] is True
    assert captured["capture_output"] is True
    assert captured["text"] is True


def test_settings_reads_stop_loss_from_env(tmp_path: Path):
    previous_stop_loss = os.environ.get("STOP_LOSS")
    previous_stop_loss_pct = os.environ.get("STOP_LOSS_PCT")
    os.environ["STOP_LOSS"] = "5"
    os.environ.pop("STOP_LOSS_PCT", None)
    try:
        settings = Settings(data_dir=tmp_path)
    finally:
        if previous_stop_loss is None:
            os.environ.pop("STOP_LOSS", None)
        else:
            os.environ["STOP_LOSS"] = previous_stop_loss
        if previous_stop_loss_pct is None:
            os.environ.pop("STOP_LOSS_PCT", None)
        else:
            os.environ["STOP_LOSS_PCT"] = previous_stop_loss_pct
    assert settings.stop_loss_pct == 0.05


class CaptureAlpacaBroker(AlpacaBroker):
    def __init__(self, settings: Settings) -> None:
        self.calls = []
        super().__init__(settings)

    def _request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        return {"status": "accepted"}


def test_alpaca_buy_uses_bracket_order_payload(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.alpaca_key_id = "key"
    settings.alpaca_secret_key = "secret"
    settings.use_broker_protective_orders = True
    settings.__post_init__()

    broker = CaptureAlpacaBroker(settings)
    broker.buy("AAPL", 3, stop_price=9.5, target_price=11.25)

    method, url, kwargs = broker.calls[-1]
    payload = kwargs["json"]
    assert method == "POST"
    assert url.endswith("/v2/orders")
    assert payload["symbol"] == "AAPL"
    assert payload["qty"] == 3
    assert payload["time_in_force"] == "gtc"
    assert payload["order_class"] == "bracket"
    assert payload["stop_loss"] == {"stop_price": 9.5}
    assert payload["take_profit"] == {"limit_price": 11.25}


def test_alpaca_fractional_buy_skips_bracket_order_payload(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.alpaca_key_id = "key"
    settings.alpaca_secret_key = "secret"
    settings.use_broker_protective_orders = True
    settings.__post_init__()

    broker = CaptureAlpacaBroker(settings)
    broker.buy("AAPL", 0.75, stop_price=9.5, target_price=11.25)

    method, url, kwargs = broker.calls[-1]
    payload = kwargs["json"]
    assert method == "POST"
    assert url.endswith("/v2/orders")
    assert payload["symbol"] == "AAPL"
    assert payload["qty"] == 0.75
    assert payload["time_in_force"] == "day"
    assert "order_class" not in payload


def test_alpaca_whole_share_protective_exit_uses_oco_gtc_payload(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.alpaca_key_id = "key"
    settings.alpaca_secret_key = "secret"
    settings.__post_init__()

    broker = CaptureAlpacaBroker(settings)
    broker.submit_protective_exit("AAPL", 3, stop_price=9.5, target_price=11.25)

    method, url, kwargs = broker.calls[-1]
    payload = kwargs["json"]
    assert method == "POST"
    assert url.endswith("/v2/orders")
    assert payload["symbol"] == "AAPL"
    assert payload["qty"] == 3
    assert payload["side"] == "sell"
    assert payload["type"] == "limit"
    assert payload["time_in_force"] == "gtc"
    assert payload["order_class"] == "oco"
    assert payload["stop_loss"] == {"stop_price": 9.5}
    assert payload["take_profit"] == {"limit_price": 11.25}


def test_alpaca_fractional_protective_exit_uses_day_stop_payload(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.alpaca_key_id = "key"
    settings.alpaca_secret_key = "secret"
    settings.__post_init__()

    broker = CaptureAlpacaBroker(settings)
    broker.submit_protective_exit("AAPL", 0.75, stop_price=9.5, target_price=11.25)

    method, url, kwargs = broker.calls[-1]
    payload = kwargs["json"]
    assert method == "POST"
    assert url.endswith("/v2/orders")
    assert payload["symbol"] == "AAPL"
    assert payload["qty"] == 0.75
    assert payload["side"] == "sell"
    assert payload["type"] == "stop"
    assert payload["time_in_force"] == "day"
    assert payload["stop_price"] == 9.5
    assert "order_class" not in payload


class UniverseAlpacaBroker(AlpacaBroker):
    def __init__(self, settings: Settings, assets_payload) -> None:
        self.assets_payload = assets_payload
        super().__init__(settings)

    def _request(self, method: str, url: str, **kwargs):
        if url.endswith("/v2/assets"):
            return self.assets_payload
        return {"status": "accepted"}


def test_alpaca_broker_builds_dynamic_universe_when_scan_universe_is_blank(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.alpaca_key_id = "key"
    settings.alpaca_secret_key = "secret"
    settings.live_universe_mode = "dynamic"
    settings.__post_init__()
    broker = UniverseAlpacaBroker(
        settings,
        [
            {"symbol": "AAPL", "tradable": True},
            {"symbol": "MSFT", "tradable": True},
            {"symbol": "SPY", "tradable": True},
            {"symbol": "$TEST", "tradable": True},
            {"symbol": "NOPE", "tradable": False},
        ],
    )

    universe = broker.universe()

    assert sorted(universe) == ["AAPL", "MSFT", "SPY"]


def test_alpaca_broker_uses_liquid_universe_by_default(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.alpaca_key_id = "key"
    settings.alpaca_secret_key = "secret"
    settings.__post_init__()
    broker = UniverseAlpacaBroker(settings, [{"symbol": "TINY", "tradable": True}])

    universe = broker.universe()

    assert "SPY" in universe
    assert "AAPL" in universe
    assert "TINY" not in universe


class CaptureBroker(BaseBroker):
    name = "capture"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.last_buy = None

    def account(self) -> AccountSnapshot:
        return AccountSnapshot(cash=1_000, equity=1_000, buying_power=1_000, mode=self.settings.broker_mode)

    def positions(self):
        return []

    def bars(self, symbols, days):
        return {}

    def latest_prices(self, symbols):
        return {}

    def buy(self, symbol: str, qty: int, stop_price=None, target_price=None) -> dict:
        self.last_buy = {
            "symbol": symbol,
            "qty": qty,
            "stop_price": stop_price,
            "target_price": target_price,
        }
        return {"symbol": symbol, "qty": qty, "filled_avg_price": 10.0, "status": "filled"}

    def sell(self, symbol: str, qty=None) -> dict:
        return {"symbol": symbol, "qty": qty or 0, "filled_avg_price": 10.0, "status": "filled"}


class PendingBuyBroker(CaptureBroker):
    def buy(self, symbol: str, qty: int, stop_price=None, target_price=None) -> dict:
        self.last_buy = {
            "symbol": symbol,
            "qty": qty,
            "stop_price": stop_price,
            "target_price": target_price,
        }
        return {"symbol": symbol, "qty": qty, "filled_avg_price": None, "status": "pending_new"}


class ScalingBroker(BaseBroker):
    name = "scaling"

    def __init__(self, settings: Settings, *, cash: float, equity: float) -> None:
        super().__init__(settings)
        self._cash = cash
        self._equity = equity

    def account(self) -> AccountSnapshot:
        return AccountSnapshot(cash=self._cash, equity=self._equity, buying_power=self._cash, mode=self.settings.broker_mode)

    def positions(self):
        return []

    def bars(self, symbols, days):
        return {}

    def latest_prices(self, symbols):
        return {}

    def buy(self, symbol: str, qty: int, stop_price=None, target_price=None) -> dict:
        return {"symbol": symbol, "qty": qty, "filled_avg_price": 10.0, "status": "filled"}

    def sell(self, symbol: str, qty=None) -> dict:
        return {"symbol": symbol, "qty": qty or 0, "filled_avg_price": 10.0, "status": "filled"}


def test_buy_candidates_passes_stop_and_target_to_broker(tmp_path: Path):
    settings = make_settings(tmp_path)
    broker = CaptureBroker(settings)
    engine = TradingEngine(settings=settings, broker=broker, db=Database(settings.db_path))
    candidate = Candidate(
        symbol="AAPL",
        price=10.0,
        final_score=90.0,
        action="buy",
        stop_price=9.5,
        target_price=11.5,
        reward_risk=2.0,
        qty=2,
    )

    result = engine.buy_candidates([candidate])

    assert result
    assert broker.last_buy == {
        "symbol": "AAPL",
        "qty": 2,
        "stop_price": 9.5,
        "target_price": 11.5,
    }


def test_buy_candidates_skips_recently_sold_symbol_even_after_profit(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.rebuy_after_sell_cooldown_hours = 4
    broker = CaptureBroker(settings)
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=broker, db=db)
    db.record_trade("AAPL", "sell", 2, 10.5, "filled", "target hit", pnl_pct=5.0)
    candidate = Candidate(
        symbol="AAPL",
        price=10.0,
        final_score=90.0,
        action="buy",
        stop_price=9.5,
        target_price=11.5,
        reward_risk=2.0,
        qty=2,
    )

    result = engine.buy_candidates([candidate])

    assert result == []
    assert broker.last_buy is None


def test_buy_candidates_records_pending_buy_without_opening_position_meta(tmp_path: Path):
    settings = make_settings(tmp_path)
    broker = PendingBuyBroker(settings)
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=broker, db=db)
    candidate = Candidate(
        symbol="AAPL",
        price=10.0,
        final_score=90.0,
        action="buy",
        stop_price=9.5,
        target_price=11.5,
        reward_risk=2.0,
        qty=2,
    )

    result = engine.buy_candidates([candidate])
    trades = db.recent_trades(5)

    assert result == [{"symbol": "AAPL", "qty": 2.0, "price": 10.0, "status": "pending_new"}]
    assert trades[0]["side"] == "buy"
    assert trades[0]["status"] == "pending_new"
    assert trades[0]["note"] == "entry pending"
    assert db.get_position_meta("AAPL") is None


def test_auto_scale_limits_grow_gradually_with_equity(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "demo"
    settings.starting_cash = 500
    settings.max_total_capital = 500
    settings.max_open_positions = 5
    settings.max_new_positions_per_run = 3
    settings.max_stock_price = 10
    settings.congress_max_price = 10
    settings.__post_init__()
    broker = ScalingBroker(settings, cash=1_500, equity=2_000)
    engine = TradingEngine(settings=settings, broker=broker, db=Database(settings.db_path))

    engine._auto_scale_limits()

    assert settings.max_total_capital == 2_000
    assert settings.max_open_positions == 9
    assert settings.max_new_positions_per_run == 5
    assert settings.max_stock_price == 16.5
    assert settings.congress_max_price == 16.5
    assert settings.starting_cash == 500


def test_auto_scale_limits_uses_first_live_equity_as_baseline(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.alpaca_key_id = "key"
    settings.alpaca_secret_key = "secret"
    settings.max_total_capital = 1_000
    settings.max_open_positions = 2
    settings.max_new_positions_per_run = 1
    settings.max_stock_price = 12
    settings.__post_init__()
    broker = ScalingBroker(settings, cash=20_000, equity=20_000)
    engine = TradingEngine(settings=settings, broker=broker, db=Database(settings.db_path))

    engine._auto_scale_limits()
    first_pass = (
        settings.max_total_capital,
        settings.max_open_positions,
        settings.max_new_positions_per_run,
        settings.max_stock_price,
    )

    broker._cash = 30_000
    broker._equity = 30_000
    engine._auto_scale_limits()

    assert first_pass == (1_000, 2, 1, 12)
    assert settings.max_total_capital == 1_500
    assert settings.max_open_positions == 3
    assert settings.max_new_positions_per_run == 2
    assert settings.max_stock_price == 14.0


def test_auto_scale_limits_expands_search_and_tapers_risk(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "demo"
    settings.starting_cash = 500
    settings.max_total_capital = 500
    settings.max_open_positions = 5
    settings.max_new_positions_per_run = 3
    settings.max_stock_price = 10
    settings.scan_limit = 200
    settings.candidate_limit = 30
    settings.min_dollar_volume = 1_000_000
    settings.risk_per_trade_pct = 0.04
    settings.max_position_pct = 0.25
    settings.__post_init__()
    broker = ScalingBroker(settings, cash=1_500, equity=2_000)
    engine = TradingEngine(settings=settings, broker=broker, db=Database(settings.db_path))

    engine._auto_scale_limits()

    assert settings.scan_limit == 283
    assert settings.candidate_limit == 43
    assert settings.min_dollar_volume == 2_300_000
    assert round(settings.risk_per_trade_pct, 4) == 0.0303
    assert round(settings.max_position_pct, 4) == 0.2031


def test_auto_scale_limits_throttle_on_drawdown_from_peak(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.alpaca_key_id = "key"
    settings.alpaca_secret_key = "secret"
    settings.max_total_capital = 1_000
    settings.max_open_positions = 4
    settings.max_new_positions_per_run = 3
    settings.max_stock_price = 12
    settings.scan_limit = 200
    settings.candidate_limit = 30
    settings.min_dollar_volume = 1_000_000
    settings.risk_per_trade_pct = 0.04
    settings.max_position_pct = 0.25
    settings.__post_init__()
    broker = ScalingBroker(settings, cash=30_000, equity=30_000)
    engine = TradingEngine(settings=settings, broker=broker, db=Database(settings.db_path))

    engine._auto_scale_limits()

    broker._cash = 22_500
    broker._equity = 22_500
    engine._auto_scale_limits()

    assert settings.max_total_capital == 600
    assert settings.max_open_positions == 2
    assert settings.max_new_positions_per_run == 1
    assert round(settings.risk_per_trade_pct, 4) == 0.014
    assert round(settings.max_position_pct, 4) == 0.125
    assert engine.dashboard_snapshot()["dynamic_controls"]["drawdown_state"] == "hard"
    assert engine.dashboard_snapshot()["dynamic_controls"]["drawdown_pct"] == 25.0


def test_candidate_from_bars_can_size_fractional_shares_when_needed(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.min_stock_price = 2
    settings.max_stock_price = 0
    settings.risk_per_trade_pct = 0.04
    settings.max_position_pct = 0.25
    settings.min_dollar_volume = 500_000
    settings.__post_init__()
    broker = LiquidityUniverseBroker(
        settings,
        {
            "WINR": _bars(25.0, 200_000),
        },
    )
    engine = TradingEngine(settings=settings, broker=broker, db=Database(settings.db_path))

    candidate = engine._candidate_from_bars("WINR", broker.bars(["WINR"], 40)["WINR"], buying_power=1_000)

    assert candidate is not None
    assert candidate.qty > 0
    assert candidate.qty != int(candidate.qty)


def test_candidate_from_bars_downgrades_buy_when_analyst_consensus_is_hold(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.min_stock_price = 2
    settings.max_stock_price = 0
    settings.analyst_consensus_enabled = True
    settings.analyst_consensus_block_hold = True
    settings.min_dollar_volume = 500_000
    settings.__post_init__()
    broker = LiquidityUniverseBroker(
        settings,
        {
            "WINR": _bars(25.0, 200_000),
        },
    )
    engine = TradingEngine(settings=settings, broker=broker, db=Database(settings.db_path))

    class FakeAnalystTracker:
        def get(self, symbol: str):
            assert symbol == "WINR"
            return {
                "symbol": "WINR",
                "consensus": "Hold",
                "target_upside_pct": 0.53,
                "source": "stockanalysis",
            }

    engine._analyst_tracker = FakeAnalystTracker()

    baseline_engine = TradingEngine(settings=settings, broker=broker, db=Database(tmp_path / "baseline.db"))
    baseline_engine._analyst_tracker = None
    baseline_candidate = baseline_engine._candidate_from_bars("WINR", broker.bars(["WINR"], 40)["WINR"], buying_power=1_000)
    candidate = engine._candidate_from_bars("WINR", broker.bars(["WINR"], 40)["WINR"], buying_power=1_000)

    assert baseline_candidate is not None
    assert candidate is not None
    assert candidate.action == "watch"
    assert candidate.signal_usage["analyst_consensus"] == "Hold"
    assert candidate.metrics["analyst_consensus_hold_signal"] == 1.0
    assert candidate.metrics["analyst_target_upside_pct"] == 0.53
    assert candidate.metrics["analyst_consensus_blocked"] == 1.0
    assert candidate.analyst_scores["decision_support"] < baseline_candidate.analyst_scores["decision_support"]
    assert "analyst consensus is Hold" in candidate.reasons[0]


def test_candidate_from_bars_rewards_buy_consensus_in_decision_support(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.min_stock_price = 2
    settings.max_stock_price = 0
    settings.analyst_consensus_enabled = True
    settings.analyst_consensus_block_hold = True
    settings.min_dollar_volume = 500_000
    settings.__post_init__()
    broker = LiquidityUniverseBroker(
        settings,
        {
            "WINR": _bars(25.0, 200_000),
        },
    )
    engine = TradingEngine(settings=settings, broker=broker, db=Database(settings.db_path))

    class FakeAnalystTracker:
        def get(self, symbol: str):
            assert symbol == "WINR"
            return {
                "symbol": "WINR",
                "consensus": "Buy",
                "target_upside_pct": 18.5,
                "source": "stockanalysis",
            }

    engine._analyst_tracker = FakeAnalystTracker()

    candidate = engine._candidate_from_bars("WINR", broker.bars(["WINR"], 40)["WINR"], buying_power=1_000)

    assert candidate is not None
    assert candidate.signal_usage["analyst_consensus"] == "Buy"
    assert candidate.metrics["analyst_consensus_buy_signal"] == 1.0
    assert candidate.metrics["analyst_target_upside_pct"] == 18.5
    assert candidate.analyst_scores["decision_support"] >= 50


def test_candidate_from_bars_skips_analyst_consensus_for_configured_etfs(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.min_stock_price = 2
    settings.max_stock_price = 0
    settings.analyst_consensus_enabled = True
    settings.exclude_broad_market_etfs = False  # this test is about consensus skip, not exclusion
    settings.min_dollar_volume = 500_000
    settings.__post_init__()
    broker = LiquidityUniverseBroker(
        settings,
        {
            "SPY": _bars(25.0, 200_000),
        },
    )
    engine = TradingEngine(settings=settings, broker=broker, db=Database(settings.db_path))

    class FailingAnalystTracker:
        def get(self, symbol: str):
            raise AssertionError(f"analyst lookup should be skipped for {symbol}")

    engine._analyst_tracker = FailingAnalystTracker()

    candidate = engine._candidate_from_bars("SPY", broker.bars(["SPY"], 40)["SPY"], buying_power=1_000)

    assert candidate is not None
    assert candidate.signal_usage["analyst_consensus"] == "skipped"
    assert candidate.metrics["analyst_target_upside_pct"] == 0.0
    assert candidate.metrics["analyst_consensus_buy_signal"] == 0.0


def test_candidate_from_bars_excludes_broad_market_etfs(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.min_stock_price = 2
    settings.max_stock_price = 0
    settings.min_dollar_volume = 500_000
    settings.__post_init__()
    broker = LiquidityUniverseBroker(
        settings,
        {"SPY": _bars(25.0, 200_000), "WINR": _bars(8.0, 200_000)},
    )
    engine = TradingEngine(settings=settings, broker=broker, db=Database(settings.db_path))

    # A broad-market ETF is filtered out entirely (not a stock pick)...
    assert engine._candidate_from_bars("SPY", broker.bars(["SPY"], 40)["SPY"], buying_power=1_000) is None
    # ...while a real single name still produces a candidate.
    assert engine._candidate_from_bars("WINR", broker.bars(["WINR"], 40)["WINR"], buying_power=1_000) is not None

    # The exclusion is toggleable.
    settings.exclude_broad_market_etfs = False
    assert engine._candidate_from_bars("SPY", broker.bars(["SPY"], 40)["SPY"], buying_power=1_000) is not None


class LiquidityUniverseBroker(BaseBroker):
    name = "liquidity-universe"

    def __init__(self, settings: Settings, bars_by_symbol: dict[str, list[dict]]) -> None:
        super().__init__(settings)
        self.bars_by_symbol = bars_by_symbol

    def universe(self):
        return list(self.bars_by_symbol.keys())

    def account(self) -> AccountSnapshot:
        return AccountSnapshot(cash=1_000, equity=1_000, buying_power=1_000, mode=self.settings.broker_mode)

    def positions(self):
        return []

    def bars(self, symbols, days):
        return {symbol: self.bars_by_symbol[symbol][-days:] for symbol in symbols if symbol in self.bars_by_symbol}

    def latest_prices(self, symbols):
        return {symbol: float(self.bars_by_symbol[symbol][-1]["c"]) for symbol in symbols if symbol in self.bars_by_symbol}

    def buy(self, symbol: str, qty: int, stop_price=None, target_price=None) -> dict:
        return {"symbol": symbol, "qty": qty, "filled_avg_price": 10.0, "status": "filled"}

    def sell(self, symbol: str, qty=None) -> dict:
        return {"symbol": symbol, "qty": qty or 0, "filled_avg_price": 10.0, "status": "filled"}


def _bars(price: float, volume: int, days: int = 40) -> list[dict]:
    bars = []
    for idx in range(days):
        close = round(price * (1 + (idx / (days * 200))), 4)
        bars.append({"t": f"2026-03-{(idx % 28) + 1:02d}", "o": close, "h": close * 1.01, "l": close * 0.99, "c": close, "v": volume})
    return bars


def _down_bars(price: float, volume: int, days: int = 60) -> list[dict]:
    bars = []
    for idx in range(days):
        close = round(price * (1 - (idx / (days * 20))), 4)
        bars.append({"t": f"2026-04-{(idx % 28) + 1:02d}", "o": close, "h": close * 1.01, "l": close * 0.99, "c": close, "v": volume})
    return bars


def test_profit_lock_pauses_new_buys(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.profit_lock_dollars = 10
    broker = CaptureBroker(settings)
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=broker, db=db)
    db.set_bot_state(f"daily_equity_anchor:{engine._today_et()}", "980")
    candidate = Candidate(
        symbol="AAPL",
        price=10.0,
        final_score=90.0,
        action="buy",
        stop_price=9.0,
        target_price=12.0,
        reward_risk=2.0,
        qty=2,
    )

    result = engine.buy_candidates([candidate])

    assert result == []
    assert "daily profit lock" in engine._buying_pause_reason()
    assert broker.last_buy is None


def test_market_regime_filter_blocks_long_candidate_when_indexes_are_weak(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.market_regime_filter = True
    settings.market_regime_short_window = 20
    settings.market_regime_long_window = 50
    settings.market_regime_limited_long_min_score = 101
    settings.min_reward_risk = 1.2
    broker = LiquidityUniverseBroker(
        settings,
        {
            "SPY": _down_bars(500.0, 5_000_000),
            "QQQ": _down_bars(450.0, 5_000_000),
            "WINR": _bars(10.0, 2_000_000, days=60),
        },
    )
    engine = TradingEngine(settings=settings, broker=broker, db=Database(settings.db_path))

    candidate = engine._candidate_from_bars("WINR", broker.bars(["WINR"], 60)["WINR"], buying_power=1_000)

    assert candidate is not None
    assert candidate.action == "watch"
    assert candidate.metrics["market_regime_blocked"] == 1.0
    assert "market regime filter" in candidate.reasons[0]


def test_market_regime_filter_allows_reduced_size_high_score_long_when_weak(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.market_regime_filter = True
    settings.market_regime_short_window = 20
    settings.market_regime_long_window = 50
    settings.market_regime_limited_long_min_score = 55
    settings.market_regime_limited_long_max_position_pct = 0.18
    settings.max_position_pct = 0.40
    settings.min_reward_risk = 1.2
    broker = LiquidityUniverseBroker(
        settings,
        {
            "SPY": _down_bars(500.0, 5_000_000),
            "QQQ": _down_bars(450.0, 5_000_000),
            "WINR": _bars(10.0, 2_000_000, days=60),
        },
    )
    engine = TradingEngine(settings=settings, broker=broker, db=Database(settings.db_path))

    candidate = engine._candidate_from_bars("WINR", broker.bars(["WINR"], 60)["WINR"], buying_power=1_000)

    assert candidate is not None
    assert candidate.action == "buy"
    assert candidate.metrics["market_regime_blocked"] == 0.5
    assert candidate.signal_usage["market_regime"].endswith("-limited")
    assert "reduced-size starter" in candidate.reasons[0]
    assert candidate.qty <= 18.0


def test_shadow_mode_records_high_score_candidates_without_buying(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.shadow_mode_strategies = True
    broker = CaptureBroker(settings)
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=broker, db=db)
    candidate = Candidate(
        symbol="AAPL",
        price=10.0,
        final_score=90.0,
        action="buy",
        stop_price=9.0,
        target_price=12.0,
        reward_risk=2.0,
        qty=2,
        analyst_scores={"momentum": 80.0},
        signal_usage={"market_regime": "uptrend"},
    )

    shadow = engine._record_shadow_candidates([candidate], bought=[])
    rows = db.recent_shadow_picks(days=1)

    assert len(shadow) == 1
    assert len(rows) == 1
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["analysis"] == {"momentum": 80.0}


def test_candidate_symbol_pool_prefilters_for_price_and_liquidity(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.live_universe_mode = "dynamic"
    settings.scan_limit = 3
    settings.candidate_limit = 3
    settings.min_stock_price = 2
    settings.max_stock_price = 10
    settings.min_dollar_volume = 1_000_000
    settings.__post_init__()
    broker = LiquidityUniverseBroker(
        settings,
        {
            "JUNK1": _bars(5.0, 1_000),
            "JUNK2": _bars(6.0, 2_000),
            "HIGH1": _bars(12.0, 500_000),
            "GOOD1": _bars(4.0, 600_000),
            "GOOD2": _bars(8.0, 300_000),
            "GOOD3": _bars(9.0, 250_000),
        },
    )
    engine = TradingEngine(settings=settings, broker=broker, db=Database(settings.db_path))
    engine.polygon = None

    pool = engine._candidate_symbol_pool()

    assert "GOOD1" in pool
    assert "GOOD2" in pool
    assert "JUNK1" not in pool
    assert "JUNK2" not in pool
    assert "HIGH1" not in pool


def test_candidate_symbol_pool_allows_higher_priced_names_when_max_stock_price_disabled(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.live_universe_mode = "dynamic"
    settings.scan_limit = 3
    settings.candidate_limit = 3
    settings.min_stock_price = 2
    settings.max_stock_price = 0
    settings.min_dollar_volume = 1_000_000
    settings.__post_init__()
    broker = LiquidityUniverseBroker(
        settings,
        {
            "LOW1": _bars(4.0, 600_000),
            "HIGH1": _bars(24.0, 200_000),
            "HIGH2": _bars(40.0, 90_000),
        },
    )
    engine = TradingEngine(settings=settings, broker=broker, db=Database(settings.db_path))
    engine.polygon = None

    pool = engine._candidate_symbol_pool()

    assert "LOW1" in pool
    assert "HIGH1" in pool
    assert "HIGH2" in pool


class FailingScanBroker(BaseBroker):
    name = "failing-scan"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)

    def universe(self):
        return ["AAPL", "MSFT", "NVDA"]

    def account(self) -> AccountSnapshot:
        return AccountSnapshot(cash=1_000, equity=1_000, buying_power=1_000, mode=self.settings.broker_mode)

    def positions(self):
        return []

    def bars(self, symbols, days):
        raise ProviderError("synthetic market data failure")

    def latest_prices(self, symbols):
        return {}

    def buy(self, symbol: str, qty: int, stop_price=None, target_price=None) -> dict:
        return {"symbol": symbol, "qty": qty, "filled_avg_price": 10.0, "status": "filled"}

    def sell(self, symbol: str, qty=None) -> dict:
        return {"symbol": symbol, "qty": qty or 0, "filled_avg_price": 10.0, "status": "filled"}


def test_scan_market_returns_empty_list_when_provider_data_fetch_fails(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.scan_limit = 5
    settings.__post_init__()
    settings.polygon_api_key = ""
    settings.analyst_consensus_enabled = False
    engine = TradingEngine(settings=settings, broker=FailingScanBroker(settings), db=Database(settings.db_path))

    result = engine.scan_market()

    assert result == []
    assert engine.db.latest_candidates() == []


def test_candidate_from_bars_requires_strong_buy_consensus(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.min_stock_price = 2
    settings.max_stock_price = 0
    settings.analyst_consensus_enabled = True
    settings.analyst_consensus_require_strong_buy = True
    settings.min_dollar_volume = 500_000
    settings.__post_init__()
    broker = LiquidityUniverseBroker(
        settings,
        {
            "WINR": _bars(25.0, 200_000),
        },
    )
    engine = TradingEngine(settings=settings, broker=broker, db=Database(settings.db_path))

    class FakeAnalystTracker:
        def __init__(self, consensus):
            self.consensus = consensus

        def get(self, symbol: str):
            if self.consensus is None:
                return None
            return {
                "symbol": symbol,
                "consensus": self.consensus,
                "target_upside_pct": 18.5,
                "source": "stockanalysis",
            }

    bars = broker.bars(["WINR"], 40)["WINR"]

    engine._analyst_tracker = FakeAnalystTracker("Buy")
    plain_buy = engine._candidate_from_bars("WINR", bars, buying_power=1_000)
    assert plain_buy is not None
    assert plain_buy.action == "watch"
    assert plain_buy.metrics["analyst_consensus_blocked"] == 1.0
    assert "only Strong Buy" in plain_buy.reasons[0]

    engine._analyst_tracker = FakeAnalystTracker("Strong Buy")
    strong_buy = engine._candidate_from_bars("WINR", bars, buying_power=1_000)
    assert strong_buy is not None
    assert strong_buy.metrics["analyst_consensus_blocked"] == 0.0

    engine._analyst_tracker = FakeAnalystTracker(None)
    no_consensus = engine._candidate_from_bars("WINR", bars, buying_power=1_000)
    assert no_consensus is not None
    assert no_consensus.action == "watch"
    assert no_consensus.metrics["analyst_consensus_blocked"] == 1.0

    settings.analyst_consensus_require_strong_buy = False
    engine._analyst_tracker = FakeAnalystTracker("Buy")
    gate_off = engine._candidate_from_bars("WINR", bars, buying_power=1_000)
    assert gate_off is not None
    assert gate_off.metrics["analyst_consensus_blocked"] == 0.0


def test_manage_positions_rotates_out_excluded_broad_market_etfs(tmp_path: Path):
    settings = make_settings(tmp_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=Database(settings.db_path))

    first = engine.trade_once()
    assert first["bought"]
    symbol = first["bought"][0]["symbol"]

    # Treat the held symbol as a broad-market ETF bought before the exclusion
    # deployed, held long enough that the rotation sell is never a day trade.
    settings.exclude_broad_market_etfs = True
    settings.broad_market_etfs = [symbol]
    with engine.db.connect() as con:
        con.execute(
            "UPDATE position_meta SET opened_at=? WHERE symbol=?",
            ((datetime.now(timezone.utc) - timedelta(days=3)).isoformat(), symbol),
        )

    sold = engine.manage_positions()

    assert any(
        item["symbol"] == symbol and "rotating out of excluded broad-market ETF" in str(item.get("note", ""))
        for item in sold
    )


def test_manage_positions_does_not_rotate_same_day_positions(tmp_path: Path):
    settings = make_settings(tmp_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=Database(settings.db_path))

    first = engine.trade_once()
    assert first["bought"]
    symbol = first["bought"][0]["symbol"]

    settings.exclude_broad_market_etfs = True
    settings.broad_market_etfs = [symbol]

    sold = engine.manage_positions()

    assert not any("rotating out" in str(item.get("note", "")) for item in sold)


def test_buy_candidates_respects_cash_buffer(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.cash_buffer_pct = 1.0  # reserve all equity: nothing is affordable
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=Database(settings.db_path))

    candidates = engine.scan_market()
    assert any(c.action == "buy" for c in candidates)

    bought = engine.buy_candidates(candidates)

    assert bought == []


def test_scan_market_checks_regime_before_candidate_bars(tmp_path: Path):
    settings = make_settings(tmp_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=Database(settings.db_path))
    calls: list[str] = []

    original_regime = engine._market_regime_status
    original_fetch = engine._fetch_bars
    engine._market_regime_status = lambda: calls.append("regime") or original_regime()  # type: ignore[method-assign]
    engine._fetch_bars = lambda symbols, days: calls.append("bars") or original_fetch(symbols, days)  # type: ignore[method-assign]

    engine.scan_market()

    # The 2-symbol regime read must happen before the big candidate bar burst,
    # otherwise rate limiting reads as regime "missing" and blocks all buys.
    assert "regime" in calls and "bars" in calls
    assert calls.index("regime") < calls.index("bars")


class BrokerManagedExitBroker(BaseBroker):
    name = "broker-managed"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._positions = []
        self._recent = {
            "AAPL": {
                "symbol": "AAPL",
                "side": "sell",
                "status": "filled",
                "filled_avg_price": 11.0,
                "filled_qty": 2,
                "order_class": "bracket",
            }
        }

    def account(self) -> AccountSnapshot:
        return AccountSnapshot(cash=1_000, equity=1_000, buying_power=1_000, mode=self.settings.broker_mode)

    def positions(self):
        return self._positions

    def bars(self, symbols, days):
        return {}

    def latest_prices(self, symbols):
        return {}

    def buy(self, symbol: str, qty: int, stop_price=None, target_price=None) -> dict:
        return {"symbol": symbol, "qty": qty, "filled_avg_price": 10.0, "status": "filled"}

    def sell(self, symbol: str, qty=None) -> dict:
        return {"symbol": symbol, "qty": qty or 0, "filled_avg_price": 10.0, "status": "filled"}

    def recent_filled_sell_orders(self, symbols):
        return {symbol: self._recent[symbol] for symbol in symbols if symbol in self._recent}


def test_reconcile_broker_managed_exits_updates_learning(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.use_broker_protective_orders = True
    settings.alpaca_key_id = "key"
    settings.alpaca_secret_key = "secret"
    settings.__post_init__()

    broker = BrokerManagedExitBroker(settings)
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=broker, db=db)
    db.open_position_meta(
        "AAPL",
        2,
        10.0,
        9.5,
        11.0,
        {"decision_support": 80.0, "momentum": 100.0, "reversion": 50.0, "risk": 75.0},
    )

    before = engine.learning_weights()
    sold = engine.manage_positions()
    after = engine.learning_weights()

    assert sold == [{"symbol": "AAPL", "pnl_pct": 10.0, "note": "bracket"}]
    assert after["decision_support"] > before["decision_support"]
    assert after["momentum"] > before["momentum"]
    assert db.get_position_meta("AAPL") is None


def test_demo_latest_price_matches_bar_close(tmp_path: Path):
    settings = make_settings(tmp_path)
    broker = build_broker(settings)
    symbol = broker.universe()[0]

    latest = broker.latest_prices([symbol])[symbol]
    bars = broker.bars([symbol], settings.lookback_days)[symbol]

    assert latest == bars[-1]["c"]


def test_demo_broker_uses_default_universe_when_scan_universe_is_blank(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.scan_universe = []
    broker = build_broker(settings)

    assert broker.universe()[:5] == ["SOFI", "HOOD", "OPEN", "UPST", "AFRM"]


def test_learning_update_caps_single_outlier_loss(tmp_path: Path):
    db = Database(tmp_path / "tradebot.db")

    db.update_learning({"momentum": 100.0}, -50.0)
    weights = db.learning_weights()

    assert weights["momentum"]["weight"] > 0.2


def test_learning_differentiates_strategies_by_conviction(tmp_path: Path):
    db = Database(tmp_path / "tradebot.db")

    # A winning trade that momentum strongly endorsed (95) but risk doubted (20).
    db.update_learning({"momentum": 95.0, "risk": 20.0}, 12.0)
    weights = db.learning_weights()

    # The strategy that endorsed the winner is trusted more; the doubter is not.
    assert weights["momentum"]["weight"] > 1.0
    assert weights["risk"]["weight"] < weights["momentum"]["weight"]
    # The whole point: weights are no longer identical across strategies.
    assert weights["momentum"]["weight"] != weights["risk"]["weight"]


def test_learning_reset_v2_wipes_corrupted_counts(tmp_path: Path):
    path = tmp_path / "tradebot.db"
    db = Database(path)

    # Simulate the corrupted/saturated table the old retro loop produced.
    with db.connect() as con:
        con.execute("UPDATE learning SET wins = 99999, losses = 99999, weight = 0.25")
        con.execute("DELETE FROM bot_state WHERE key = 'learning_reset_v2'")

    # Re-opening runs the one-time migration, which wipes the bad counts.
    healed = Database(path).learning_weights()
    assert all(row["wins"] == 0 and row["losses"] == 0 and row["weight"] == 1.0 for row in healed.values())

    # It must not fire a second time and clobber real learning.
    db_again = Database(path)
    db_again.update_learning({"momentum": 95.0}, 10.0)
    reopened = Database(path).learning_weights()
    assert reopened["momentum"]["wins"] == 1


class PositionBroker(BaseBroker):
    name = "positions"

    def __init__(self, settings: Settings, positions: list[PositionSnapshot] | None = None) -> None:
        super().__init__(settings)
        self._positions = positions or []
        self.last_buy = None
        self.sell_calls = []
        self.cancel_calls = []

    def account(self) -> AccountSnapshot:
        market_value = sum(position.market_value for position in self._positions)
        return AccountSnapshot(cash=1_000, equity=1_000 + market_value, buying_power=1_000, mode=self.settings.broker_mode)

    def positions(self):
        return list(self._positions)

    def bars(self, symbols, days):
        return {}

    def latest_prices(self, symbols):
        return {position.symbol: position.current_price for position in self._positions if position.symbol in symbols}

    def buy(self, symbol: str, qty: int, stop_price=None, target_price=None) -> dict:
        self.last_buy = {"symbol": symbol, "qty": qty, "stop_price": stop_price, "target_price": target_price}
        return {"symbol": symbol, "qty": qty, "filled_avg_price": 10.0, "status": "filled"}

    def sell(self, symbol: str, qty=None) -> dict:
        self.sell_calls.append((symbol, qty))
        self._positions = [position for position in self._positions if position.symbol != symbol]
        return {"symbol": symbol, "qty": qty or 0, "filled_avg_price": 10.0, "status": "filled"}

    def cancel_open_orders_for_symbol(self, symbol: str) -> int:
        self.cancel_calls.append(symbol)
        return 1


class ProtectivePositionBroker(PositionBroker):
    def __init__(
        self,
        settings: Settings,
        positions: list[PositionSnapshot] | None = None,
        exit_orders: list[dict] | None = None,
    ) -> None:
        super().__init__(settings, positions)
        self.exit_orders = exit_orders or []
        self.protective_calls = []

    def open_exit_orders_for_symbol(self, symbol: str):
        return list(self.exit_orders)

    def submit_protective_exit(self, symbol: str, qty: float, stop_price: float, target_price: float | None = None):
        self.protective_calls.append((symbol, qty, stop_price, target_price))
        return {"symbol": symbol, "status": "accepted", "order_class": "oco"}


class FreshPriceProtectiveBroker(ProtectivePositionBroker):
    def __init__(
        self,
        settings: Settings,
        positions: list[PositionSnapshot] | None = None,
        prices: dict[str, list[float]] | None = None,
    ) -> None:
        super().__init__(settings, positions)
        self.prices = {symbol: list(values) for symbol, values in (prices or {}).items()}

    def latest_prices(self, symbols):
        result = {}
        for symbol in symbols:
            values = self.prices.get(symbol)
            if values:
                result[symbol] = values.pop(0) if len(values) > 1 else values[0]
            else:
                result.update(super().latest_prices([symbol]))
        return result


class RejectingProtectiveBroker(FreshPriceProtectiveBroker):
    def submit_protective_exit(self, symbol: str, qty: float, stop_price: float, target_price: float | None = None):
        self.protective_calls.append((symbol, qty, stop_price, target_price))
        raise ProviderError(
            'Alpaca request failed: Alpaca error 422: {"message":"stop price must be less than current price"}'
        )


def test_manage_positions_liquidates_when_daily_dollar_loss_limit_hit(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.daily_loss_limit_dollars = 20
    settings.liquidate_on_daily_loss = True
    position = PositionSnapshot(
        symbol="AAPL",
        qty=2,
        avg_entry_price=10.0,
        current_price=11.0,
        market_value=22.0,
        unrealized_pl_pct=10.0,
    )
    broker = PositionBroker(settings, [position])
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=broker, db=db)
    db.set_bot_state(f"daily_equity_anchor:{engine._today_et()}", "1100")
    db.open_position_meta("AAPL", 2, 10.0, 9.0, 12.0, {"momentum": 100.0})

    sold = engine.manage_positions()

    assert sold
    assert sold[0]["symbol"] == "AAPL"
    assert sold[0]["note"].startswith("daily loss limit")
    assert broker.sell_calls == [("AAPL", 2.0)]
    assert db.get_position_meta("AAPL") is None


def test_manage_positions_waits_for_market_open_before_daily_loss_liquidation(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.alpaca_key_id = "key"
    settings.alpaca_secret_key = "secret"
    settings.daily_loss_limit_dollars = 20
    settings.liquidate_on_daily_loss = True
    settings.__post_init__()
    position = PositionSnapshot(
        symbol="AAPL",
        qty=2,
        avg_entry_price=10.0,
        current_price=11.0,
        market_value=22.0,
        unrealized_pl_pct=10.0,
    )
    broker = PositionBroker(settings, [position])
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=broker, db=db)
    engine._market_is_closed = lambda: True  # type: ignore[method-assign]
    db.set_bot_state(f"daily_equity_anchor:{engine._today_et()}", "1100")
    db.open_position_meta("AAPL", 2, 10.0, 9.0, 12.0, {"momentum": 100.0})

    sold = engine.manage_positions()

    assert sold == []
    assert broker.sell_calls == []
    assert db.get_position_meta("AAPL") is not None


def test_manage_positions_recreates_missing_protective_exit(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.alpaca_key_id = "key"
    settings.alpaca_secret_key = "secret"
    settings.use_broker_protective_orders = True
    settings.stop_loss_pct = 0.05
    settings.__post_init__()
    position = PositionSnapshot(
        symbol="AAPL",
        qty=2,
        avg_entry_price=10.0,
        current_price=10.5,
        market_value=21.0,
        unrealized_pl_pct=5.0,
    )
    broker = ProtectivePositionBroker(settings, [position])
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=broker, db=db)
    db.open_position_meta("AAPL", 2, 10.0, 9.0, 12.0, {"momentum": 100.0})

    sold = engine.manage_positions()

    assert sold == []
    assert broker.protective_calls == [("AAPL", 2.0, 9.5, 12.0)]
    assert broker.cancel_calls == []


def test_manage_positions_defers_tiny_protective_stop_ratchet(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.alpaca_key_id = "key"
    settings.alpaca_secret_key = "secret"
    settings.use_broker_protective_orders = True
    settings.stop_loss_pct = 0.05
    settings.trailing_stop_pct = 0.10
    settings.protective_stop_replace_min_step_pct = 0.01
    settings.__post_init__()
    position = PositionSnapshot(
        symbol="AAPL",
        qty=2,
        avg_entry_price=10.0,
        current_price=10.8,
        market_value=21.6,
        unrealized_pl_pct=8.0,
    )
    broker = ProtectivePositionBroker(settings, [position], exit_orders=[{"symbol": "AAPL", "side": "sell", "stop_price": 9.65}])
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=broker, db=db)
    db.open_position_meta("AAPL", 2, 10.0, 9.5, 12.0, {"momentum": 100.0})

    sold = engine.manage_positions()

    assert sold == []
    assert broker.cancel_calls == []
    assert broker.protective_calls == []


def test_manage_positions_replaces_material_protective_stop_ratchet(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.alpaca_key_id = "key"
    settings.alpaca_secret_key = "secret"
    settings.use_broker_protective_orders = True
    settings.stop_loss_pct = 0.05
    settings.trailing_stop_pct = 0.10
    settings.protective_stop_replace_min_step_pct = 0.01
    settings.__post_init__()
    position = PositionSnapshot(
        symbol="AAPL",
        qty=2,
        avg_entry_price=10.0,
        current_price=10.8,
        market_value=21.6,
        unrealized_pl_pct=8.0,
    )
    broker = ProtectivePositionBroker(settings, [position], exit_orders=[{"symbol": "AAPL", "side": "sell", "stop_price": 9.5}])
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=broker, db=db)
    db.open_position_meta("AAPL", 2, 10.0, 9.5, 12.0, {"momentum": 100.0})

    sold = engine.manage_positions()

    assert sold == []
    assert broker.cancel_calls == ["AAPL"]
    assert broker.protective_calls == [("AAPL", 2.0, 9.72, 12.0)]


def test_manage_positions_sells_when_fresh_price_breaches_protective_stop(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.alpaca_key_id = "key"
    settings.alpaca_secret_key = "secret"
    settings.use_broker_protective_orders = True
    settings.stop_loss_pct = 0.05
    settings.__post_init__()
    position = PositionSnapshot(
        symbol="AAPL",
        qty=2,
        avg_entry_price=10.0,
        current_price=10.5,
        market_value=21.0,
        unrealized_pl_pct=5.0,
    )
    broker = FreshPriceProtectiveBroker(settings, [position], prices={"AAPL": [10.5, 9.4]})
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=broker, db=db)
    db.open_position_meta("AAPL", 2, 10.0, 9.5, 12.0, {"momentum": 100.0})

    sold = engine.manage_positions()

    assert sold
    assert sold[0]["symbol"] == "AAPL"
    assert sold[0]["note"] == "protective stop already breached"
    assert broker.protective_calls == []
    assert broker.sell_calls == [("AAPL", 2.0)]
    assert db.get_position_meta("AAPL") is None


def test_manage_positions_sells_when_protective_stop_rejected_as_above_market(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.alpaca_key_id = "key"
    settings.alpaca_secret_key = "secret"
    settings.use_broker_protective_orders = True
    settings.stop_loss_pct = 0.05
    settings.__post_init__()
    position = PositionSnapshot(
        symbol="AAPL",
        qty=2,
        avg_entry_price=10.0,
        current_price=10.5,
        market_value=21.0,
        unrealized_pl_pct=5.0,
    )
    broker = RejectingProtectiveBroker(settings, [position], prices={"AAPL": [10.5, 10.5, 9.4]})
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=broker, db=db)
    db.open_position_meta("AAPL", 2, 10.0, 9.5, 12.0, {"momentum": 100.0})

    sold = engine.manage_positions()

    assert sold
    assert sold[0]["symbol"] == "AAPL"
    assert sold[0]["note"] == "protective stop already breached"
    assert broker.protective_calls == [("AAPL", 2.0, 9.5, 12.0)]
    assert broker.sell_calls == [("AAPL", 2.0)]
    assert db.get_position_meta("AAPL") is None


def test_manage_positions_respects_min_hold_days_for_target_exit(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.min_hold_days = 3
    position = PositionSnapshot(
        symbol="AAPL",
        qty=2,
        avg_entry_price=10.0,
        current_price=12.0,
        market_value=24.0,
        unrealized_pl_pct=20.0,
    )
    broker = PositionBroker(settings, [position])
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=broker, db=db)
    db.open_position_meta("AAPL", 2, 10.0, 9.0, 11.0, {"momentum": 100.0})

    sold = engine.manage_positions()

    assert sold == []
    assert broker.sell_calls == []


def test_manage_positions_sells_target_hit_after_min_hold(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.min_hold_days = 0
    position = PositionSnapshot(
        symbol="AAPL",
        qty=2,
        avg_entry_price=10.0,
        current_price=12.0,
        market_value=24.0,
        unrealized_pl_pct=20.0,
    )
    broker = PositionBroker(settings, [position])
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=broker, db=db)
    db.open_position_meta("AAPL", 2, 10.0, 9.0, 11.0, {"momentum": 100.0})

    sold = engine.manage_positions()

    assert sold
    assert sold[0]["symbol"] == "AAPL"
    assert sold[0]["note"] == "target hit"
    assert broker.sell_calls == [("AAPL", 2.0)]


def test_buy_candidates_respects_capital_and_position_limits(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.max_total_capital = 150
    settings.max_open_positions = 1
    existing = PositionSnapshot(
        symbol="MSFT",
        qty=5,
        avg_entry_price=20.0,
        current_price=20.0,
        market_value=100.0,
        unrealized_pl_pct=0.0,
    )
    broker = PositionBroker(settings, [existing])
    engine = TradingEngine(settings=settings, broker=broker, db=Database(settings.db_path))
    candidate = Candidate(
        symbol="AAPL",
        price=10.0,
        final_score=90.0,
        action="buy",
        stop_price=9.0,
        target_price=12.0,
        reward_risk=2.0,
        qty=10,
    )

    result = engine.buy_candidates([candidate])

    assert result == []
    assert broker.last_buy is None


def test_buy_candidates_pause_after_recent_pdt_rejection(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.broker_mode = "paper"
    settings.pdt_cooldown_hours = 24
    broker = CaptureBroker(settings)
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=broker, db=db)
    db.record_trade(
        "AAPL",
        "buy",
        1,
        10.0,
        "error",
        'Alpaca request failed: Alpaca error 403: {"code":40310100,"message":"trade denied due to pattern day trading protection"}',
    )
    candidate = Candidate(
        symbol="MSFT",
        price=10.0,
        final_score=90.0,
        action="buy",
        stop_price=9.0,
        target_price=12.0,
        reward_risk=2.0,
        qty=2,
    )

    result = engine.buy_candidates([candidate])

    assert result == []
    assert broker.last_buy is None
    assert "pattern day trading protection" in engine._buying_pause_reason().lower()


def test_trade_once_reports_buying_pause_reason(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.pdt_cooldown_hours = 24
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=build_broker(settings), db=db)
    db.record_trade(
        "AAPL",
        "buy",
        1,
        10.0,
        "error",
        'Alpaca request failed: Alpaca error 403: {"code":40310100,"message":"trade denied due to pattern day trading protection"}',
    )

    result = engine.trade_once()

    assert "buying_paused_reason" in result
    assert "pattern day trading protection" in result["buying_paused_reason"].lower()


def test_reconcile_broker_state_creates_external_position_meta(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.alpaca_key_id = "key"
    settings.alpaca_secret_key = "secret"
    settings.__post_init__()
    position = PositionSnapshot(
        symbol="AAPL",
        qty=3,
        avg_entry_price=10.0,
        current_price=10.5,
        market_value=31.5,
        unrealized_pl_pct=5.0,
    )
    broker = PositionBroker(settings, [position])
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=broker, db=db)

    notes = engine.reconcile_broker_state()
    meta = db.get_position_meta("AAPL")
    trades = db.recent_trades(5)

    assert notes == [{"symbol": "AAPL", "note": "reconciled external position"}]
    assert meta is not None
    assert float(meta["qty"]) == 3.0
    assert trades[0]["note"] == "reconciled external position"


def test_reconcile_broker_state_syncs_partial_fill_qty(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.alpaca_key_id = "key"
    settings.alpaca_secret_key = "secret"
    settings.__post_init__()
    position = PositionSnapshot(
        symbol="AAPL",
        qty=3,
        avg_entry_price=10.0,
        current_price=10.5,
        market_value=31.5,
        unrealized_pl_pct=5.0,
    )
    broker = PositionBroker(settings, [position])
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=broker, db=db)
    db.open_position_meta("AAPL", 5, 10.0, 9.0, 12.0, {"momentum": 100.0})

    notes = engine.reconcile_broker_state()
    meta = db.get_position_meta("AAPL")

    assert notes == [{"symbol": "AAPL", "note": "synced live position metadata"}]
    assert meta is not None
    assert float(meta["qty"]) == 3.0


def test_manage_positions_clears_stale_pending_exit(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.alpaca_key_id = "key"
    settings.alpaca_secret_key = "secret"
    settings.use_broker_protective_orders = False
    settings.__post_init__()
    position = PositionSnapshot(
        symbol="AAPL",
        qty=2,
        avg_entry_price=10.0,
        current_price=10.5,
        market_value=21.0,
        unrealized_pl_pct=5.0,
    )
    broker = PositionBroker(settings, [position])
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=broker, db=db)
    db.open_position_meta("AAPL", 2, 10.0, 9.0, 12.0, {"momentum": 100.0})
    db.set_exit_pending("AAPL", True)

    notes = engine.manage_positions()
    meta = db.get_position_meta("AAPL")

    assert notes == [{"symbol": "AAPL", "note": "cleared stale pending exit"}]
    assert meta is not None
    assert meta["exit_pending"] == 0


def test_buy_candidates_stores_stop_at_or_above_loss_cap_from_fill(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.stop_loss_pct = 0.10
    broker = CaptureBroker(settings)
    engine = TradingEngine(settings=settings, broker=broker, db=Database(settings.db_path))
    candidate = Candidate(
        symbol="AAPL",
        price=10.0,
        final_score=90.0,
        action="buy",
        stop_price=8.7,
        target_price=12.0,
        reward_risk=2.0,
        qty=2,
    )

    engine.buy_candidates([candidate])
    meta = engine.db.get_position_meta("AAPL")

    assert meta is not None
    assert float(meta["stop_price"]) == 9.0


def test_manage_positions_enforces_percent_loss_cap_when_stored_stop_is_looser(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.broker_mode = "paper"
    settings.stop_loss_pct = 0.10
    settings.use_broker_protective_orders = True
    position = PositionSnapshot(
        symbol="AAPL",
        qty=2,
        avg_entry_price=10.0,
        current_price=8.99,
        market_value=17.98,
        unrealized_pl_pct=-10.1,
    )
    broker = PositionBroker(settings, [position])
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=broker, db=db)
    db.open_position_meta("AAPL", 2, 10.0, 8.5, 12.0, {"momentum": 100.0})

    sold = engine.manage_positions()

    assert sold
    assert sold[0]["symbol"] == "AAPL"
    assert "trailing stop" in sold[0]["note"] or sold[0]["note"] in {"stop hit", "loss cap"}
    assert broker.cancel_calls == ["AAPL"]


class PendingSellBroker(PositionBroker):
    def sell(self, symbol: str, qty=None) -> dict:
        self.sell_calls.append((symbol, qty))
        return {"symbol": symbol, "qty": None, "filled_avg_price": None, "status": "accepted"}


def test_manage_positions_handles_unfilled_sell_response(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.broker_mode = "paper"
    settings.stop_loss_pct = 0.10
    settings.use_broker_protective_orders = True
    position = PositionSnapshot(
        symbol="AAPL",
        qty=2,
        avg_entry_price=10.0,
        current_price=8.99,
        market_value=17.98,
        unrealized_pl_pct=-10.1,
    )
    broker = PendingSellBroker(settings, [position])
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=broker, db=db)
    db.open_position_meta("AAPL", 2, 10.0, 8.5, 12.0, {"momentum": 100.0})
    before = engine.learning_weights()

    sold = engine.manage_positions()
    trades = db.recent_trades(5)
    meta = db.get_position_meta("AAPL")
    after = engine.learning_weights()

    assert sold
    assert trades[0]["status"] == "accepted"
    assert trades[0]["qty"] == 2.0
    assert trades[0]["price"] == 8.99
    assert trades[0]["pnl_pct"] is None
    assert meta is not None
    assert meta["exit_pending"] == 1
    assert after == before
