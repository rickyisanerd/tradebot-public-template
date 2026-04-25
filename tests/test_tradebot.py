import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from tradebot.congress import CongressTracker
from tradebot.config import Settings
from tradebot.dashboard import TradingScheduler, create_app
from tradebot.db import Database
from tradebot.engine import TradingEngine
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
    settings.__post_init__()
    settings.congress_report_urls = []
    settings.sec_user_agent = ""
    settings.alpha_vantage_api_key = ""
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

    scheduler.run_cycle()

    assert calls == ["tick"]


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
            "near_cpi_count": 0.0,
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
            "near_cpi_count": 0.0,
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


def test_macro_tracker_parses_cpi_and_fomc_dates(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    tracker = MacroTracker(settings)
    cpi_html = "<html><body>Consumer Price Index April 10, 2026 Consumer Price Index May 12, 2026</body></html>"
    fomc_html = (
        '<html><body>\n'
        '<div>2026 FOMC Meetings</div>\n'
        '<div class="fomc-meeting__month">June</div>\n'
        '<div class="fomc-meeting__date">15-16</div>\n'
        '<div class="fomc-meeting__month">July</div>\n'
        '<div class="fomc-meeting__date">28-29</div>\n'
        '</body></html>'
    )

    # Patch _get_text so we can inject HTML without hitting the network.
    original_get_text = tracker._get_text
    call_count = {"cpi": 0, "fomc": 0}

    def fake_get_text(url: str) -> str:
        if "inflation" in url:
            call_count["cpi"] += 1
            return cpi_html
        call_count["fomc"] += 1
        return fomc_html

    tracker._get_text = fake_get_text  # type: ignore[method-assign]
    try:
        cpi_events = tracker._fetch_cpi()
        fomc_events = tracker._fetch_fomc()
    finally:
        tracker._get_text = original_get_text  # type: ignore[method-assign]

    assert any(event.event_type == "cpi" for event in cpi_events)
    assert any(event.event_type == "fomc" for event in fomc_events)


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
    assert payload["order_class"] == "bracket"
    assert payload["stop_loss"] == {"stop_price": 9.5}
    assert payload["take_profit"] == {"limit_price": 11.25}


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


def test_candidate_symbol_pool_prefilters_for_price_and_liquidity(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
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

    pool = engine._candidate_symbol_pool()

    assert "GOOD1" in pool
    assert "GOOD2" in pool
    assert "JUNK1" not in pool
    assert "JUNK2" not in pool
    assert "HIGH1" not in pool


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
    engine = TradingEngine(settings=settings, broker=FailingScanBroker(settings), db=Database(settings.db_path))

    result = engine.scan_market()

    assert result == []
    assert engine.db.latest_candidates() == []


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
        self._positions = [position for position in self._positions if position.symbol != symbol]
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
    assert trades[0]["status"] == "filled"  # async sells now treated as filled
    assert trades[0]["qty"] == 2.0
    assert trades[0]["price"] == 8.99
    assert trades[0]["pnl_pct"] is not None  # P&L is now calculated immediately
    assert meta is None  # position meta closed immediately
    assert after != before  # learning weights SHOULD update now
