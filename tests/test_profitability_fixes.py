"""Regression tests for the profitability review fixes.

Each test pins one behaviour that was losing money or leaving risk unguarded.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tradebot.config import Settings
from tradebot.dashboard import create_app
from tradebot.db import Database
from tradebot.engine import TradingEngine
from tradebot.models import AccountSnapshot, PositionSnapshot
from tradebot.providers import BaseBroker, ProviderError

from test_tradebot import LiquidityUniverseBroker, _bars, make_settings

WEAK_CONFIRMED = {
    "enabled": True,
    "allow_long_buys": False,
    "inverse_buys_confirmed": True,
    "state": "weak",
    "reason": "SPY/QQQ trend filter is weak",
}
UPTREND = {
    "enabled": True,
    "allow_long_buys": True,
    "inverse_buys_confirmed": False,
    "state": "uptrend",
    "reason": "broad market uptrend confirmed",
}
DISABLED = {"enabled": False, "allow_long_buys": True, "state": "disabled", "reason": "disabled"}


def _paper_settings(tmp_path: Path) -> Settings:
    settings = Settings(data_dir=tmp_path)
    settings.broker_mode = "paper"
    settings.min_stock_price = 2
    settings.max_stock_price = 0
    settings.min_dollar_volume = 500_000
    settings.analyst_consensus_enabled = False
    settings.__post_init__()
    return settings


def _volatile_bars(price: float, volume: int, swing: float, days: int = 40) -> list[dict]:
    """Rising closes with wide daily ranges (ATR ~ 2*swing of price)."""
    bars = []
    for idx in range(days):
        close = round(price * (1 + idx / (days * 200)), 4)
        bars.append(
            {"t": f"2026-03-{(idx % 28) + 1:02d}", "o": close, "h": close * (1 + swing), "l": close * (1 - swing), "c": close, "v": volume}
        )
    return bars


# --- 1. inverse ETFs only as a confirmed-downtrend hedge -------------------

def test_inverse_etf_buyable_only_in_confirmed_weak_regime(tmp_path: Path):
    settings = _paper_settings(tmp_path)
    broker = LiquidityUniverseBroker(settings, {"SH": _bars(40.0, 200_000)})
    engine = TradingEngine(settings=settings, broker=broker, db=Database(settings.db_path))
    bars = broker.bars(["SH"], 40)["SH"]

    hedge = engine._candidate_from_bars("SH", bars, buying_power=10_000, market_regime=dict(WEAK_CONFIRMED))
    assert hedge is not None and hedge.action == "buy"  # otherwise a valid setup

    no_filter = engine._candidate_from_bars("SH", bars, buying_power=10_000, market_regime=dict(DISABLED))
    assert no_filter.action == "watch"
    assert "MARKET_REGIME_FILTER" in no_filter.reasons[0]

    bull = engine._candidate_from_bars("SH", bars, buying_power=10_000, market_regime=dict(UPTREND))
    assert bull.action == "watch"


class HeldPositionBroker(BaseBroker):
    name = "held"

    def __init__(self, settings: Settings, position: PositionSnapshot, *, fail_partial: bool = False) -> None:
        super().__init__(settings)
        self.position = position
        self.fail_partial = fail_partial
        self.sells: list = []

    def account(self) -> AccountSnapshot:
        return AccountSnapshot(cash=1_000, equity=2_000, buying_power=1_000, mode=self.settings.broker_mode)

    def positions(self):
        return [self.position]

    def bars(self, symbols, days):
        return {}

    def latest_prices(self, symbols):
        return {self.position.symbol: self.position.current_price}

    def buy(self, symbol, qty, stop_price=None, target_price=None):
        raise AssertionError("no buys expected")

    def sell(self, symbol, qty=None):
        if self.fail_partial and qty is not None and qty < self.position.qty:
            raise ProviderError("partial sell rejected")
        self.sells.append((symbol, qty))
        return {"symbol": symbol, "qty": qty or self.position.qty, "filled_avg_price": self.position.current_price, "status": "filled"}


def test_inverse_hedge_is_closed_when_regime_is_no_longer_weak(tmp_path: Path):
    settings = make_settings(tmp_path)
    position = PositionSnapshot(symbol="SH", qty=10, avg_entry_price=40, current_price=40.2, market_value=402, unrealized_pl_pct=0.5)
    broker = HeldPositionBroker(settings, position)
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=broker, db=db)
    db.open_position_meta("SH", 10, 40.0, 36.0, 48.0, {"momentum": 60})
    with db.connect() as con:  # held for two days so the exit can't be a day trade
        con.execute("UPDATE position_meta SET opened_at = '2020-01-01T00:00:00+00:00' WHERE symbol = 'SH'")
    engine._market_regime_status = lambda: dict(UPTREND)  # type: ignore[method-assign]
    settings.max_hold_days = 0

    sold = engine.manage_positions()

    assert broker.sells == [("SH", 10)]
    assert "no longer weak" in sold[0]["note"]


# --- 2. universe / price-cap mismatch --------------------------------------

def test_low_price_cap_switches_liquid_universe_to_dynamic(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LIVE_UNIVERSE_MODE", "liquid")
    monkeypatch.setenv("MAX_STOCK_PRICE", "10")
    monkeypatch.delenv("LIQUID_SCAN_UNIVERSE", raising=False)
    settings = Settings(data_dir=tmp_path)
    assert settings.live_universe_mode == "dynamic"
    assert settings.universe_mode_auto_switched

    monkeypatch.setenv("LIQUID_SCAN_UNIVERSE", "SOFI,F,NOK")
    custom = Settings(data_dir=tmp_path)
    assert custom.live_universe_mode == "liquid"  # user's own list is respected


# --- 3. sizing and stops ---------------------------------------------------

def test_position_size_has_no_fixed_dollar_floor(tmp_path: Path):
    settings = _paper_settings(tmp_path)
    settings.risk_per_trade_pct = 0.02
    settings.max_position_pct = 0.20
    settings.max_total_capital = 5_000  # not a small account, so no overrides
    broker = LiquidityUniverseBroker(settings, {"WINR": _bars(5.0, 400_000)})
    engine = TradingEngine(settings=settings, broker=broker, db=Database(settings.db_path))

    candidate = engine._candidate_from_bars("WINR", broker.bars(["WINR"], 40)["WINR"], buying_power=200)

    # 20% of $200 = $40 max position. The old $100 floor allowed $100 (50%).
    assert candidate.qty * candidate.price <= 40.0 + 0.01


def test_scan_sizes_from_equity_not_margin_buying_power(tmp_path: Path):
    settings = _paper_settings(tmp_path)
    seen: list[float] = []

    class MarginBroker(LiquidityUniverseBroker):
        def account(self):
            return AccountSnapshot(cash=1_000, equity=1_000, buying_power=4_000, mode="paper")

    broker = MarginBroker(settings, {"WINR": _bars(5.0, 400_000)})
    engine = TradingEngine(settings=settings, broker=broker, db=Database(settings.db_path))
    original = engine._candidate_from_bars

    def spy(symbol, bars, buying_power, market_regime=None):
        seen.append(buying_power)
        return original(symbol, bars, buying_power, market_regime=market_regime)

    engine._candidate_from_bars = spy  # type: ignore[method-assign]
    engine._market_regime_status = lambda: dict(DISABLED)  # type: ignore[method-assign]
    engine._candidate_symbol_pool = lambda: ["WINR"]  # type: ignore[method-assign]
    engine._auto_scale_limits = lambda: None  # type: ignore[method-assign]
    engine.scan_market()

    assert seen == [1_000]


def test_setup_rejected_when_atr_stop_is_wider_than_loss_cap(tmp_path: Path):
    settings = _paper_settings(tmp_path)
    settings.stop_loss_pct = 0.08
    broker = LiquidityUniverseBroker(
        settings,
        {"CALM": _bars(5.0, 400_000), "WILD": _volatile_bars(5.0, 400_000, swing=0.04)},
    )
    engine = TradingEngine(settings=settings, broker=broker, db=Database(settings.db_path))

    wild = engine._candidate_from_bars("WILD", broker.bars(["WILD"], 40)["WILD"], buying_power=10_000, market_regime=dict(DISABLED))
    assert wild.action == "watch"
    assert any("too volatile" in reason for reason in wild.reasons)

    settings.reject_stops_inside_noise = False
    wild_allowed = engine._candidate_from_bars("WILD", broker.bars(["WILD"], 40)["WILD"], buying_power=10_000, market_regime=dict(DISABLED))
    assert not any("too volatile" in reason for reason in wild_allowed.reasons)


def test_failed_partial_profit_sell_still_runs_stop_checks(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.partial_profit_enabled = True
    settings.partial_profit_pct = 0.15
    settings.trailing_stop_pct = 0.10
    settings.min_hold_days = 0
    settings.max_hold_days = 0
    # Up 16% from entry, but down 42% from a $20 peak: trailing stop must fire.
    position = PositionSnapshot(symbol="RUNR", qty=10, avg_entry_price=10, current_price=11.6, market_value=116, unrealized_pl_pct=16)
    broker = HeldPositionBroker(settings, position, fail_partial=True)
    db = Database(settings.db_path)
    engine = TradingEngine(settings=settings, broker=broker, db=db)
    db.open_position_meta("RUNR", 10, 10.0, 9.2, 30.0, {"momentum": 60})
    db.update_peak_price("RUNR", 20.0)

    sold = engine.manage_positions()

    assert broker.sells == [("RUNR", 10)]
    assert "trailing stop" in sold[0]["note"]


# --- 4. dashboard auth -----------------------------------------------------

def test_dashboard_token_gates_routes_but_not_health(tmp_path: Path):
    settings = make_settings(tmp_path)
    settings.dashboard_token = "s3cret"
    client = TestClient(create_app(settings))

    assert client.get("/health").status_code == 200
    assert client.post("/trade-once", follow_redirects=False).status_code == 401
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"X-Dashboard-Token": "wrong"}).status_code == 401
    assert client.get("/api/status", headers={"X-Dashboard-Token": "s3cret"}).status_code == 200


# --- 5. market data feed ---------------------------------------------------

def test_bars_use_sip_adjusted_feed_and_fall_back_to_iex(tmp_path: Path):
    from tradebot.providers import AlpacaBroker

    settings = _paper_settings(tmp_path)
    settings.alpaca_key_id = "key"
    settings.alpaca_secret_key = "secret"
    calls: list[dict] = []

    class NoSipBroker(AlpacaBroker):
        def _request(self, method, url, **kwargs):
            params = dict(kwargs.get("params") or {})
            calls.append(params)
            if params.get("feed") == "sip":
                raise ProviderError('Alpaca error 403: {"message":"subscription does not permit querying recent SIP data"}')
            return {"bars": {"WINR": [{"t": "2026-03-01", "o": 5, "h": 5.1, "l": 4.9, "c": 5, "v": 1000}]}}

    broker = NoSipBroker(settings)
    assert broker.bars(["WINR"], 10)["WINR"][0]["c"] == 5
    assert [c["feed"] for c in calls] == ["sip", "iex"]
    assert all(c["adjustment"] == "all" for c in calls)

    broker.bars(["WINR"], 10)  # remembers the fallback; no second failed SIP call
    assert [c["feed"] for c in calls] == ["sip", "iex", "iex"]
